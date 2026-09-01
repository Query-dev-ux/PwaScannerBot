from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

from aiogram import Bot
from aiogram.types import FSInputFile
from selenium.common.exceptions import TimeoutException, WebDriverException

from app.config import Settings
from app.db import Database
from app.proxies import pproxy_upstream
from app.services.local_proxy import LocalProxy
from app.services.packer import build_pack
from app.utils import esc, origin_of, pack_caption

log = logging.getLogger(__name__)


class SessionLimit(Exception):
    pass


@dataclass
class InspectResult:
    session_id: str
    name: str
    start_url: str
    scope: str
    installable: bool
    screenshot: str | None
    deep_link: str | None = None
    push_subscribed: bool = False
    push_by: str | None = None
    shell: bool = False
    exit_ip: str | None = None
    exit_isp: str | None = None
    exit_hosting: bool = False
    exit_mobile: bool = False


@dataclass
class PushInfo:
    expires_at: float
    stage: str
    name: str = "сессия"
    download_url: str | None = None
    deep_link: str | None = None
    push_subscribed: bool = False
    push_endpoint: str | None = None


# Push-collection stages, in order.
STAGE_INSTALL = "install"
STAGE_REGISTRATION = "registration"
STAGE_DEPOSIT = "deposit"
STAGE_ORDER = [STAGE_INSTALL, STAGE_REGISTRATION, STAGE_DEPOSIT]
STAGE_LABEL = {
    STAGE_INSTALL: "после установки",
    STAGE_REGISTRATION: "после регистрации",
    STAGE_DEPOSIT: "после депозита",
}


def next_stage(stage: str | None) -> str | None:
    try:
        i = STAGE_ORDER.index(stage or STAGE_INSTALL)
    except ValueError:
        return None
    return STAGE_ORDER[i + 1] if i + 1 < len(STAGE_ORDER) else None


class SessionManager:
    def __init__(self, settings: Settings, db: Database, bot: Bot, webcontrol=None):
        self.s = settings
        self.db = db
        self.bot = bot
        self.webcontrol = webcontrol
        self._sessions: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        os.makedirs(self.s.sessions_dir, exist_ok=True)

    def control_url(self, session_id: str) -> str | None:
        if self.webcontrol:
            return self.webcontrol.url_for(session_id)
        return None

    async def start(self) -> None:
        """Initialize session manager (no-op for undetected-chromedriver)."""
        pass

    async def stop(self) -> None:
        """Close all active sessions."""
        for session_data in list(self._sessions.values()):
            await self._teardown_session(session_data)
        self._sessions.clear()

    # Android device we emulate. PWA funnels (nutra/gambling, mobile-geo offers)
    # hard-403 desktop traffic, so we must look like a real mobile Chrome -
    # including consistent Sec-CH-UA client hints, which Chrome's native mobile
    # emulation produces and a bare UA override does not.
    _ANDROID_MODEL = "Pixel 7"
    _ANDROID_VERSION = "14.0.0"

    def _mobile_ua(self, chrome_major: str) -> str:
        return (
            f"Mozilla/5.0 (Linux; Android 14; {self._ANDROID_MODEL}) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_major}.0.0.0 Mobile Safari/537.36"
        )

    def _setup_undetected_driver(
        self,
        profile_dir: str,
        proxy_url: str | None = None,
        geo: dict | None = None,
    ):
        """Launch undetected-chromedriver emulating a real Android Chrome."""
        import undetected_chromedriver as uc

        chrome_options = uc.ChromeOptions()
        chrome_options.user_data_dir = profile_dir

        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_argument(
            "--force-webrtc-ip-handling-policy=default_public_interface_only"
        )
        chrome_options.add_argument("--disable-features=WebRtcHideLocalIpsWithMdns")

        # Containers / running as root: Chrome refuses the sandbox, and /dev/shm
        # is usually tiny.
        _root = hasattr(os, "geteuid") and os.geteuid() == 0
        if _root or self.s.headless:
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")

        if self.s.headless:
            chrome_options.add_argument("--headless=new")
            chrome_options.add_argument("--disable-gpu")

        chrome_options.page_load_strategy = "eager"

        if proxy_url:
            chrome_options.add_argument(f"--proxy-server={proxy_url}")
            log.info("using proxy: %s", proxy_url)

        # Accept-Language must match the proxy geo (funnels filter on it).
        # NOTE: intl.accept_languages wants a plain comma list of tags
        # ("es-PE,es,en") — feeding it the header string with q-values makes
        # navigator.languages come out as ["es-PE","es;q=0.9",...].
        locale, accept_lang = self._lang_for_geo(geo)
        tags = self._lang_tags(accept_lang)
        chrome_options.add_argument(f"--lang={locale}")
        chrome_options.add_argument(f"--accept-lang={tags}")
        chrome_options.add_experimental_option(
            "prefs", {"intl.accept_languages": tags}
        )

        # Mobile viewport. The real Android UA + consistent Sec-CH-UA client
        # hints are applied via CDP in _apply_stealth (needs the runtime Chrome
        # version, so it can't be done here at options time).
        chrome_options.add_argument("--window-size=393,852")

        driver = uc.Chrome(options=chrome_options, version_main=None)
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(20)
        log.info(
            "launched uc (headless=%s, lang=%s)", self.s.headless, accept_lang
        )
        return driver

    def _lang_for_geo(self, geo: dict | None) -> tuple[str, str]:
        cc = (geo or {}).get("country_code") or ""
        lang, accept_lang = self._LANG_BY_CC.get(cc, ("en", "en-US,en;q=0.9"))
        locale = f"{lang}-{cc}" if cc else "en-US"
        return locale, accept_lang

    @staticmethod
    def _lang_tags(accept_lang: str) -> str:
        """'es-PE,es;q=0.9,en;q=0.8' -> 'es-PE,es,en'"""
        seen, out = set(), []
        for part in accept_lang.split(","):
            t = part.split(";")[0].strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return ",".join(out)

    async def extract_link(self, proxy: dict | None, url: str) -> dict:
        """Fast path: pass the cloaker, resolve the in-app deep link, close.
        No funnel interaction, no manifest deep-dive, no push, no kept session."""
        profile_dir = str(
            (Path(self.s.sessions_dir) / ("_link_" + uuid.uuid4().hex)).resolve()
        )
        os.makedirs(profile_dir, exist_ok=True)
        local_proxy = driver = None
        try:
            proxy_url = None
            if proxy:
                local_proxy = LocalProxy(pproxy_upstream(proxy))
                proxy_url = await local_proxy.start()

            geo = None
            if proxy_url:
                geo = await asyncio.to_thread(self._probe_geo_http, proxy_url)
                if not (geo and geo.get("ip")):
                    raise RuntimeError("прокси не выходит в интернет")

            driver = await asyncio.to_thread(
                self._setup_undetected_driver, profile_dir, proxy_url, geo
            )

            def _dc_hint() -> str:
                if (geo or {}).get("hosting") and not (geo or {}).get("mobile"):
                    return (
                        f"выходной IP {geo.get('ip')} ({geo.get('isp')}) — "
                        "дата-центр, нужна мобильная/резидентная прокси"
                    )
                return "гео/трек-параметры под оффер"

            def _load_past_cloaker():
                """Load url, retrying while the cloaker serves a non-funnel page.
                The verdict is not stable — a reload often gets the real funnel.

                A real PWA offer funnel always ships a web app manifest. The
                cloaker's safe pages (empty 'App Market' shell, or a full
                white-label 'review' site with Google Play links) never do —
                so 'no manifest' is the reliable decoy tell here."""
                last_title = last_kind = ""
                for attempt in range(1, 6):
                    if attempt > 1:
                        try:
                            driver.delete_all_cookies()
                        except Exception:
                            pass
                        time.sleep(2)
                    try:
                        driver.get(url)
                    except TimeoutException:
                        pass
                    time.sleep(3)
                    src = cur = title = ""
                    try:
                        src = driver.page_source or ""
                    except Exception:
                        pass
                    try:
                        cur = driver.current_url or ""
                    except Exception:
                        pass
                    try:
                        title = driver.title or ""
                    except Exception:
                        pass
                    last_title = title or last_title
                    if self._looks_like_error_page(cur, src):
                        raise RuntimeError("сайт не открылся через прокси")
                    body_txt = driver.execute_script(
                        "return document.body?document.body.innerText:''") or ""

                    manifest, murl = {}, None
                    try:
                        murl = driver.execute_script(
                            "var l=document.querySelector('link[rel~=\"manifest\"]');"
                            "return l?l.href:null;"
                        )
                        if murl:
                            import requests

                            manifest = requests.get(murl, timeout=12).json()
                    except Exception:
                        pass

                    if manifest and not self._looks_blocked(title, body_txt):
                        return cur, title, manifest, murl

                    has_store = bool(re.search(
                        r"play\.google\.com/store|apps\.apple\.com", src))
                    if len(body_txt.strip()) < 20:
                        last_kind = "пустая заглушка"
                    elif has_store:
                        last_kind = "белая страница (сайт-заглушка для модерации)"
                    else:
                        last_kind = "не воронка (нет manifest)"
                    log.info(
                        "offer-link scan: decoy on attempt %d — %s (title=%r) — retry",
                        attempt, last_kind, title,
                    )
                raise RuntimeError(
                    f"клоака {attempt} раз(а) отдала {last_kind} вместо воронки "
                    f"({_dc_hint()}); последний title={last_title!r}"
                )

            def work():
                self._apply_stealth(driver, geo)
                cur, title, manifest, murl = _load_past_cloaker()

                # Let the funnel finish loading and run its install flow — some
                # funnels only arm the standalone-launch redirect after the CTA
                # is clicked. This is what the pre-split single "Scan" did.
                try:
                    self._auto_funnel_interaction_sync(driver)
                except Exception as e:  # noqa: BLE001
                    log.warning("funnel interaction failed: %s", e)

                try:
                    cur = driver.current_url or cur
                except Exception:
                    pass
                base = cur if cur.startswith("http") else url
                start_url = urljoin(base, "/")
                if manifest.get("start_url"):
                    start_url = urljoin(murl or base, manifest["start_url"])
                name = (manifest.get("name") or manifest.get("short_name")
                        or title or url)
                log.info(
                    "offer-link scan: name=%r start_url=%s manifest=%s",
                    name, start_url, bool(manifest),
                )
                launch = self._launch_pwa(driver, start_url, link_only=True)
                return {
                    "name": name,
                    "start_url": start_url,
                    "deep_link": launch["deep_link"],
                }

            return await asyncio.to_thread(work)
        finally:
            if driver:
                try:
                    await asyncio.to_thread(driver.quit)
                except Exception:
                    pass
            if local_proxy:
                await local_proxy.stop()
            try:
                import shutil

                shutil.rmtree(profile_dir, ignore_errors=True)
            except Exception:
                pass

    async def open_site(
        self, user_id: int, chat_id: int, proxy: dict | None, url: str
    ) -> InspectResult:
        """Open site and install PWA using undetected-chromedriver."""
        async with self._lock:
            if len(self._sessions) >= self.s.max_sessions:
                from app.services.session_manager import SessionLimit

                raise SessionLimit()

        session_id = uuid.uuid4().hex
        profile_dir = str((Path(self.s.sessions_dir) / session_id).resolve())
        os.makedirs(profile_dir, exist_ok=True)

        local_proxy = None
        driver = None

        try:
            # Start local proxy forwarder if needed
            proxy_url = None
            if proxy:
                local_proxy = LocalProxy(pproxy_upstream(proxy))
                proxy_url = await local_proxy.start()
                log.info("local proxy started: %s", proxy_url)

            # Probe proxy geo BEFORE launch so UA/lang/timezone match on the
            # very first request (funnels 403 on the first hit otherwise).
            geo = None
            if proxy_url:
                geo = await asyncio.to_thread(self._probe_geo_http, proxy_url)
                if geo and geo.get("ip"):
                    log.info(
                        "proxy OK: ip=%s cc=%s city=%s tz=%s isp=%s "
                        "mobile=%s hosting=%s proxy=%s",
                        geo.get("ip"), geo.get("country_code"), geo.get("city"),
                        geo.get("timezone"), geo.get("isp"),
                        geo.get("mobile"), geo.get("hosting"), geo.get("proxy"),
                    )
                    if geo.get("hosting") and not geo.get("mobile"):
                        log.warning(
                            "exit IP %s (%s) is a HOSTING/datacenter range — "
                            "mobile-geo cloakers reject these; a shell/decoy "
                            "response is expected. Use a mobile or residential "
                            "proxy for this offer.",
                            geo.get("ip"), geo.get("isp"),
                        )
                else:
                    raise RuntimeError(
                        "браузер/прокси не выходит в интернет "
                        "(geo-проба не прошла). Проверь прокси."
                    )

            # Launch undetected driver (blocking -> thread)
            driver = await asyncio.to_thread(
                self._setup_undetected_driver, profile_dir, proxy_url, geo
            )

            # Add QA token if configured
            final_url = url
            if self.s.qa_token:
                separator = "&" if "?" in url else "?"
                final_url = f"{url}{separator}qa={self.s.qa_token}"
                log.info("using QA token, URL: %s", final_url.replace(self.s.qa_token, "***"))

            # Navigate + interact + inspect. All blocking Selenium work runs in
            # a worker thread so the aiogram event loop stays responsive.
            info = await asyncio.to_thread(
                self._navigate_and_inspect, driver, final_url, profile_dir, geo
            )

            manifest = info["manifest"]
            installable = info["installable"]
            screenshot = info["screenshot"]
            start_url = info["start_url"]
            name = info["name"]
            scope = info["scope"]
            deep_link = info.get("deep_link")

            # Keep the session in memory ONLY. It is persisted to the DB later,
            # when the user actually turns on push collection.
            self._sessions[session_id] = {
                "id": session_id,
                "driver": driver,
                "local_proxy": local_proxy,
                "user_id": user_id,
                "chat_id": chat_id,
                "proxy": proxy,
                "site_url": url,
                "pwa_name": name,
                "start_url": start_url,
                "scope": scope,
                "deep_link": deep_link,
                "profile_dir": profile_dir,
                "scanned_at": time.time(),
                "collecting": False,
                "stage": None,
                "push_queue": [],
                "observer": None,
                "push_subscribed": info.get("push_subscribed", False),
                "push_endpoint": info.get("push_endpoint"),
                "push_by": info.get("push_by"),
            }

            return InspectResult(
                session_id, name, start_url, scope, installable, screenshot,
                deep_link, bool(info.get("push_subscribed")),
                info.get("push_by"), bool(info.get("shell")),
                (geo or {}).get("ip"), (geo or {}).get("isp"),
                bool((geo or {}).get("hosting")), bool((geo or {}).get("mobile")),
            )

        except Exception as e:
            if driver:
                try:
                    await asyncio.to_thread(driver.quit)
                except Exception:
                    pass
            if local_proxy:
                await local_proxy.stop()
            raise

    # ------------------------------------------------------------------
    # Blocking helpers - always call via asyncio.to_thread
    # ------------------------------------------------------------------

    _ERROR_NEEDLES = (
        "ERR_TIMED_OUT",
        "ERR_PROXY_CONNECTION_FAILED",
        "ERR_TUNNEL_CONNECTION_FAILED",
        "ERR_SOCKS_CONNECTION_FAILED",
        "ERR_CONNECTION_TIMED_OUT",
        "ERR_CONNECTION_REFUSED",
        "ERR_CONNECTION_RESET",
        "ERR_CONNECTION_CLOSED",
        "ERR_NAME_NOT_RESOLVED",
        "ERR_EMPTY_RESPONSE",
        "DNS_PROBE_FINISHED",
        "Не удалось получить доступ к сайту",
        "This site can’t be reached",
        "took too long to respond",
    )

    @classmethod
    def _looks_like_error_page(cls, current_url: str, page_source: str) -> bool:
        if current_url.startswith("chrome-error://") or "chromewebdata" in current_url:
            return True
        head = page_source[:4000]
        return any(n in head for n in cls._ERROR_NEEDLES)

    _BLOCK_NEEDLES = (
        "Access Denied",
        "access denied",
        "Доступ запрещён",
        "Доступ запрещен",
        "доступ к",  # "Доступ к <site> запрещён"
        "403 Forbidden",
        "Error 403",
        "HTTP ERROR 403",
        "You don't have permission to access",
        "you have been blocked",
        "Sorry, you have been blocked",
        "Attention Required! | Cloudflare",
        "Just a moment...",
        "cf-browser-verification",
        "Checking your browser before",
        "Enable JavaScript and cookies to continue",
        "captcha-delivery.com",
        "Request blocked",
        "blocked by the security",
    )

    @classmethod
    def _looks_blocked(cls, title: str, page_source: str) -> bool:
        blob = (title + "\n" + page_source[:8000])
        low = blob.lower()
        for n in cls._BLOCK_NEEDLES:
            if n.lower() in low:
                return True
        return False

    # country code -> (primary language, Accept-Language header)
    _LANG_BY_CC = {
        "US": ("en", "en-US,en;q=0.9"),
        "GB": ("en", "en-GB,en;q=0.9"),
        "CA": ("en", "en-CA,en;q=0.9,fr-CA;q=0.7"),
        "AU": ("en", "en-AU,en;q=0.9"),
        "DE": ("de", "de-DE,de;q=0.9,en;q=0.8"),
        "FR": ("fr", "fr-FR,fr;q=0.9,en;q=0.8"),
        "ES": ("es", "es-ES,es;q=0.9,en;q=0.8"),
        "IT": ("it", "it-IT,it;q=0.9,en;q=0.8"),
        "NL": ("nl", "nl-NL,nl;q=0.9,en;q=0.8"),
        "PL": ("pl", "pl-PL,pl;q=0.9,en;q=0.8"),
        "PT": ("pt", "pt-PT,pt;q=0.9,en;q=0.8"),
        "BR": ("pt", "pt-BR,pt;q=0.9,en;q=0.8"),
        "MX": ("es", "es-MX,es;q=0.9,en;q=0.8"),
        "PE": ("es", "es-PE,es;q=0.9,en;q=0.8"),
        "CL": ("es", "es-CL,es;q=0.9,en;q=0.8"),
        "AR": ("es", "es-AR,es;q=0.9,en;q=0.8"),
        "CO": ("es", "es-CO,es;q=0.9,en;q=0.8"),
        "RU": ("ru", "ru-RU,ru;q=0.9,en;q=0.8"),
        "UA": ("uk", "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7"),
        "TR": ("tr", "tr-TR,tr;q=0.9,en;q=0.8"),
        "IN": ("en", "en-IN,en;q=0.9,hi;q=0.8"),
    }

    @staticmethod
    def _probe_geo_http(proxy_url: str) -> dict | None:
        """Fetch the proxy's exit IP + geo via the local HTTP forwarder.

        Runs before the browser launches so UA / language / timezone can be set
        correctly on the very first request. None => proxy has no connectivity.
        """
        import requests

        proxies = {"http": proxy_url, "https": proxy_url}
        try:
            r = requests.get(
                "http://ip-api.com/json/?fields=status,message,country,"
                "countryCode,region,city,timezone,lat,lon,query,isp,"
                "mobile,proxy,hosting",
                proxies=proxies,
                timeout=25,
            )
            data = r.json()
            if data.get("status") == "success":
                return {
                    "ip": data.get("query"),
                    "country_code": (data.get("countryCode") or "").upper(),
                    "country": data.get("country"),
                    "city": data.get("city"),
                    "timezone": data.get("timezone"),
                    "lat": data.get("lat"),
                    "lon": data.get("lon"),
                    "isp": data.get("isp"),
                    "mobile": bool(data.get("mobile")),
                    "proxy": bool(data.get("proxy")),
                    "hosting": bool(data.get("hosting")),
                }
        except Exception as e:  # noqa: BLE001
            log.warning("geo probe (ip-api) failed: %s", e)

        # Fallback: just confirm the tunnel carries traffic at all.
        try:
            r = requests.get(
                "https://api.ipify.org/?format=json", proxies=proxies, timeout=20
            )
            ip = r.json().get("ip")
            return {"ip": ip} if ip else None
        except Exception as e:  # noqa: BLE001
            log.warning("geo probe (ipify) failed: %s", e)
            return None

    _STEALTH_JS = """
    (() => {
      try { Object.defineProperty(navigator, 'webdriver', {get: () => false}); } catch (e) {}
      // real Android reports a touchscreen
      try { Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 5}); } catch (e) {}
      try {
        if (!window.chrome) window.chrome = {};
        if (!window.chrome.runtime) window.chrome.runtime = {};
      } catch (e) {}
      try {
        const q = window.navigator.permissions && window.navigator.permissions.query;
        if (q) {
          window.navigator.permissions.query = (p) =>
            (p && p.name === 'notifications')
              ? Promise.resolve({state: Notification.permission})
              : q.call(window.navigator.permissions, p);
        }
      } catch (e) {}
      try {
        window.__bipFired = false;
        window.__bipEvent = null;
        window.__appInstalled = false;
        window.addEventListener('beforeinstallprompt', (e) => {
          e.preventDefault();
          window.__bipEvent = e;
          window.__bipFired = true;
        });
        window.addEventListener('appinstalled', () => { window.__appInstalled = true; });
      } catch (e) {}
      // --- push: make the funnel believe consent is already given, and grab
      // the VAPID key it wants to use so we can subscribe ourselves if needed.
      try {
        try { Object.defineProperty(Notification, 'permission', {get: () => 'granted'}); } catch (e) {}
        const _rp = Notification.requestPermission.bind(Notification);
        Notification.requestPermission = function (cb) {
          if (typeof cb === 'function') { try { cb('granted'); } catch (e) {} }
          return Promise.resolve('granted');
        };
        if (window.PushManager && PushManager.prototype.subscribe) {
          const _sub = PushManager.prototype.subscribe;
          PushManager.prototype.subscribe = function (opts) {
            try {
              let k = opts && opts.applicationServerKey;
              if (typeof k === 'string') window.__vapidKey = k;
              else if (k) {
                const u = new Uint8Array(k.buffer ? k.buffer : k);
                let s = ''; for (let i = 0; i < u.length; i++) s += String.fromCharCode(u[i]);
                window.__vapidKey = btoa(s).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
              }
            } catch (e) {}
            return _sub.apply(this, arguments);
          };
        }
      } catch (e) {}
    })();
    """

    def _apply_stealth(self, driver, geo: dict | None) -> None:
        """Align the browser fingerprint with the proxy's geo before navigation.

        Timezone / locale / language / geolocation that don't match the exit IP
        are the fastest way to get a 403 from DataDome / Imperva / Cloudflare.
        """
        cc = (geo or {}).get("country_code") or ""
        locale, accept_lang = self._lang_for_geo(geo)
        # CDP acceptLanguage / prefs want plain tags — Chrome adds the q-values.
        # Passing the header string ("es;q=0.9") gets it doubled ("es;q=0.9;q=0.9").
        lang_tags = self._lang_tags(accept_lang)
        tz = (geo or {}).get("timezone")

        # Emulate a real Android Chrome: UA + FULL userAgentMetadata so the
        # Sec-CH-UA / Sec-CH-UA-Mobile / Sec-CH-UA-Platform headers stay
        # consistent with the UA string. PWA funnels hard-403 on any mismatch
        # (verified: mobile UA alone = 403, mobile UA + matching hints = 200).
        try:
            ver = driver.execute_cdp_cmd("Browser.getVersion", {})
            full = re.search(r"[\d.]+", ver.get("product", "") or "")
            full_ver = full.group(0) if full else "131.0.0.0"
        except Exception:
            full_ver = "131.0.0.0"
        major = full_ver.split(".")[0]
        mobile_ua = self._mobile_ua(major)
        brands = [
            {"brand": "Not(A:Brand", "version": "24"},
            {"brand": "Chromium", "version": major},
            {"brand": "Google Chrome", "version": major},
        ]
        full_list = [
            {"brand": "Not(A:Brand", "version": "24.0.0.0"},
            {"brand": "Chromium", "version": full_ver},
            {"brand": "Google Chrome", "version": full_ver},
        ]
        try:
            driver.execute_cdp_cmd(
                "Network.setUserAgentOverride",
                {
                    "userAgent": mobile_ua,
                    "acceptLanguage": lang_tags,
                    "platform": "Linux armv8l",
                    "userAgentMetadata": {
                        "brands": brands,
                        "fullVersionList": full_list,
                        "platform": "Android",
                        "platformVersion": self._ANDROID_VERSION,
                        "architecture": "",
                        "model": self._ANDROID_MODEL,
                        "mobile": True,
                        "bitness": "",
                        "wow64": False,
                    },
                },
            )
        except Exception as e:  # noqa: BLE001
            log.warning("mobile UA override failed: %s", e)

        try:
            driver.execute_cdp_cmd(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": 393,
                    "height": 852,
                    "deviceScaleFactor": 2.75,
                    "mobile": True,
                    "screenWidth": 393,
                    "screenHeight": 852,
                },
            )
            driver.execute_cdp_cmd(
                "Emulation.setTouchEmulationEnabled",
                {"enabled": True, "maxTouchPoints": 5},
            )
        except Exception as e:  # noqa: BLE001
            log.warning("device metrics override failed: %s", e)

        if tz:
            try:
                driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": tz})
            except Exception as e:  # noqa: BLE001
                log.warning("timezone override failed (%s): %s", tz, e)

        try:
            driver.execute_cdp_cmd("Emulation.setLocaleOverride", {"locale": locale})
        except Exception as e:  # noqa: BLE001
            log.warning("locale override failed: %s", e)

        lat, lon = (geo or {}).get("lat"), (geo or {}).get("lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            try:
                driver.execute_cdp_cmd(
                    "Emulation.setGeolocationOverride",
                    {"latitude": lat, "longitude": lon, "accuracy": 80},
                )
            except Exception as e:  # noqa: BLE001
                log.warning("geolocation override failed: %s", e)

        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument", {"source": self._STEALTH_JS}
            )
            # navigator.languages must be clean tags, not the Accept-Language
            # header string (Chrome's pref handling can mangle it).
            langs = self._lang_tags(accept_lang).split(",")
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source":
                 "try{Object.defineProperty(navigator,'languages',"
                 f"{{get:()=>{json.dumps(langs)}}});}}catch(e){{}}"
                 "try{Object.defineProperty(navigator,'language',"
                 f"{{get:()=>{json.dumps(langs[0])}}});}}catch(e){{}}"},
            )
        except Exception as e:  # noqa: BLE001
            log.warning("stealth script inject failed: %s", e)

        log.info(
            "stealth applied: cc=%s tz=%s locale=%s langs=%s ua=Android/Chrome %s",
            cc or "?", tz or "?", locale, self._lang_tags(accept_lang), major,
        )

    def _navigate_and_inspect(
        self, driver, url: str, profile_dir: str, geo: dict | None = None
    ) -> dict:
        """Open the target, run the funnel, read manifest/SW, screenshot.

        Raises RuntimeError with a user-facing message when the site itself
        did not load (Chrome error page / empty response / anti-bot block).
        """
        # Match fingerprint to proxy geo BEFORE hitting the target.
        self._apply_stealth(driver, geo)

        log.info("opening site: %s", url)
        nav_timeout = False
        try:
            driver.get(url)
        except TimeoutException:
            nav_timeout = True
            log.warning("page load timeout (eager) - continuing with partial DOM")
        except WebDriverException as e:
            raise RuntimeError(f"навигация не удалась: {e.msg or e}") from e

        time.sleep(3)

        current_url, page_source = "", ""
        try:
            current_url = driver.current_url or ""
        except Exception as e:  # noqa: BLE001
            log.warning("current_url failed: %s", e)
        try:
            page_source = driver.page_source or ""
        except Exception as e:  # noqa: BLE001
            log.warning("page_source failed: %s", e)

        if self._looks_like_error_page(current_url, page_source):
            raise RuntimeError(
                "сайт не открылся — Chrome показывает «не удалось получить "
                "доступ к сайту». Прокси не пропускает трафик браузера или "
                f"сайт недоступен с IP прокси. URL: {current_url or url}"
            )
        if nav_timeout and len(page_source.strip()) < 200:
            raise RuntimeError(
                "страница не загрузилась за 60 с (пустой ответ через прокси)"
            )

        page_title = ""
        try:
            page_title = driver.title or ""
        except Exception:
            pass

        # Always dump what we actually got, so blocks can be diagnosed.
        page_text = ""
        try:
            page_text = driver.execute_script(
                "return document.body ? document.body.innerText : '';"
            ) or ""
        except Exception:
            pass
        try:
            Path(profile_dir, "page.html").write_text(page_source, encoding="utf-8")
            driver.save_screenshot(str(Path(profile_dir, "landing.png")))
        except Exception as e:  # noqa: BLE001
            log.warning("diag dump failed: %s", e)
        log.info(
            "landing: title=%r url=%s dom=%dB text[:300]=%r",
            page_title, current_url or url, len(page_source), page_text[:300],
        )
        log.info("diag files in: %s", profile_dir)

        if self._looks_blocked(page_title, page_source) or self._looks_blocked(
            page_title, page_text
        ):
            raise RuntimeError(
                "сайт вернул страницу блокировки (403 / анти-бот WAF). "
                f"Заголовок: {page_title!r}. Дамп: {profile_dir}\\page.html + "
                "landing.png. Сеть/прокси/гео в порядке — блокирует фингерпринт."
            )

        log.info("site loaded (%d bytes DOM): %s", len(page_source), current_url or url)

        # Funnel: scroll, spin wheel, click install
        try:
            self._auto_funnel_interaction_sync(driver)
        except Exception as e:  # noqa: BLE001
            log.warning("funnel interaction failed: %s", e)

        # Manifest
        manifest: dict = {}
        try:
            manifest_url = driver.execute_script(
                "var l=document.querySelector('link[rel~=\"manifest\"]');"
                "return l ? l.href : null;"
            )
            if manifest_url:
                import requests

                resp = requests.get(manifest_url, timeout=15)
                manifest = resp.json()
                if manifest.get("start_url"):
                    manifest["start_url"] = urljoin(manifest_url, manifest["start_url"])
                if manifest.get("scope"):
                    manifest["scope"] = urljoin(manifest_url, manifest["scope"])
                log.info("manifest read: %s", manifest.get("name") or manifest_url)
        except Exception as e:  # noqa: BLE001
            log.warning("manifest read failed: %s", e)

        try:
            current_url = driver.current_url or current_url
        except Exception:
            pass

        start_url = manifest.get("start_url") or current_url or url
        name = (
            manifest.get("name")
            or manifest.get("short_name")
            or page_title
            or url
        )
        scope = manifest.get("scope") or start_url

        # A funnel with no manifest AND an empty <body> is a cloaker decoy /
        # broken landing — don't burn ~5 min on push-subscription retries.
        shell = not manifest and len((page_text or "").strip()) < 20
        if shell:
            log.warning(
                "funnel returned an empty shell (title=%r, no manifest) — "
                "likely a cloaker decoy; skipping push subscription", page_title,
            )
            launch = self._launch_pwa(driver, start_url, link_only=True)
            early = {"subscribed": False, "endpoint": None, "sw": False}
        else:
            # Best-effort: if the funnel subscribes on the normal page
            # (yap-games style), grab it now.
            early = self._subscribe_push(driver, url, budget_ms=8000)
            # Simulate the PWA launch: resolve the in-app deep link AND collect
            # the push subscription (fake-store funnels only subscribe here).
            launch = self._launch_pwa(driver, start_url)
        deep_link = launch["deep_link"]
        push_subscribed = early["subscribed"] or launch["push_subscribed"]
        push_endpoint = early["endpoint"] or launch["push_endpoint"]
        push_by = "funnel" if early["subscribed"] else launch.get("push_by")
        installable = bool(early["sw"]) or push_subscribed

        # Park the main tab back on the funnel origin so a later attempt has SW
        # context without re-triggering the cloaker.
        try:
            if origin_of(driver.current_url or "") != origin_of(start_url):
                driver.get(start_url)
                time.sleep(3)
        except Exception:
            pass

        # Screenshot
        screenshot: str | None = None
        try:
            path = str(Path(profile_dir) / "pwa.png")
            driver.save_screenshot(path)
            screenshot = path
        except Exception as e:  # noqa: BLE001
            log.warning("screenshot failed: %s", e)

        return {
            "manifest": manifest,
            "installable": installable,
            "screenshot": screenshot,
            "start_url": start_url,
            "name": name,
            "scope": scope,
            "deep_link": deep_link,
            "push_subscribed": push_subscribed,
            "push_endpoint": push_endpoint,
            "push_by": push_by,
            "shell": shell,
        }

    _SUBSCRIBE_JS = r"""
    const budgetMs = arguments[0] || 15000;
    const cb = arguments[arguments.length - 1];
    const wait = ms => new Promise(r => setTimeout(r, ms));
    function b64u(s) {
      s = String(s).replace(/-/g, '+').replace(/_/g, '/');
      s += '='.repeat((4 - s.length % 4) % 4);
      const b = atob(s), u = new Uint8Array(b.length);
      for (let i = 0; i < b.length; i++) u[i] = b.charCodeAt(i);
      return u;
    }
    (async () => {
      try {
        if (!('serviceWorker' in navigator)) return cb({sw:false, reason:'no-sw-api'});
        let reg = null;
        for (let i = 0; i < 18; i++) {              // wait for an ACTIVE worker
          reg = await navigator.serviceWorker.getRegistration();
          if (reg && reg.active) break;
          await wait(1000);
        }
        if (!reg) { try { reg = await Promise.race([navigator.serviceWorker.ready, wait(2000)]); } catch(e){} }
        if (!reg) return cb({sw:false, reason:'no-registration'});
        if (!reg.active) return cb({sw:true, reason:'sw-not-active'});

        let lastErr = null;
        const deadline = Date.now() + budgetMs;
        do {
          let s = await reg.pushManager.getSubscription();
          if (s) return cb({sw:true, subscribed:true, endpoint:s.endpoint, by:'funnel'});
          if (window.__vapidKey) {
            try {
              s = await reg.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: b64u(window.__vapidKey),
              });
              return cb({sw:true, subscribed:true, endpoint:s.endpoint, by:'self'});
            } catch (e) { lastErr = String(e); }
          }
          await wait(2500);
        } while (Date.now() < deadline);

        cb({sw:true, subscribed:false,
            reason: window.__vapidKey ? ('subscribe: ' + lastErr) : 'no-vapid-key'});
      } catch (e) { cb({sw:false, reason:String(e)}); }
    })();
    """

    _WAIT_SW_JS = r"""
    const cb = arguments[arguments.length - 1];
    (async () => {
      try {
        for (let i = 0; i < 25; i++) {
          const r = await navigator.serviceWorker.getRegistration();
          if (r && r.active) return cb(true);
          await new Promise(x => setTimeout(x, 1000));
        }
        cb(false);
      } catch (e) { cb(false); }
    })();
    """

    _FUNNEL_SUB_JS = r"""
    const budgetMs = arguments[0] || 3000;
    const cb = arguments[arguments.length - 1];
    (async () => {
      try {
        const reg = await navigator.serviceWorker.getRegistration();
        if (!reg) return cb({});
        const deadline = Date.now() + budgetMs;
        do {
          const s = await reg.pushManager.getSubscription();
          if (s) return cb({endpoint: s.endpoint});
          await new Promise(x => setTimeout(x, 1200));
        } while (Date.now() < deadline);
        cb({});
      } catch (e) { cb({}); }
    })();
    """

    def _subscribe_push(
        self, driver, funnel_url: str, budget_ms: int = 12000, allow_navigate: bool = False
    ) -> dict:
        """Ensure a PushSubscription exists in this browser for the funnel origin.

        Grants the permission, waits for the SW to activate, gives the funnel
        time to subscribe, and self-subscribes with the VAPID key captured from
        the funnel's own subscribe() call as a fallback.
        """
        out = {"sw": False, "subscribed": False, "endpoint": None, "reason": None}
        origin = origin_of(funnel_url)
        try:
            driver.execute_cdp_cmd(
                "Browser.grantPermissions",
                {"origin": origin, "permissions": ["notifications"]},
            )
        except Exception as e:  # noqa: BLE001
            log.warning("grant notifications (%s) failed: %s", origin, e)

        try:
            if allow_navigate and origin_of(driver.current_url or "") != origin:
                log.info("push: navigating to funnel origin for subscription")
                driver.get(funnel_url)
                time.sleep(5)

            driver.set_script_timeout(budget_ms / 1000 + 55)
            r = driver.execute_async_script(self._SUBSCRIBE_JS, budget_ms) or {}
            out["sw"] = bool(r.get("sw"))
            out["reason"] = r.get("reason")
            if r.get("subscribed") and r.get("endpoint"):
                out.update(subscribed=True, endpoint=r["endpoint"])
                log.info("push subscribed (%s): %s",
                         r.get("by", "?"), r["endpoint"].split("/")[2])
            else:
                log.info("push not subscribed: sw=%s reason=%s",
                         r.get("sw"), r.get("reason"))
        except Exception as e:  # noqa: BLE001
            log.warning("push subscribe failed: %s", e)
        finally:
            try:
                driver.set_script_timeout(20)
            except Exception:
                pass
        return out

    # display-mode:standalone + navigator.standalone spoof so the funnel serves
    # the "app was launched" branch (which redirects to the real offer).
    _STANDALONE_SPOOF_JS = (
        "(()=>{const mm=window.matchMedia.bind(window);"
        "window.matchMedia=(q)=>q&&/display-mode:\\s*standalone/i.test(q)"
        "?{matches:true,media:q,onchange:null,addListener(){},removeListener(){},"
        "addEventListener(){},removeEventListener(){},dispatchEvent(){return false;}}"
        ":mm(q);"
        "try{Object.defineProperty(navigator,'standalone',{get:()=>true});}catch(e){}})();"
    )


    def _launch_pwa(self, driver, start_url: str, link_only: bool = False) -> dict:
        """Open start_url in a fresh tab emulating a PWA launch. Follows the
        redirect chain to the in-app deep link AND (unless link_only) grabs the
        push subscription the funnel creates on that standalone launch."""
        out = {"deep_link": start_url, "push_subscribed": False,
               "push_endpoint": None, "push_by": None}
        if not str(start_url).lower().startswith(("http://", "https://")):
            log.warning("pwa launch: bad start_url %r", start_url)
            return out
        original = None
        origin = origin_of(start_url)
        try:
            original = driver.current_window_handle
            driver.switch_to.new_window("tab")
            try:
                # the new tab is a fresh target - re-inject the page hooks
                # (VAPID capture + Notification.permission=granted live here too)
                for src in (self._STEALTH_JS, self._STANDALONE_SPOOF_JS):
                    driver.execute_cdp_cmd(
                        "Page.addScriptToEvaluateOnNewDocument", {"source": src}
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("launch-tab hook inject failed: %s", e)

            driver.set_page_load_timeout(45)
            driver.set_script_timeout(12)
            norm = lambda u: (u or "").rstrip("/").split("#")[0]

            def _grant():
                if link_only:
                    return
                try:
                    driver.execute_cdp_cmd(
                        "Browser.grantPermissions",
                        {"origin": origin, "permissions": ["notifications"]},
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("grant notifications failed: %s", e)

            def _grab_sub(budget_ms):
                if out["push_endpoint"]:
                    return
                try:
                    driver.set_script_timeout(budget_ms / 1000 + 6)
                    r = driver.execute_async_script(self._SUBSCRIBE_JS, budget_ms) or {}
                    driver.set_script_timeout(12)
                    if r.get("endpoint"):
                        out.update(push_subscribed=True, push_endpoint=r["endpoint"],
                                   push_by=r.get("by") or "self")
                        log.info("pwa launch: push subscribed (%s) %s",
                                 r.get("by"), r["endpoint"].split("/")[2])
                except Exception as e:  # noqa: BLE001
                    log.warning("sub grab failed: %s", e)

            def _funnel_sub(budget_ms):
                """Check ONLY for a subscription the funnel itself created
                (it POSTs that endpoint to its backend -> pushes get delivered;
                a self-made one does not)."""
                if out["push_endpoint"]:
                    return
                try:
                    if origin_of(driver.current_url or "") != origin:
                        return
                    driver.set_script_timeout(budget_ms / 1000 + 45)
                    r = driver.execute_async_script(
                        self._FUNNEL_SUB_JS, budget_ms) or {}
                    driver.set_script_timeout(12)
                    if r.get("endpoint"):
                        out.update(push_subscribed=True, push_endpoint=r["endpoint"],
                                   push_by="funnel")
                        log.info("pwa launch: push subscribed (funnel) %s",
                                 r["endpoint"].split("/")[2])
                except Exception as e:  # noqa: BLE001
                    log.warning("funnel-sub check failed: %s", e)

            try:
                driver.get(start_url)
            except TimeoutException:
                pass
            _grant()

            # Follow the redirect chain. link_only used to bail the moment the
            # page held still for 3s — which fired BEFORE the funnel's deferred
            # redirect (setTimeout / post-init), so the deep link came back as
            # start_url. Now both modes wait out the chain; we only stop early
            # once we've actually landed somewhere other than start_url.
            last, stable, final = None, 0, start_url
            for i in range(24):
                if not link_only:
                    _funnel_sub(2500)
                time.sleep(1)
                try:
                    cur = driver.current_url or ""
                except Exception:
                    break
                if cur and not cur.startswith("about:"):
                    final = cur
                redirected = (
                    bool(cur) and not cur.startswith("about:")
                    and norm(cur) != norm(start_url)
                )
                if cur == last:
                    stable += 1
                    if stable >= 3 and (
                        redirected
                        or (link_only and i >= 14)
                        or (out["push_endpoint"] and i >= 12)
                    ):
                        break
                else:
                    if cur != start_url:
                        log.info("pwa launch nav [%d]: %s", i, cur)
                    stable, last = 0, cur

            if norm(final) and norm(final) != norm(start_url):
                out["deep_link"] = final
                log.info("pwa deep link: %s", final)
            else:
                log.info("no redirect on PWA launch - deep link = start_url")
                if link_only:
                    self._log_launch_state(driver, origin)

            if not link_only and not out["push_endpoint"]:
                # The funnel fires subscribe() before its SW is active and never
                # retries. Reload the funnel a couple of times once the SW is
                # active so it can subscribe for real (and POST to its backend).
                for rnd in range(2):
                    try:
                        driver.get(origin + "/")
                        time.sleep(2)
                        _grant()
                        driver.set_script_timeout(30)
                        driver.execute_async_script(self._WAIT_SW_JS)
                        driver.set_script_timeout(12)
                        driver.get(start_url)
                        _grant()
                    except Exception as e:  # noqa: BLE001
                        log.warning("re-launch %d failed: %s", rnd + 1, e)
                        break
                    for _ in range(8):
                        _funnel_sub(2500)
                        if out["push_endpoint"]:
                            break
                        time.sleep(1)
                    if out["push_endpoint"]:
                        break
                    log.info("pwa launch: funnel still not subscribed (round %d)", rnd + 1)

                # absolute last resort — self-subscribe (may not receive pushes)
                if not out["push_endpoint"]:
                    try:
                        driver.get(origin + "/favicon.ico")
                        time.sleep(1.5)
                        _grab_sub(15000)
                        if out["push_endpoint"]:
                            log.warning("pwa launch: only a SELF subscription — "
                                        "pushes may not be delivered")
                    except Exception as e:  # noqa: BLE001
                        log.warning("self-sub fallback failed: %s", e)
        except Exception as e:  # noqa: BLE001
            log.warning("pwa launch failed: %s", e)
        finally:
            try:
                driver.close()
            except Exception:
                pass
            try:
                if original:
                    driver.switch_to.window(original)
            except Exception:
                pass
            try:
                driver.set_page_load_timeout(60)
                driver.set_script_timeout(20)
            except Exception:
                pass
        return out

    def _log_launch_state(self, driver, origin: str) -> None:
        """Dump what the standalone launch actually rendered when no redirect
        was seen — so we can tell WHY the deep link didn't resolve."""
        try:
            info = driver.execute_script(
                r"""
                const origin = arguments[0];
                const abs = (h) => { try { return new URL(h, location.href).href; }
                                     catch(e) { return null; } };
                const meta = document.querySelector(
                    'meta[http-equiv="refresh" i]');
                const offOrigin = [...document.querySelectorAll('a[href]')]
                    .map(a => a.href)
                    .filter(h => { try { return new URL(h).origin !== origin
                        && /^https?:/.test(h); } catch(e){ return false; } });
                const scripts = [...document.scripts]
                    .map(s => s.textContent || '').join('\n');
                const locHits = (scripts.match(
                    /(location\.(href|replace|assign)|window\.open)\s*[=(]\s*['"`][^'"`]+/g)
                    || []).slice(0, 6);
                const globals = Object.keys(window).filter(k =>
                    /link|offer|redirect|target|deep|url/i.test(k)).slice(0, 15);
                return {
                    title: document.title,
                    bodyLen: (document.body ? document.body.innerText : '').length,
                    bodySnippet: (document.body ? document.body.innerText : '')
                        .slice(0, 200),
                    hasSW: !!navigator.serviceWorker.controller,
                    displayMode: window.matchMedia('(display-mode: standalone)').matches,
                    metaRefresh: meta ? meta.content : null,
                    offOriginLinks: [...new Set(offOrigin)].slice(0, 8),
                    locationCalls: locHits,
                    suspectGlobals: globals,
                };
                """,
                origin,
            )
            log.info("launch state (no redirect): %s",
                     json.dumps(info, ensure_ascii=False)[:1500])
        except Exception as e:  # noqa: BLE001
            log.warning("launch-state probe failed: %s", e)

    def _wait_for_funnel(self, driver, timeout: float = 45.0) -> None:
        """Wait for the funnel's game/loader to finish and CTA to appear."""
        deadline = time.time() + timeout
        loader_re = re.compile(r"starting renderer|loading|rendering|\b\d{1,3}\s*%", re.I)
        cta_re = re.compile(
            r"install|obtener|instalar|descargar|reclamar|get|claim|jugar|play|"
            r"установ|получить|играть|abrir",
            re.I,
        )
        while time.time() < deadline:
            try:
                if driver.execute_script("return window.__bipFired === true;"):
                    log.info("funnel ready: beforeinstallprompt fired")
                    return
                txt = driver.execute_script(
                    "return (document.body && document.body.innerText) || '';"
                ) or ""
                has_cta = driver.execute_script(
                    """
                    const re = arguments[0];
                    const rx = new RegExp(re, 'i');
                    return [...document.querySelectorAll('button,a,[role=button],[onclick]')]
                        .some(b => b.offsetHeight > 0 && rx.test(b.textContent || ''));
                    """,
                    cta_re.pattern,
                )
                if has_cta and not loader_re.search(txt[:400]):
                    log.info("funnel ready: CTA visible")
                    return
            except Exception as e:  # noqa: BLE001
                log.warning("wait_for_funnel probe error: %s", e)
            time.sleep(2)
        log.info("funnel wait timed out (%.0fs) - proceeding anyway", timeout)

    def _auto_funnel_interaction_sync(self, driver) -> None:
        """Automatically interact with PWA installation funnel (synchronous, blocking)."""
        try:
            log.info("starting funnel interaction")

            self._wait_for_funnel(driver)

            # Scroll page like human would
            for _ in range(random.randint(2, 4)):
                driver.execute_script(f"window.scrollBy(0, {random.randint(100, 300)})")
                time.sleep(random.uniform(0.3, 0.8))

            log.info("scrolling done, looking for wheel button")

            # Find and click wheel button
            wheel_keywords = "spin|wheel|крут|вращ|pokutaj|girar|rotar|reclamar"
            try:
                wheel_el = driver.execute_script(
                    f"""
                    const keywords = /{wheel_keywords}/i;
                    const exclude = /install|download|get|установ|скачать|получить/i;
                    const buttons = document.querySelectorAll('button, a[role="button"], [onclick], .btn, [class*="button"]');
                    for (const btn of buttons) {{
                        const text = btn.textContent.toLowerCase();
                        if (keywords.test(text) && !exclude.test(text) && btn.offsetHeight > 0) {{
                            return btn;
                        }}
                    }}
                    return null;
                    """
                )

                if wheel_el:
                    log.info("found wheel button, spinning 15 times")
                    for i in range(15):
                        driver.execute_script(
                            "arguments[0].scrollIntoView();arguments[0].click();",
                            wheel_el,
                        )
                        time.sleep(random.uniform(0.2, 0.6))
                    time.sleep(random.uniform(1.5, 2.5))
            except Exception as e:
                log.warning("wheel interaction failed: %s", e)

            # Find and click install button
            install_keywords = (
                "install|get|download|claim|open|setup|obtener|establecer|"
                "получить|установить|скачать|запустить|reclamar|descargar|aceptar"
            )
            try:
                install_el = driver.execute_script(
                    f"""
                    const keywords = /{install_keywords}/i;
                    const buttons = document.querySelectorAll('button, a[role="button"], [onclick], .btn, [class*="button"]');
                    for (const btn of buttons) {{
                        const text = btn.textContent.toLowerCase();
                        if (keywords.test(text) && btn.offsetHeight > 0) {{
                            return btn;
                        }}
                    }}
                    return null;
                    """
                )

                if install_el:
                    log.info("found install button, clicking")
                    driver.execute_script(
                        "arguments[0].scrollIntoView();arguments[0].click();",
                        install_el,
                    )
                    time.sleep(random.uniform(1.5, 2.5))
            except Exception as e:
                log.warning("install button interaction failed: %s", e)

            self._trigger_install_prompt(driver)

        except Exception as e:
            log.warning("auto funnel interaction failed: %s", e)

    def _trigger_install_prompt(self, driver) -> None:
        """Fire the captured beforeinstallprompt from a *trusted* click.

        `event.prompt()` is rejected unless called from a real user gesture, so
        we inject a button whose handler calls it and click it via WebDriver
        (which dispatches trusted input events).
        """
        from selenium.webdriver.common.by import By

        try:
            if not driver.execute_script("return window.__bipFired === true;"):
                log.info("no beforeinstallprompt (funnel installs via its own flow)")
                return
            driver.execute_script(
                """
                if (!document.getElementById('__pwaInstallBtn')) {
                  const b = document.createElement('button');
                  b.id = '__pwaInstallBtn';
                  b.textContent = 'install';
                  b.style.cssText =
                    'position:fixed;left:8px;bottom:8px;width:120px;height:40px;z-index:2147483647';
                  b.addEventListener('click', async () => {
                    try {
                      window.__bipEvent.prompt();
                      const c = await window.__bipEvent.userChoice;
                      window.__bipChoice = c && c.outcome;
                    } catch (e) { window.__bipChoice = 'error:' + e; }
                  });
                  document.body.appendChild(b);
                }
                """
            )
            time.sleep(0.5)
            driver.find_element(By.ID, "__pwaInstallBtn").click()
            log.info("clicked injected install button (trusted gesture)")
            for _ in range(10):
                time.sleep(1)
                choice = driver.execute_script("return window.__bipChoice || null;")
                if choice:
                    log.info("install userChoice=%s", choice)
                    break
            installed = driver.execute_script("return window.__appInstalled === true;")
            log.info("appinstalled=%s", installed)
        except Exception as e:  # noqa: BLE001
            log.warning("install prompt trigger failed: %s", e)

    async def enable_push_collection(self, session_id: str) -> PushInfo:
        """Persist the session and start collecting pushes (stage: install)."""
        sess = self._sessions.get(session_id)
        if not sess:
            raise RuntimeError(
                "Сессия не активна (браузер закрыт / истекла). Просканируй заново."
            )
        if sess.get("collecting"):
            return PushInfo(
                sess["expires_at"], sess["stage"], sess["pwa_name"],
                sess["start_url"], sess.get("deep_link"),
            )

        driver = sess["driver"]

        # Retry unless we already have a FUNNEL-made subscription (a self-made
        # one is never registered with the funnel's backend, so it gets no
        # pushes — keep trying for a real one).
        if sess.get("push_by") != "funnel":
            sub = await asyncio.to_thread(
                self._subscribe_push, driver, sess["start_url"], 30000, True
            )
            if sub["subscribed"]:
                sess.update(push_subscribed=True, push_endpoint=sub.get("endpoint"),
                            push_by="funnel")
            else:
                launch = await asyncio.to_thread(self._launch_pwa, driver, sess["start_url"])
                if launch["push_subscribed"]:
                    sess.update(push_subscribed=True,
                                push_endpoint=launch.get("push_endpoint"),
                                push_by=launch.get("push_by"))

        expires_at = time.time() + self.s.collect_seconds
        sess.update(collecting=True, stage=STAGE_INSTALL, expires_at=expires_at)

        await self.db.create_session(
            {
                "id": session_id,
                "user_id": sess["user_id"],
                "chat_id": sess["chat_id"],
                "proxy": json.dumps(sess["proxy"]) if sess.get("proxy") else None,
                "site_url": sess["site_url"],
                "pwa_name": sess["pwa_name"],
                "start_url": sess["start_url"],
                "scope": sess["scope"],
                "deep_link": sess.get("deep_link"),
                "stage": STAGE_INSTALL,
                "push_subscribed": 1 if sess.get("push_subscribed") else 0,
                "push_endpoint": sess.get("push_endpoint"),
                "profile_dir": sess["profile_dir"],
                "status": "collecting",
                "created_at": sess.get("scanned_at", time.time()),
                "expires_at": expires_at,
                "delivered_at": None,
            }
        )

        await asyncio.to_thread(self._start_observer, session_id, sess)

        # a real (funnel-made) subscription -> the scan proxy is no longer needed
        if sess.get("push_by") == "funnel":
            await self._swap_to_hold(sess)

        return PushInfo(
            expires_at, STAGE_INSTALL, sess["pwa_name"],
            sess["start_url"], sess.get("deep_link"),
            sess.get("push_subscribed", False), sess.get("push_endpoint"),
        )

    def _hold_upstream(self, sess: dict) -> str | None:
        proxy = sess.get("proxy") or {}
        hold = proxy.get("hold") or self.s.hold_proxy
        if not hold:
            return None
        if hold.strip().lower() == "direct":
            return "direct"  # proxyless — pushes arrive over the FCM connection
        return pproxy_upstream({"server": hold})

    async def _swap_to_hold(self, sess: dict) -> None:
        if sess.get("on_hold") or not sess.get("local_proxy"):
            return
        up = self._hold_upstream(sess)
        if not up:
            return
        try:
            await sess["local_proxy"].swap(up)
            sess["on_hold"] = True
            log.info("session %s moved to hold proxy", sess["id"][:8])
        except Exception as e:  # noqa: BLE001
            log.warning("swap to hold proxy failed: %s", e)

    def _start_observer(self, session_id: str, sess: dict) -> None:
        from app.services.cdp_bridge import CdpBridge

        try:
            _, ws_url = sess["driver"]._get_cdp_details()
        except Exception as e:  # noqa: BLE001
            log.warning("cannot get CDP ws url for bridge: %s", e)
            return

        def on_event(ev: dict) -> None:
            ev["stage"] = sess.get("stage")
            sess["push_queue"].append(ev)
            log.info(
                "push [%s/%s] %s: %s",
                session_id[:8], ev.get("stage"), ev.get("event"), ev.get("title"),
            )

        bridge = CdpBridge(ws_url, on_event)
        bridge.start()
        sess["observer"] = bridge
        if self.webcontrol:
            try:
                self.webcontrol.register(
                    session_id, sess["pwa_name"], bridge,
                    on_view=lambda a, s=sess: s.__setitem__("ctl_active", a),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("webcontrol register failed: %s", e)

    async def set_stage(self, session_id: str, stage: str) -> None:
        sess = self._sessions.get(session_id)
        if not sess:
            raise RuntimeError("Сессия не активна")
        if stage not in STAGE_ORDER:
            raise RuntimeError("Неизвестная стадия")
        sess["stage"] = stage
        await self.db.set_session_fields(session_id, stage=stage)
        # drop a marker into the push stream so the pack shows the transition
        sess["push_queue"].append(
            {
                "ts": time.time(),
                "stage": stage,
                "service": "stage",
                "event": "stage_change",
                "title": f"— стадия: {STAGE_LABEL.get(stage, stage)} —",
                "body": None,
                "icon": None,
                "url": None,
                "raw": None,
            }
        )
        await self.flush_pushes()

    async def flush_pushes(self) -> None:
        """Drain queued push events from every session into the DB."""
        for sid, sess in list(self._sessions.items()):
            q = sess.get("push_queue")
            if not q:
                continue
            batch, sess["push_queue"] = q, []
            for rec in batch:
                try:
                    await self.db.add_push(sid, rec)
                except Exception as e:  # noqa: BLE001
                    log.warning("add_push failed: %s", e)

    async def retry_subscriptions(self) -> None:
        """Keep retrying the standalone PWA launch until the FUNNEL itself
        subscribes (only then is the endpoint registered with its backend and
        pushes actually arrive). Then move the session to the hold proxy."""
        now = time.time()
        for sid, sess in list(self._sessions.items()):
            if not sess.get("collecting") or sess.get("stage") != STAGE_INSTALL:
                continue
            aged_out = now - sess.get("scanned_at", now) > 1800
            if sess.get("push_by") == "funnel" or aged_out:
                # nothing more to try — release the scan proxy
                await self._swap_to_hold(sess)
                continue
            if sess.get("ctl_active"):
                continue
            try:
                launch = await asyncio.to_thread(
                    self._launch_pwa, sess["driver"], sess["start_url"]
                )
            except Exception as e:  # noqa: BLE001
                log.warning("retry_subscriptions %s: %s", sid[:8], e)
                continue
            if launch.get("push_subscribed"):
                was = sess.get("push_by")
                sess["push_subscribed"] = True
                sess["push_endpoint"] = launch.get("push_endpoint")
                sess["push_by"] = launch.get("push_by")
                await self.db.set_session_fields(
                    sid, push_subscribed=1, push_endpoint=launch.get("push_endpoint")
                )
                log.info("retry_subscriptions: %s subscribed (%s)",
                         sid[:8], launch.get("push_by"))
                if was == launch.get("push_by"):
                    continue  # already told the user about this one
                try:
                    await self.bot.send_message(
                        sess["chat_id"],
                        f"🔔 Push-подписка создана — <b>{esc(sess['pwa_name'])}</b>. "
                        "Пуши будут собираться.",
                    )
                except Exception:
                    pass
                await self._swap_to_hold(sess)

    # ---------- interactive browser access ----------

    # CSS viewport we emulate (see _apply_stealth device metrics)
    _VIEW_W, _VIEW_H = 393, 852

    async def screenshot(self, session_id: str) -> bytes:
        sess = self._require(session_id)
        return await asyncio.to_thread(sess["driver"].get_screenshot_as_png)

    async def tap(self, session_id: str, px: float, py: float) -> None:
        """px/py are percentages (0..100) of the viewport width/height."""
        sess = self._require(session_id)
        cx = max(0.0, min(100.0, px)) / 100 * self._VIEW_W
        cy = max(0.0, min(100.0, py)) / 100 * self._VIEW_H

        def _do():
            d = sess["driver"]
            d.execute_cdp_cmd("Input.dispatchMouseEvent",
                              {"type": "mouseMoved", "x": cx, "y": cy})
            for etype in ("mousePressed", "mouseReleased"):
                d.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {"type": etype, "x": cx, "y": cy, "button": "left", "clickCount": 1},
                )

        await asyncio.to_thread(_do)

    async def type_text(self, session_id: str, text: str) -> None:
        sess = self._require(session_id)
        await asyncio.to_thread(
            sess["driver"].execute_cdp_cmd, "Input.insertText", {"text": text}
        )

    async def press_key(self, session_id: str, key: str = "Enter") -> None:
        sess = self._require(session_id)
        code = {"Enter": ("Enter", 13), "Backspace": ("Backspace", 8), "Tab": ("Tab", 9)}
        k, kc = code.get(key, (key, 0))

        def _do():
            d = sess["driver"]
            for etype in ("keyDown", "keyUp"):
                d.execute_cdp_cmd(
                    "Input.dispatchKeyEvent",
                    {"type": etype, "key": k, "windowsVirtualKeyCode": kc,
                     "text": "\r" if k == "Enter" else ""},
                )

        await asyncio.to_thread(_do)

    async def go_back(self, session_id: str) -> None:
        sess = self._require(session_id)
        await asyncio.to_thread(sess["driver"].back)

    async def open_url(self, session_id: str, url: str) -> None:
        sess = self._require(session_id)
        await asyncio.to_thread(sess["driver"].get, url)

    async def scroll(self, session_id: str, dy: int) -> None:
        sess = self._require(session_id)
        await asyncio.to_thread(
            sess["driver"].execute_script, f"window.scrollBy(0,{int(dy)})"
        )

    def _require(self, session_id: str) -> dict:
        sess = self._sessions.get(session_id)
        if not sess:
            raise RuntimeError("Сессия не активна (браузер закрыт)")
        return sess

    async def finalize_expired(self) -> None:
        """Finalize and deliver expired sessions."""
        for row in await self.db.list_due(time.time()):
            try:
                await self.deliver(row["id"])
            except Exception as e:
                log.exception("deliver failed: %s", e)

    async def _send_pack(self, session_id: str, final: bool) -> bool:
        """Build + send the pushes archive. Returns True on success."""
        row = await self.db.get_session(session_id)
        if not row:
            return False
        await self.flush_pushes()
        pushes = await self.db.list_pushes(session_id)
        pack_path = build_pack(row, pushes, self.s.sessions_dir)
        try:
            await self.bot.send_document(
                row["chat_id"], FSInputFile(pack_path),
                caption=pack_caption(row, pushes, final=final),
            )
            return True
        except Exception as e:  # noqa: BLE001
            log.exception("send_document failed: %s", e)
            return False

    async def export_pack(self, session_id: str) -> None:
        """Send the current archive WITHOUT stopping collection."""
        await self._send_pack(session_id, final=False)

    async def deliver(self, session_id: str) -> None:
        """Final delivery: send the archive, mark delivered, close the session."""
        row = await self.db.get_session(session_id)
        if not row or row["status"] == "delivered":
            return
        await self._send_pack(session_id, final=True)
        await self.db.set_session_fields(
            session_id, status="delivered", delivered_at=time.time()
        )
        await self._teardown_session(self._sessions.pop(session_id, None))

    async def cancel(self, session_id: str) -> None:
        """Cancel a session (also works for scanned-but-not-persisted sessions)."""
        await self._teardown_session(self._sessions.pop(session_id, None))
        if await self.db.get_session(session_id):
            await self.db.set_session_fields(session_id, status="cancelled")

    async def _teardown_session(self, session_data: dict | None) -> None:
        if not session_data:
            return
        if self.webcontrol and session_data.get("id"):
            try:
                self.webcontrol.unregister(session_data["id"])
            except Exception:
                pass
        obs = session_data.get("observer")
        if obs:
            try:
                obs.stop()
            except Exception:
                pass
        driver = session_data.get("driver")
        if driver:
            try:
                await asyncio.to_thread(driver.quit)
            except Exception:
                pass
        local_proxy = session_data.get("local_proxy")
        if local_proxy:
            try:
                await local_proxy.stop()
            except Exception:
                pass

    async def sweep_stale(self, max_age: float = 1800.0) -> None:
        """Close scanned sessions that were never turned into collection."""
        now = time.time()
        for sid, sess in list(self._sessions.items()):
            if sess.get("collecting"):
                continue
            if now - sess.get("scanned_at", now) > max_age:
                log.info("sweeping stale scanned session %s", sid[:8])
                await self._teardown_session(self._sessions.pop(sid, None))

    async def restore(self) -> None:
        """Restore active sessions after restart (skipped for undetected-chromedriver)."""
        log.info("restore skipped (undetected-chromedriver sessions)")
