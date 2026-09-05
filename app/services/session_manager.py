from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import random
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

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
        self._loop: asyncio.AbstractEventLoop | None = None
        os.makedirs(self.s.sessions_dir, exist_ok=True)

    def control_url(self, session_id: str) -> str | None:
        if self.webcontrol:
            return self.webcontrol.url_for(session_id)
        return None

    async def start(self) -> None:
        """Initialize session manager (no-op for undetected-chromedriver)."""
        self._loop = asyncio.get_running_loop()
        log.info(
            "env: DISPLAY=%r DBUS_SESSION_BUS_ADDRESS=%r HEADLESS=%s",
            os.environ.get("DISPLAY"),
            os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
            self.s.headless,
        )
        # if the entrypoint's D-Bus / display didn't propagate, at least make
        # sure a display is set so headful Chrome + dunst agree
        if not self.s.headless and not os.environ.get("DISPLAY"):
            os.environ["DISPLAY"] = ":99"

    async def stop(self) -> None:
        """Close all active sessions."""
        for session_data in list(self._sessions.values()):
            await self._teardown_session(session_data)
        self._sessions.clear()

    # Android device we emulate. PWA funnels (nutra/gambling, mobile-geo offers)
    # hard-403 desktop traffic, so we must look like a real mobile Chrome -
    # including consistent Sec-CH-UA client hints, which Chrome's native mobile
    # emulation produces and a bare UA override does not.
    # The real device (version + model) lives in the client-hint metadata; the
    # UA STRING must be the frozen "Android 10; K" form Chrome 110+ ships since
    # User-Agent Reduction — funnels reject a UA that still carries a real
    # Android version/model as a spoof (verified: newlifejoker.club bails on
    # push subscription when the UA is not "Android 10; K").
    _ANDROID_MODEL = "Pixel 7"
    _ANDROID_VERSION = "14.0.0"

    def _mobile_ua(self, chrome_major: str) -> str:
        return (
            "Mozilla/5.0 (Linux; Android 10; K) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_major}.0.0.0 Mobile Safari/537.36"
        )

    def _setup_undetected_driver(
        self,
        profile_dir: str,
        proxy_url: str | None = None,
        geo: dict | None = None,
        app_url: str | None = None,
    ):
        """Launch undetected-chromedriver emulating a real Android Chrome.

        app_url: open in Chrome "app window" mode — a REAL display-mode:
        standalone context (not the JS spoof), i.e. what an installed PWA
        launched from the home screen gets. Some funnels only arm their
        push subscription in that context.
        """
        import undetected_chromedriver as uc

        # chromedriver does NOT reliably propagate the launcher's env to the
        # Chrome process (verified: main browser had no DISPLAY / DBUS). Set
        # them here so Chrome can reach Xvfb + the notification daemon —
        # without a displayable notification Chrome revokes userVisibleOnly
        # push subscriptions after the first push.
        if not self.s.headless:
            os.environ.setdefault("DISPLAY", ":99")
            if os.path.exists("/tmp/dbus-session"):
                os.environ["DBUS_SESSION_BUS_ADDRESS"] = (
                    "unix:path=/tmp/dbus-session")

        chrome_options = uc.ChromeOptions()
        chrome_options.user_data_dir = profile_dir
        if not self.s.headless:
            chrome_options.add_argument(
                f"--display={os.environ.get('DISPLAY', ':99')}")
        if app_url:
            chrome_options.add_argument(f"--app={app_url}")

        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_argument(
            "--force-webrtc-ip-handling-policy=default_public_interface_only"
        )
        # route web notifications to the system (D-Bus / dunst) server, not
        # Chrome's own message-center UI which never renders under Xvfb — if a
        # push results in no visible notification Chrome revokes the
        # userVisibleOnly subscription ("Unsubscribed due to error").
        chrome_options.add_argument(
            "--enable-features=SystemNotifications,NativeNotifications")
        chrome_options.add_argument("--disable-features=WebRtcHideLocalIpsWithMdns")
        # keep the footprint small — sessions hold a live Chrome for 7 days.
        # NB: do NOT disable background networking — FCM push rides on it.
        chrome_options.add_argument("--renderer-process-limit=2")
        chrome_options.add_argument("--js-flags=--max-old-space-size=192")
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-sync")
        chrome_options.add_argument("--disable-breakpad")
        chrome_options.add_argument("--no-first-run")

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
            "prefs", {
                "intl.accept_languages": tags,
                # grant web-notification permission to every origin. The push
                # DELIVERY check reads the real content setting (not the
                # Browser.grantPermissions override, and not the spoofed
                # Notification.permission) — without this Chrome revokes the
                # subscription on the first push: Reason DELIVERY_PERMISSION_DENIED.
                "profile.default_content_setting_values.notifications": 1,
                "profile.managed_default_content_settings.notifications": 1,
            }
        )

        # Mobile viewport. The real Android UA + consistent Sec-CH-UA client
        # hints are applied via CDP in _apply_stealth (needs the runtime Chrome
        # version, so it can't be done here at options time).
        chrome_options.add_argument("--window-size=393,852")

        driver = uc.Chrome(options=chrome_options, version_main=None)
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(20)
        log.info(
            "launched uc (headless=%s, lang=%s, DISPLAY=%s dbus=%s)",
            self.s.headless, accept_lang, os.environ.get("DISPLAY"),
            bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS")),
        )
        # confirm the Chrome process actually got the notification env
        try:
            import glob as _glob
            for cl in _glob.glob("/proc/*/cmdline"):
                data = open(cl, "rb").read()
                if b"--user-data-dir" not in data or b"--type=" in data:
                    continue
                pid = cl.split("/")[2]
                env = open(f"/proc/{pid}/environ", "rb").read().decode(
                    "utf-8", "replace")
                log.info("chrome pid=%s DISPLAY=%s DBUS=%s", pid,
                         "DISPLAY=" in env,
                         "DBUS_SESSION_BUS_ADDRESS=" in env)
                break
        except Exception as e:  # noqa: BLE001
            log.debug("chrome env check skipped: %s", e)
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

                    manifest, murl = self._read_manifest(driver, budget_ms=7000)

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
                # Carry the original click params into the launch so the funnel
                # fills them into the deep link ({sub10}/{sub9}/click_id/...).
                orig_q = urlsplit(url).query
                if orig_q and not urlsplit(start_url).query:
                    s = urlsplit(start_url)
                    start_url = urlunsplit(
                        (s.scheme, s.netloc, s.path, orig_q, ""))
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

    async def dump_js(self, chat_id: int, proxy: dict | None, url: str) -> None:
        """Load url past the cloaker, fetch every same-origin script bundle +
        the service worker (through the proxy / browser), send them as files."""
        profile_dir = str(
            (Path(self.s.sessions_dir) / ("_js_" + uuid.uuid4().hex)).resolve()
        )
        os.makedirs(profile_dir, exist_ok=True)
        local_proxy = driver = None
        sent = 0
        try:
            proxy_url = None
            if proxy:
                local_proxy = LocalProxy(pproxy_upstream(proxy))
                proxy_url = await local_proxy.start()
            geo = None
            if proxy_url:
                geo = await asyncio.to_thread(self._probe_geo_http, proxy_url)
            driver = await asyncio.to_thread(
                self._setup_undetected_driver, profile_dir, proxy_url, geo)

            def work() -> dict:
                self._apply_stealth(driver, geo)
                m = {}
                for _ in range(4):
                    try:
                        driver.get(url)
                    except TimeoutException:
                        pass
                    time.sleep(3)
                    m, _mu = self._read_manifest(driver, budget_ms=6000)
                    if m:
                        break
                    try:
                        driver.delete_all_cookies()
                    except Exception:
                        pass
                start_url = (m or {}).get("start_url") or url
                return driver.execute_async_script(r"""
                  const cb = arguments[arguments.length - 1];
                  const startUrl = arguments[0];
                  const org = location.origin;
                  const urls = new Set(['/push/vapp/VappWorker.js',
                    '/PwaWorker.js', '/revisionMap.json', '/service-worker.js',
                    '/service-worker-fcm.js', '/service-worker-os.js',
                    '/landing-static/init-work.min.js']);
                  for (const s of document.scripts)
                    if (s.src && s.src.indexOf(org) === 0) urls.add(s.src);
                  (async () => {
                    const out = {};
                    try {
                      const reg = await navigator.serviceWorker.getRegistration();
                      const u = reg && (reg.active||reg.installing||reg.waiting);
                      if (u && u.scriptURL) urls.add(u.scriptURL);
                    } catch (e) {}
                    // grab the raw HTML of the funnel root + the pwa_ start_url
                    for (const [k, u] of [['__html_root__', org + '/'],
                                          ['__html_start__', startUrl]]) {
                      try {
                        const t = await fetch(u, {credentials:'include'})
                          .then(r => r.text());
                        out[k] = t;
                        // pull assets/*.js and /push/*.js refs out of the HTML
                        (t.match(/[\w./-]*assets\/[\w.-]+\.js/g) || [])
                          .forEach(x => urls.add(new URL(x, org).href));
                      } catch (e) { out[k] = '/* ' + e + ' */'; }
                    }
                    // walk one level of JS to find more chunk names
                    for (const u of [...urls]) {
                      if (!/\.js($|\?)/.test(u)) continue;
                      try {
                        const t = await fetch(u).then(r => r.text());
                        out[u] = t;
                        (t.match(/assets\/[\w.-]+\.js/g) || [])
                          .forEach(x => urls.add(new URL('/' + x, org).href));
                        (t.match(/\/push\/[\w./-]+\.js/g) || [])
                          .forEach(x => urls.add(new URL(x, org).href));
                      } catch (e) { out[u] = '/* ' + e + ' */'; }
                    }
                    for (const u of urls) {
                      if (u in out) continue;
                      try { out[u] = await fetch(u).then(r => r.text()); }
                      catch (e) { out[u] = '/* ' + e + ' */'; }
                    }
                    out['__has_va_app_id__'] = JSON.stringify({
                      root: /va_app_id/.test(out['__html_root__'] || ''),
                      start: /va_app_id/.test(out['__html_start__'] || ''),
                    });
                    cb(out);
                  })();
                """, start_url) or {}

            files = await asyncio.to_thread(work)
            for u, text in files.items():
                name = re.sub(r"[^\w.-]", "_", u.split("/")[-1] or "file")[:60]
                if not name.endswith((".js", ".json", ".txt")):
                    name += ".txt"
                p = Path(profile_dir) / f"{sent:02d}_{name}"
                p.write_text(text or "", encoding="utf-8")
                await self.bot.send_document(
                    chat_id, FSInputFile(str(p)),
                    caption=u[:1000])
                sent += 1
            if not sent:
                await self.bot.send_message(chat_id, "Скриптов не найдено")
        except Exception as e:  # noqa: BLE001
            await self.bot.send_message(chat_id, f"dump_js: {esc(str(e))}")
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

    async def probe_offer(self, chat_id: int, proxy: dict | None, url: str) -> None:
        """Full diagnostic dump for one offer URL — what the proxy exit looks
        like, what the cloaker returns over N loads (HTTP + browser), and what
        the standalone launch resolves. Sends a report + screenshot to chat_id.
        For comparing a funnel that passes against one that does not."""
        profile_dir = str(
            (Path(self.s.sessions_dir) / ("_probe_" + uuid.uuid4().hex)).resolve()
        )
        os.makedirs(profile_dir, exist_ok=True)
        local_proxy = driver = None
        lines: list[str] = [f"🔬 <b>Probe</b> {esc(url)}"]
        shot = str(Path(profile_dir, "probe.png").resolve())

        def _http_probe(proxy_url: str | None) -> str:
            import requests
            px = {"http": proxy_url, "https": proxy_url} if proxy_url else None
            try:
                r = requests.get(url, proxies=px, timeout=30,
                                 allow_redirects=True)
            except Exception as e:  # noqa: BLE001
                return f"HTTP: ошибка {e}"
            h = r.headers
            m = re.search(r"<title[^>]*>(.*?)</title>", r.text or "",
                          re.I | re.S)
            has_mani = bool(re.search(
                r'rel=["\']?[^"\'>]*manifest', r.text or "", re.I))
            cookies = ",".join(c.name for c in r.cookies) or "—"
            return (
                f"HTTP: {r.status_code} → {esc(r.url)}\n"
                f"  server={esc(h.get('server','—'))} "
                f"cf-ray={esc(h.get('cf-ray','—'))} "
                f"cf-mitigated={esc(h.get('cf-mitigated','—'))}\n"
                f"  content-type={esc(h.get('content-type','—'))} "
                f"len={len(r.text or '')} manifest={has_mani}\n"
                f"  set-cookie={esc(cookies)}\n"
                f"  title-tag={esc((m.group(1).strip() if m else '')[:80])}"
            )

        try:
            proxy_url = None
            if proxy:
                local_proxy = LocalProxy(pproxy_upstream(proxy))
                proxy_url = await local_proxy.start()

            geo = None
            if proxy_url:
                geo = await asyncio.to_thread(self._probe_geo_http, proxy_url)
            g = geo or {}
            lines.append(
                f"\n<b>Прокси</b>: {esc(proxy.get('name') if proxy else 'без прокси')}\n"
                f"  exit ip={esc(g.get('ip') or '?')} "
                f"cc={esc(g.get('country_code') or '?')} "
                f"isp={esc(g.get('isp') or '?')}\n"
                f"  mobile={g.get('mobile')} hosting={g.get('hosting')} "
                f"proxy={g.get('proxy')} tz={esc(g.get('timezone') or '?')}"
            )

            lines.append("\n<b>HTTP-проба</b> (без браузера):")
            lines.append(await asyncio.to_thread(_http_probe, proxy_url))

            driver = await asyncio.to_thread(
                self._setup_undetected_driver, profile_dir, proxy_url, geo
            )

            def work() -> None:
                self._apply_stealth(driver, geo)
                lines.append("\n<b>Браузер</b> (до 3 загрузок):")
                for attempt in range(1, 4):
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
                    cur = title = body = ""
                    try:
                        cur = driver.current_url or ""
                    except Exception:
                        pass
                    try:
                        title = driver.title or ""
                    except Exception:
                        pass
                    try:
                        body = driver.execute_script(
                            "return document.body?document.body.innerText:''") or ""
                    except Exception:
                        pass
                    mani, murl = self._read_manifest(driver, budget_ms=7000)
                    blocked = self._looks_blocked(title, body)
                    lines.append(
                        f"  [{attempt}] url={esc(cur)}\n"
                        f"      title={esc(title[:70])} bodyLen={len(body)} "
                        f"blocked={blocked}\n"
                        f"      manifest={'да' if mani else 'нет'}"
                        f" murl={esc(str(murl))}"
                        + (f"\n      name={esc(str(mani.get('name')))} "
                           f"start_url={esc(str(mani.get('start_url')))}"
                           if mani else "")
                    )
                    if mani and not blocked:
                        base = cur if cur.startswith("http") else url
                        start_url = urljoin(
                            murl or base, mani.get("start_url") or "/")
                        try:
                            self._auto_funnel_interaction_sync(driver)
                        except Exception as e:  # noqa: BLE001
                            lines.append(f"  interaction: {esc(str(e))}")
                        launch = self._launch_pwa(
                            driver, start_url, link_only=True, shot_path=shot)
                        lines.append(
                            f"\n<b>Launch</b>: start_url={esc(start_url)}\n"
                            f"  deep_link={esc(launch['deep_link'])}\n"
                            f"  {'✅ редирект пойман (скриншот — страница deep-link)' if launch['deep_link'] != start_url else '⚠️ редиректа нет — deep_link = start_url'}"
                        )
                        return
                lines.append("\n⚠️ Ни одна загрузка не дала воронку с manifest — "
                             "клоака отдаёт заглушку")
                try:
                    driver.save_screenshot(shot)
                except Exception:
                    pass

            await asyncio.to_thread(work)
        except Exception as e:  # noqa: BLE001
            lines.append(f"\n❌ Ошибка: {esc(str(e))}")
        finally:
            if driver:
                try:
                    await asyncio.to_thread(driver.quit)
                except Exception:
                    pass
            if local_proxy:
                await local_proxy.stop()

        text = "\n".join(lines)
        for i in range(0, len(text), 3900):
            chunk = text[i:i + 3900]
            try:
                await self.bot.send_message(chat_id, chunk,
                                            disable_web_page_preview=True)
            except Exception:  # noqa: BLE001
                # HTML parse failed somewhere in the dump — send it raw
                import re as _re
                await self.bot.send_message(
                    chat_id, _re.sub(r"<[^>]+>", "", chunk),
                    parse_mode=None, disable_web_page_preview=True,
                )
        try:
            if Path(shot).exists():
                await self.bot.send_photo(chat_id, FSInputFile(shot),
                                          caption="landing / launch")
        except Exception as e:  # noqa: BLE001
            log.warning("probe screenshot send failed: %s", e)
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
                        "(geo-проба не прошла) — проверь прокси"
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
                "scan_upstream": pproxy_upstream(proxy) if proxy else None,
                "net": "proxy" if proxy else "direct",
                "user_id": user_id,
                "chat_id": chat_id,
                "proxy": proxy,
                "geo": geo,
                "site_url": url,
                "pwa_name": name,
                "start_url": start_url,
                "scope": scope,
                "deep_link": deep_link,
                "nav_chain": info.get("nav_chain") or [],
                "profile_dir": profile_dir,
                "scanned_at": time.time(),
                "collecting": False,
                "stage": None,
                "push_queue": [],
                "observer": None,
                "push_subscribed": info.get("push_subscribed", False),
                "push_endpoint": info.get("push_endpoint"),
                "push_by": info.get("push_by"),
                "_drv_lock": asyncio.Lock(),
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
      // make a spoofed getter/function read back as native code, so a cloaker
      // probing `fn.toString()` / descriptor.get.toString() can't tell.
      const nativeStr = (name) => 'function ' + name + '() { [native code] }';
      const mask = (obj, name, fn) => {
        try {
          Object.defineProperty(fn, 'toString', {
            value: () => nativeStr(name), writable: true, configurable: true,
          });
        } catch (e) {}
        return fn;
      };
      const defGet = (obj, prop, val, name) => {
        try {
          const g = mask(obj, name || ('get ' + prop), () => val);
          Object.defineProperty(obj, prop, {get: g, configurable: true});
        } catch (e) {}
      };

      defGet(navigator, 'webdriver', false, 'get webdriver');
      // real Android reports a touchscreen + a real core/RAM count
      defGet(navigator, 'maxTouchPoints', 5, 'get maxTouchPoints');
      defGet(navigator, 'hardwareConcurrency', 8, 'get hardwareConcurrency');
      defGet(navigator, 'deviceMemory', 8, 'get deviceMemory');
      try {
        defGet(navigator, 'connection', {
          effectiveType: '4g', rtt: 100, downlink: 10,
          saveData: false, type: 'cellular',
        }, 'get connection');
      } catch (e) {}

      // WebGL: headless Chrome renders through SwiftShader — a dead giveaway.
      // Report the Pixel 7's GPU (ARM Mali-G710) instead.
      try {
        const GL_VENDOR = 0x9245, GL_RENDERER = 0x9246;
        const spoof = {
          [GL_VENDOR]: 'ARM',
          [GL_RENDERER]: 'ANGLE (ARM, Mali-G710, OpenGL ES 3.2)',
        };
        for (const proto of [
          window.WebGLRenderingContext && WebGLRenderingContext.prototype,
          window.WebGL2RenderingContext && WebGL2RenderingContext.prototype,
        ]) {
          if (!proto) continue;
          const orig = proto.getParameter;
          proto.getParameter = mask(proto, 'getParameter', function (p) {
            if (p in spoof) return spoof[p];
            return orig.call(this, p);
          });
        }
      } catch (e) {}

      try {
        if (!window.chrome) window.chrome = {};
        if (!window.chrome.runtime) window.chrome.runtime = {};
        if (!window.chrome.app) window.chrome.app = {
          isInstalled: false, InstallState: {}, RunningState: {},
        };
        if (!window.chrome.csi) window.chrome.csi = function () { return {}; };
        if (!window.chrome.loadTimes) window.chrome.loadTimes =
          function () { return {}; };
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
            const p = _sub.apply(this, arguments);
            // capture the endpoint the FUNNEL got, even if the page then
            // redirects away before we can poll getSubscription().
            try {
              window.__subCalled = (window.__subCalled || 0) + 1;
              Promise.resolve(p).then((s) => {
                try {
                  if (s && s.endpoint) {
                    window.__pushEndpoint = s.endpoint;
                    localStorage.setItem('__pushEndpoint', s.endpoint);
                  }
                } catch (e) {}
              }).catch(() => {});
            } catch (e) {}
            return p;
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

    # Poll for <link rel=manifest> (SPAs inject it late), then fetch it FROM THE
    # PAGE so it goes through the proxy + browser session + cookies. A serverside
    # requests.get() hits the cloaked origin from the datacenter IP and gets a
    # 400/403 — which made real funnels look manifest-less (= decoy).
    _MANIFEST_JS = r"""
    const budget = arguments[0], cb = arguments[arguments.length - 1];
    const t0 = Date.now();
    (function poll() {
      const l = document.querySelector('link[rel~="manifest"]');
      if (l && l.href) {
        fetch(l.href, {credentials: 'include'})
          .then(r => r.text())
          .then(txt => cb(JSON.stringify({href: l.href, body: txt})))
          .catch(e => cb(JSON.stringify({href: l.href, body: null})));
        return;
      }
      if (Date.now() - t0 > budget) { cb(JSON.stringify({href: null})); return; }
      setTimeout(poll, 400);
    })();
    """

    def _read_manifest(self, driver, budget_ms: int = 8000):
        """Return (manifest_dict, manifest_url). {} if none / unreadable."""
        try:
            driver.set_script_timeout(budget_ms / 1000 + 10)
            raw = driver.execute_async_script(self._MANIFEST_JS, budget_ms) or "{}"
        except Exception as e:  # noqa: BLE001
            log.warning("manifest probe failed: %s", e)
            return {}, None
        finally:
            try:
                driver.set_script_timeout(20)
            except Exception:
                pass
        try:
            d = json.loads(raw)
        except Exception:
            return {}, None
        murl = d.get("href")
        body = d.get("body")
        if murl and not body:
            # page fetch was blocked/opaque — last-ditch serverside try
            try:
                import requests

                body = requests.get(murl, timeout=12).text
            except Exception:
                body = None
        if not body:
            return {}, murl
        try:
            m = json.loads(body)
        except Exception:
            return {}, murl
        if not isinstance(m, dict):
            return {}, murl
        if m.get("start_url"):
            m["start_url"] = urljoin(murl, m["start_url"])
        if m.get("scope"):
            m["scope"] = urljoin(murl, m["scope"])
        return m, murl

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

        # The cloaker's verdict flip-flops (real funnel <-> white page / shell).
        # Only the real funnel ships a manifest — reload until we get one.
        _m, _mu = self._read_manifest(driver, budget_ms=6000)
        for _try in range(2, 6):
            if _m:
                break
            _bt = ""
            try:
                _bt = driver.execute_script(
                    "return document.body?document.body.innerText:''") or ""
            except Exception:
                pass
            if self._looks_blocked(driver.title or "", _bt):
                break
            log.info("navigate: no manifest (try %d) — reloading past cloaker", _try - 1)
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
            _m, _mu = self._read_manifest(driver, budget_ms=6000)

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

        # Manifest — fetched from the page (proxy + cookies), not serverside
        manifest, manifest_url = self._read_manifest(driver, budget_ms=8000)
        if not manifest and _m:
            manifest, manifest_url = _m, _mu
        if manifest:
            log.info("manifest read: name=%s scope=%s start_url=%s",
                     manifest.get("name"), manifest.get("scope"),
                     manifest.get("start_url"))
        else:
            log.warning("manifest read failed (url=%s)", manifest_url)

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

        # No manifest => not the real funnel. Either an empty shell, or the
        # cloaker's white-label "review" page (long body, app-store links).
        # Don't burn ~5 min on push-subscription retries against a decoy.
        _store = bool(re.search(
            r"play\.google\.com/store|apps\.apple\.com", page_source))
        shell = not manifest and (
            len((page_text or "").strip()) < 20 or _store
        )
        early = {"subscribed": False, "endpoint": None, "sw": False}
        push_subscribed = push_endpoint = None
        push_by = None
        if shell:
            log.warning(
                "no manifest (title=%r, store_links=%s) — cloaker decoy / white "
                "page; skipping push subscription", page_title, _store,
            )
            launch = self._launch_pwa(driver, start_url, link_only=True)
        else:
            # This whole funnel family (yap-games / mainggames / newlifejoker /
            # nequ) ships <meta va_app_public_key> on the pwa_ page and a push
            # worker at /push/vapp/VappWorker.js. Subscribe THAT way first — it
            # registers a push-capable SW and POSTs to /subscribe. The funnel's
            # own in-page subscribe often lands on a push-less SW → Chrome kills
            # it after the first push ("Unsubscribed due to error").
            va = self._vapp_subscribe(driver, start_url)
            if va.get("endpoint"):
                push_subscribed = True
                push_endpoint = va["endpoint"]
                push_by = "funnel"
                launch = self._launch_pwa(driver, start_url, link_only=True)
            else:
                early = self._subscribe_push(driver, url, budget_ms=8000)
                launch = self._launch_pwa(driver, start_url)

        deep_link = launch["deep_link"]
        nav_chain = launch.get("chain") or []
        push_subscribed = push_subscribed or early["subscribed"] or launch["push_subscribed"]
        push_endpoint = push_endpoint or early["endpoint"] or launch["push_endpoint"]
        push_by = push_by or ("funnel" if early["subscribed"] else launch.get("push_by"))
        installable = bool(early["sw"]) or push_subscribed

        # The push prompt is shown by an intermediate "PWA service" domain
        # (e.g. yapegamenew.club) that sits between the funnel and the casino
        # and flashes past too fast on launch. Revisit it and let it subscribe.
        if not push_subscribed and len(nav_chain) > 1:
            mid = self._subscribe_on_intermediate(driver, start_url, nav_chain)
            if mid.get("endpoint"):
                push_subscribed = True
                push_endpoint = mid["endpoint"]
                push_by = "funnel"

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
            "nav_chain": nav_chain,
            "push_subscribed": push_subscribed,
            "push_endpoint": push_endpoint,
            "push_by": push_by,
            "shell": shell,
        }

    _FREEZE_JS = r"""
    (() => {
      try {
        const L = window.location;
        try { L.assign = () => {}; } catch (e) {}
        try { L.replace = () => {}; } catch (e) {}
        try {
          const p = Object.getPrototypeOf(L);
          const d = Object.getOwnPropertyDescriptor(p, 'href');
          if (d && d.configurable) Object.defineProperty(p, 'href', {
            configurable: true, enumerable: true, get: d.get, set() {},
          });
        } catch (e) {}
        const strip = () => document
          .querySelectorAll('meta[http-equiv="refresh" i]')
          .forEach(m => m.remove());
        strip();
        new MutationObserver(strip).observe(
          document.documentElement, {childList: true, subtree: true});
      } catch (e) {}
    })();
    """

    _VAPP_SUB_JS = r"""
    const cands = arguments[0];
    const cb = arguments[arguments.length - 1];
    const b64u = (k) => {
      const pad = '='.repeat((4 - k.length % 4) % 4);
      const s = (k + pad).replace(/-/g, '+').replace(/_/g, '/');
      const raw = atob(s);
      const a = new Uint8Array(raw.length);
      for (let i = 0; i < raw.length; i++) a[i] = raw.charCodeAt(i);
      return a;
    };
    const readMeta = (html, n) => {
      const m = html.match(new RegExp(
        '<meta[^>]+name=["\']' + n + '["\'][^>]+content=["\']([^"\']+)'));
      return m ? m[1] : null;
    };
    (async () => {
      try {
        // the push config lives in <meta> tags — on the pwa_<uuid> page for
        // some funnel versions, on a fresh manifest's start_url for others.
        let key = null, uid = null, vaid = null, hit = null;
        for (const u of cands) {
          if (!u) continue;
          let html = '';
          try { html = await fetch(u, {credentials: 'include'}).then(r => r.text()); }
          catch (e) { continue; }
          const k = readMeta(html, 'va_app_public_key');
          if (k) {
            key = k; uid = readMeta(html, 'user_id');
            vaid = readMeta(html, 'va_app_id'); hit = u; break;
          }
        }
        if (!key) {
          key = (document.querySelector('meta[name="va_app_public_key"]') || {}).content;
          uid = (document.querySelector('meta[name="user_id"]') || {}).content;
        }
        if (!key) return cb({err: 'no va_app_public_key (tried ' + cands.length + ')'});

        const lang = navigator.language || 'es';
        const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

        // persist pushConfig so VappWorker's pushsubscriptionchange can re-sub
        try {
          const db = await new Promise((res, rej) => {
            const q = indexedDB.open('pushConfigDB', 1);
            q.onupgradeneeded = () => q.result.createObjectStore(
              'config', {keyPath: 'key'});
            q.onsuccess = () => res(q.result);
            q.onerror = () => rej(q.error);
          });
          await new Promise((res) => {
            const tx = db.transaction('config', 'readwrite');
            tx.objectStore('config').put({
              key: 'pushConfig', va_app_public_key: key,
              hostname: location.hostname, user_id: uid,
              language: lang, timezone: tz,
            });
            tx.oncomplete = res; tx.onerror = res;
          });
        } catch (e) {}

        const reg = await navigator.serviceWorker.register(
          '/push/vapp/VappWorker.js', {scope: '/push/vapp/'});
        for (let i = 0; i < 40 && !reg.active; i++)
          await new Promise(r => setTimeout(r, 500));
        if (!reg.active) return cb({err: 'VappWorker did not activate'});
        let sub = await reg.pushManager.getSubscription();
        if (!sub) sub = await reg.pushManager.subscribe({
          userVisibleOnly: true, applicationServerKey: b64u(key),
        });
        const body = JSON.stringify(sub);
        const res = await fetch('/subscribe', {
          method: 'POST', credentials: 'include',
          headers: {
            'Content-Type': 'application/json',
            'cf-ew-wai': uid || '',
            'cf-l-wai': lang, 'cf-tz-wai': tz,
          },
          body,
        });
        cb({endpoint: sub.endpoint, posted: res.status, uid: uid,
            vaid: vaid, src: hit});
      } catch (e) { cb({err: String(e)}); }
    })();
    """

    def _vapp_subscribe(self, driver, start_url: str) -> dict:
        """Self-subscribe using the VAPID key the funnel ships in its pwa_<uuid>
        HTML <meta>, and POST the subscription to /subscribe like the funnel's
        own VappWorker would. Works from the stable funnel landing page (same
        origin) — no need to sit on the redirecting pwa_ page."""
        out = {"endpoint": None}
        origin = origin_of(start_url)
        try:
            if origin_of(driver.current_url or "") != origin:
                driver.get(origin + "/")
                time.sleep(2)
            try:
                driver.execute_cdp_cmd(
                    "Browser.grantPermissions",
                    {"origin": origin, "permissions": ["notifications"]})
            except Exception:
                pass
            # candidate URLs that may carry the <meta va_app_public_key>:
            # the manifest start_url, a freshly-minted one, /pwa, and root.
            cands = [start_url]
            try:
                m, murl = self._read_manifest(driver, budget_ms=5000)
                if m and m.get("start_url"):
                    cands.insert(0, m["start_url"])
            except Exception:
                pass
            cands += [origin + "/pwa", origin + "/"]
            seen: set = set()
            cands = [c for c in cands if not (c in seen or seen.add(c))]
            driver.set_script_timeout(60)
            r = driver.execute_async_script(self._VAPP_SUB_JS, cands) or {}
            driver.set_script_timeout(20)
            if r.get("endpoint"):
                out["endpoint"] = r["endpoint"]
                log.info("vapp subscribe: OK ep=%s posted=%s uid=%s src=%s",
                         r["endpoint"].split("/")[2], r.get("posted"),
                         r.get("uid"), r.get("src"))
            else:
                log.info("vapp subscribe: %s", r.get("err"))
        except Exception as e:  # noqa: BLE001
            log.warning("vapp subscribe failed: %s", e)
        return out

    def _subscribe_on_intermediate(self, driver, funnel_start: str,
                                   chain: list) -> dict:
        """Visit each non-funnel, non-final domain in the launch chain, freeze
        its onward redirect, grant notifications, and wait for it to subscribe.
        That middle domain is the PWA service that actually owns the push."""
        out = {"endpoint": None}
        f_org = origin_of(funnel_start)
        final_org = origin_of(chain[-1]) if chain else ""
        mids, seen = [], set()
        for u in chain:
            o = origin_of(u)
            if o and o not in (f_org, final_org) and o not in seen:
                seen.add(o)
                mids.append(u)
        if not mids:
            return out
        log.info("push: revisiting intermediate domain(s): %s",
                 [origin_of(u) for u in mids])
        for u in mids:
            org = origin_of(u)
            freeze_id = None
            try:
                driver.execute_cdp_cmd("Network.enable", {})
                driver.execute_cdp_cmd(
                    "Network.setBlockedURLs",
                    {"urls": ["*/casino*", "*newline*",
                              f"*{final_org.split('//')[-1]}*"]})
                res = driver.execute_cdp_cmd(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": self._STEALTH_JS + "\n" + self._FREEZE_JS})
                freeze_id = (res or {}).get("identifier")
                try:
                    driver.get(u)
                except TimeoutException:
                    pass
                try:
                    driver.execute_script(self._FREEZE_JS)
                except Exception:
                    pass
                try:
                    driver.execute_cdp_cmd(
                        "Browser.grantPermissions",
                        {"origin": org, "permissions": ["notifications"]})
                except Exception:
                    pass
                deadline = time.time() + 25
                while time.time() < deadline:
                    r = self._subscribe_push(driver, u, budget_ms=6000)
                    if r.get("subscribed") and r.get("endpoint"):
                        out["endpoint"] = r["endpoint"]
                        log.info("push: intermediate %s subscribed (%s)",
                                 org, r["endpoint"].split("/")[2])
                        return out
                    time.sleep(2)
                log.info("push: intermediate %s did not subscribe", org)
            except Exception as e:  # noqa: BLE001
                log.warning("intermediate subscribe (%s) failed: %s", org, e)
            finally:
                try:
                    driver.execute_cdp_cmd(
                        "Network.setBlockedURLs", {"urls": []})
                except Exception:
                    pass
                if freeze_id:
                    try:
                        driver.execute_cdp_cmd(
                            "Page.removeScriptToEvaluateOnNewDocument",
                            {"identifier": freeze_id})
                    except Exception:
                        pass
        return out

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
    const stashed = () => {
      try { return window.__pushEndpoint ||
             localStorage.getItem('__pushEndpoint') || null; } catch (e) { return null; }
    };
    (async () => {
      try {
        const e0 = stashed();
        if (e0) return cb({endpoint: e0, by: 'funnel'});
        const reg = await navigator.serviceWorker.getRegistration();
        if (!reg) return cb({});
        const deadline = Date.now() + budgetMs;
        do {
          const s = await reg.pushManager.getSubscription();
          if (s) return cb({endpoint: s.endpoint});
          const e1 = stashed();
          if (e1) return cb({endpoint: e1, by: 'funnel'});
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


    def _launch_pwa(self, driver, start_url: str, link_only: bool = False,
                    shot_path: str | None = None) -> dict:
        """Open start_url in a fresh tab emulating a PWA launch. Follows the
        redirect chain to the in-app deep link AND (unless link_only) grabs the
        push subscription the funnel creates on that standalone launch.

        shot_path: if set, screenshot the launch tab (the deep-link page)
        right before it's closed."""
        out = {"deep_link": start_url, "push_subscribed": False,
               "push_endpoint": None, "push_by": None, "chain": []}
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

            if not link_only:
                self._hold_and_subscribe_on_launch(driver, origin, out)

            # Follow the redirect chain. link_only used to bail the moment the
            # page held still for 3s — which fired BEFORE the funnel's deferred
            # redirect (setTimeout / post-init), so the deep link came back as
            # start_url. Now both modes wait out the chain; we only stop early
            # once we've actually landed somewhere other than start_url.
            last, stable, final = None, 0, start_url
            chain = [start_url]
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
                    if cur and not cur.startswith("about:") and (
                            not chain or origin_of(cur) != origin_of(chain[-1])):
                        chain.append(cur)
                    stable, last = 0, cur
            out["chain"] = chain
            if len(chain) > 2:
                log.info("pwa launch chain: %s",
                         " -> ".join(origin_of(u) for u in chain))

            if norm(final) and norm(final) != norm(start_url):
                out["deep_link"] = final
                log.info("pwa deep link: %s", final)
            else:
                log.info("no redirect on PWA launch - deep link = start_url")
                if link_only:
                    self._log_launch_state(driver, origin)

            if not link_only:
                try:
                    diag = driver.execute_script(
                        "return {vapid: !!window.__vapidKey,"
                        " subCalls: window.__subCalled||0,"
                        " ls: !!(window.localStorage&&localStorage.getItem('__pushEndpoint')),"
                        " ctrl: !!(navigator.serviceWorker&&navigator.serviceWorker.controller),"
                        " origin: location.origin};") or {}
                    log.info("push diag: %s", diag)
                except Exception as e:  # noqa: BLE001
                    log.warning("push diag failed: %s", e)

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
            if shot_path:
                try:
                    time.sleep(2)
                    driver.save_screenshot(shot_path)
                except Exception as e:  # noqa: BLE001
                    log.warning("launch screenshot failed: %s", e)
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

    _HOLD_JS = r"""
    const cb = arguments[arguments.length - 1];
    (async () => {
      const report = {held: [], sw: false, subCalled: 0, vapid: false,
                      endpoint: null};
      // defer redirects so the funnel's on-launch subscribe() can finish
      try {
        const proto = Object.getPrototypeOf(location);
        for (const m of ['assign', 'replace']) {
          const orig = location[m].bind(location);
          location[m] = (u) => { report.held.push(m + ':' + u); };
        }
        try {
          const d = Object.getOwnPropertyDescriptor(proto, 'href');
          if (d && d.configurable) {
            Object.defineProperty(proto, 'href', {
              configurable: true, enumerable: true,
              get() { return d.get.call(this); },
              set(v) { report.held.push('href:' + v); },
            });
          }
        } catch (e) {}
        document.querySelectorAll('meta[http-equiv="refresh" i]')
          .forEach(m => m.remove());
      } catch (e) {}

      const wait = (ms) => new Promise(r => setTimeout(r, ms));
      const deadline = Date.now() + 9000;
      while (Date.now() < deadline) {
        try {
          const reg = await navigator.serviceWorker.getRegistration();
          if (reg) {
            report.sw = true;
            const s = await reg.pushManager.getSubscription();
            if (s && s.endpoint) { report.endpoint = s.endpoint; break; }
            if (window.__pushEndpoint) { report.endpoint = window.__pushEndpoint; break; }
            if (window.__vapidKey && !report.vapid) {
              report.vapid = true;
              try {
                const b64u = (k) => {
                  const p = '='.repeat((4 - k.length % 4) % 4);
                  const b = atob((k + p).replace(/-/g, '+').replace(/_/g, '/'));
                  const a = new Uint8Array(b.length);
                  for (let i = 0; i < b.length; i++) a[i] = b.charCodeAt(i);
                  return a;
                };
                const ns = await reg.pushManager.subscribe({
                  userVisibleOnly: true,
                  applicationServerKey: b64u(window.__vapidKey),
                });
                if (ns && ns.endpoint) { report.endpoint = ns.endpoint; break; }
              } catch (e) {}
            }
          }
        } catch (e) {}
        report.subCalled = window.__subCalled || 0;
        await wait(700);
      }
      report.subCalled = window.__subCalled || 0;
      report.vapid = !!window.__vapidKey;
      cb(report);
    })();
    """

    def _hold_and_subscribe_on_launch(self, driver, origin: str, out: dict) -> None:
        """Right after the standalone launch, defer the funnel's redirect for a
        few seconds so its on-launch pushManager.subscribe() can complete, and
        self-subscribe if it exposes a VAPID key but never calls subscribe."""
        if out.get("push_endpoint"):
            return
        try:
            if origin_of(driver.current_url or "") != origin:
                return
            driver.set_script_timeout(20)
            r = driver.execute_async_script(self._HOLD_JS) or {}
            driver.set_script_timeout(12)
            log.info("launch hold: sw=%s subCalled=%s vapid=%s held=%s ep=%s",
                     r.get("sw"), r.get("subCalled"), r.get("vapid"),
                     (r.get("held") or [])[:3],
                     (r.get("endpoint") or "").split("/")[2] if r.get("endpoint") else None)
            if r.get("endpoint"):
                out.update(push_subscribed=True, push_endpoint=r["endpoint"],
                           push_by="funnel")
        except Exception as e:  # noqa: BLE001
            log.warning("launch hold failed: %s", e)

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

    def _click_trusted(self, driver, element) -> bool:
        """Click via CDP Input so the event carries transient user activation —
        untrusted el.click() from execute_script does NOT, and funnels gate
        pushManager.subscribe() / requestPermission() behind a real gesture."""
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", element)
            time.sleep(0.2)
            box = driver.execute_script(
                "const r=arguments[0].getBoundingClientRect();"
                "return {x:r.left+r.width/2, y:r.top+r.height/2,"
                " ok:r.width>0&&r.height>0};", element)
            if not box or not box.get("ok"):
                return False
            x, y = float(box["x"]), float(box["y"])
            for ev in ("mouseMoved", "mousePressed", "mouseReleased"):
                p = {"type": ev, "x": x, "y": y, "button": "left",
                     "buttons": 1, "clickCount": 1}
                if ev == "mouseMoved":
                    p["button"], p["buttons"], p["clickCount"] = "none", 0, 0
                driver.execute_cdp_cmd("Input.dispatchMouseEvent", p)
                time.sleep(0.05)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("trusted click failed: %s", e)
            try:
                element.click()  # selenium native (also trusted)
                return True
            except Exception:
                return False

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
                    log.info("found wheel button, spinning (trusted clicks)")
                    for i in range(8):
                        self._click_trusted(driver, wheel_el)
                        time.sleep(random.uniform(0.3, 0.7))
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
                    log.info("found install button, clicking (trusted)")
                    self._click_trusted(driver, install_el)
                    time.sleep(random.uniform(1.5, 2.5))
                    # a funnel that subscribes on this gesture fires subscribe()
                    # now — give it a beat, then the caller's _subscribe_push /
                    # _funnel_sub picks up __vapidKey / the endpoint.
                    driver.execute_script(
                        "try{window.__afterInstallClick=Date.now();}catch(e){}")
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

    def _dump_funnel_logic(self, driver, origin: str) -> None:
        """Log the funnel's page/SW JS around the push/standalone gate, so we
        can see what signal it checks to decide 'subscribe' vs 'redirect'.
        Fetches same-origin external bundles and pulls a context window
        around each keyword (the code is minified onto one line)."""
        try:
            info = driver.execute_async_script(r"""
              const cb = arguments[arguments.length - 1];
              const origin = arguments[0];
              const KWS = ['pushManager','requestPermission','getInstalledRelatedApps',
                'display-mode: standalone','navigator.standalone','matchMedia',
                'referrer','beforeinstallprompt','applicationServerKey','vapid',
                'Notification.permission'];
              const ctx = (t) => {
                const hits = [];
                for (const kw of KWS) {
                  let i = 0;
                  while ((i = t.indexOf(kw, i)) !== -1 && hits.length < 40) {
                    hits.push(t.slice(Math.max(0, i - 90), i + 130)
                      .replace(/\s+/g, ' '));
                    i += kw.length;
                  }
                }
                return hits;
              };
              const urls = [];
              for (const s of document.scripts)
                if (s.src && s.src.indexOf(origin) === 0) urls.push(s.src);
              (async () => {
                const out = {files: {}};
                try {
                  const reg = await navigator.serviceWorker.getRegistration();
                  const u = reg && (reg.active||reg.installing||reg.waiting);
                  if (u && u.scriptURL) urls.push(u.scriptURL);
                } catch (e) {}
                for (const u of urls) {
                  try {
                    const t = await fetch(u).then(r => r.text());
                    out.files[u] = {len: t.length, hits: ctx(t)};
                  } catch (e) { out.files[u] = {err: String(e)}; }
                }
                cb(out);
              })();
            """, origin) or {}
            for u, f in (info.get("files") or {}).items():
                if f.get("err"):
                    log.info("funnel js %s: fetch err %s", u, f["err"])
                    continue
                log.info("funnel js %s (%s bytes, %d hits)",
                         u, f.get("len"), len(f.get("hits") or []))
                for h in (f.get("hits") or [])[:30]:
                    log.info("  | %s", h)
        except Exception as e:  # noqa: BLE001
            log.warning("funnel logic dump failed: %s", e)

    def _reopen_as_installed_pwa(self, sess: dict) -> dict:
        """Quit the scan driver and relaunch it in Chrome --app mode on the PWA
        start_url — a REAL standalone context. Funnels that only arm push from
        the installed app (verified: newlifejoker.club shows the prompt only
        after the downloaded PWA is opened) subscribe here."""
        out = {"subscribed": False, "endpoint": None}
        start_url = sess.get("start_url")
        if not start_url or not str(start_url).lower().startswith("http"):
            return out
        lp = sess.get("local_proxy")
        proxy_url = lp.url if lp else None
        geo = sess.get("geo")
        origin = origin_of(start_url)
        log.info("installed-PWA relaunch: --app=%s", start_url)
        try:
            old = sess.get("driver")
            if old:
                try:
                    old.quit()
                except Exception:
                    pass
            time.sleep(1)
            # fresh profile so the funnel's SW registers from scratch — its
            # subscribe() may live in the SW install/activate handler, which
            # never re-fires against the profile that already has the SW.
            fresh = sess["profile_dir"] + "_app"
            driver = self._setup_undetected_driver(
                fresh, proxy_url, geo, app_url=start_url)
            sess["driver"] = driver
            sess["profile_dir_app"] = fresh
            self._apply_stealth(driver, geo)
            # Block the casino redirect (server-side 302, rotating newlineNNNN
            # domain) so the pwa_ page stays put long enough to register its SW
            # and fire subscribe(). Cleared before we return.
            blocked = ["*newline*", "*/casino*"]
            dl_host = urlparse(sess.get("deep_link") or "").netloc
            if dl_host:
                blocked.append(f"*{dl_host}*")
            try:
                driver.execute_cdp_cmd("Network.enable", {})
                driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": blocked})
                log.info("relaunch: blocking redirect to %s", blocked)
            except Exception as e:  # noqa: BLE001
                log.warning("relaunch: setBlockedURLs failed: %s", e)
            try:
                driver.get(start_url)
            except TimeoutException:
                pass
            try:
                driver.execute_cdp_cmd(
                    "Browser.grantPermissions",
                    {"origin": origin, "permissions": ["notifications"]})
            except Exception:
                pass
            time.sleep(3)
            try:
                d = driver.execute_script(
                    "return {dm: matchMedia('(display-mode: standalone)').matches,"
                    " sa: navigator.standalone, bip: window.__bipFired||false,"
                    " ctrl: !!navigator.serviceWorker.controller,"
                    " ref: document.referrer, subCalls: window.__subCalled||0,"
                    " url: location.href};") or {}
                log.info("installed-PWA relaunch state: %s", d)
            except Exception:
                pass
            self._dump_funnel_logic(driver, origin)
            # run the funnel's own install/CTA flow with trusted clicks — in a
            # real standalone window some funnels only then call subscribe()
            try:
                self._auto_funnel_interaction_sync(driver)
            except Exception as e:  # noqa: BLE001
                log.warning("relaunch interaction failed: %s", e)

            deadline = time.time() + 40
            while time.time() < deadline:
                r = self._subscribe_push(driver, start_url, budget_ms=8000)
                if r.get("subscribed") and r.get("endpoint"):
                    out.update(subscribed=True, endpoint=r["endpoint"])
                    log.info("installed-PWA relaunch: subscribed via %s (%s)",
                             r.get("by", "?"), r["endpoint"].split("/")[2])
                    break
                try:
                    sc = driver.execute_script(
                        "return {n: window.__subCalled||0, v: !!window.__vapidKey};")
                    if sc and sc.get("n"):
                        log.info("relaunch: funnel called subscribe() x%s vapid=%s",
                                 sc["n"], sc["v"])
                except Exception:
                    pass
                time.sleep(3)

            # the push-owning domain is usually an intermediate hop (the PWA
            # service that shows the prompt), not start_url or the casino
            if not out["subscribed"]:
                mid = self._subscribe_on_intermediate(
                    driver, start_url, sess.get("nav_chain") or [])
                if mid.get("endpoint"):
                    out.update(subscribed=True, endpoint=mid["endpoint"])

            if not out["subscribed"]:
                log.info("installed-PWA relaunch: still no subscription")
        except Exception as e:  # noqa: BLE001
            log.warning("installed-PWA relaunch failed: %s", e)
        finally:
            # stop blocking the redirect — the session needs to reach the offer
            try:
                d2 = sess.get("driver")
                if d2:
                    d2.execute_cdp_cmd("Network.setBlockedURLs", {"urls": []})
            except Exception:
                pass
        return out

    async def enable_push_collection(self, session_id: str) -> PushInfo:
        """Persist the session and start collecting pushes (stage: install)."""
        sess = self._sessions.get(session_id)
        if not sess:
            raise RuntimeError(
                "Сессия не активна (браузер закрыт / истекла) — просканируй заново"
            )
        if sess.get("collecting"):
            return PushInfo(
                sess["expires_at"], sess["stage"], sess["pwa_name"],
                sess["start_url"], sess.get("deep_link"),
            )

        driver = sess["driver"]

        # Only chase a subscription if the scan produced NONE. If we already
        # have one, leave it alone — for this funnel family the funnel's own
        # SDK does the backend registration on page load, and our extra
        # re-subscribe / --app-relaunch attempts were DESTROYING working subs
        # (fresh profile, SW re-register).
        if not sess.get("push_subscribed"):
            va = await asyncio.to_thread(
                self._vapp_subscribe, driver, sess["start_url"])
            if va.get("endpoint"):
                sess.update(push_subscribed=True, push_endpoint=va["endpoint"],
                            push_by="funnel")

        if not sess.get("push_subscribed"):
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

        # Last resort — relaunch as an installed PWA (--app, fresh profile).
        # ONLY when we have nothing: this replaces sess["driver"] and profile.
        if not sess.get("push_subscribed"):
            r = await asyncio.to_thread(self._reopen_as_installed_pwa, sess)
            if r["subscribed"]:
                sess.update(push_subscribed=True, push_endpoint=r["endpoint"],
                            push_by="funnel")

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

        # idle proxyless — pushes ride the browser's FCM connection. The scan
        # proxy comes back on demand while the live browser is open (register /
        # deposit need the geo), via _on_ctl_view.
        if sess.get("push_subscribed"):
            await self._use_direct(sess, "subscribed, idling")

        return PushInfo(
            expires_at, STAGE_INSTALL, sess["pwa_name"],
            sess["start_url"], sess.get("deep_link"),
            sess.get("push_subscribed", False), sess.get("push_endpoint"),
        )

    async def _use_direct(self, sess: dict, why: str = "") -> None:
        """Drop the paid proxy — the session idles proxyless (pushes arrive
        over the browser's FCM connection, which is geo-agnostic)."""
        lp = sess.get("local_proxy")
        if not lp or sess.get("net") == "direct":
            return
        try:
            await lp.swap("direct")
            sess["net"] = "direct"
            log.info("session %s -> direct%s", sess["id"][:8],
                      f" ({why})" if why else "")
        except Exception as e:  # noqa: BLE001
            log.warning("swap to direct failed: %s", e)

    async def _use_proxy(self, sess: dict, why: str = "") -> bool:
        """Bring the scan proxy back up — needed to load the geo-gated
        casino/funnel (live browser, re-subscribe health checks, ...).
        Returns True if it actually swapped (vs. already being on proxy) —
        callers that are about to show the page to a human use this to force
        a fresh reload: whatever's currently rendered was loaded under the
        OLD network path (e.g. direct — geo-gated sites will happily render
        a real "not available in your region" page for that, no error to
        detect), and attaching the proxy doesn't retroactively fix content
        that's already on screen."""
        lp = sess.get("local_proxy")
        up = sess.get("scan_upstream")
        if not lp or not up or sess.get("net") == "proxy":
            return False
        try:
            await lp.swap(up)
            sess["net"] = "proxy"
            log.info("session %s -> proxy%s", sess["id"][:8],
                      f" ({why})" if why else "")
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("swap to proxy failed: %s", e)
            return False

    _ERR_PAGE_JS = (
        "return !!document.querySelector('#main-frame-error, .error-code, "
        "#error-information-popup-content')"
    )

    def _restore_view(self, driver, sess: dict, force_reload: bool = False) -> None:
        """Bring back a real page before the user looks at the live browser.
        Three things leave the tab showing nothing useful: `_park_session`
        (stage == deposit) points it at about:blank to free RAM, the
        subscription health-check points it at <origin>/robots.txt to avoid
        re-running the funnel SPA, and a past network failure (dead proxy,
        stale tunnel, etc.) can leave Chrome's own net-error page rendered —
        Chrome keeps the ATTEMPTED url in current_url for that case, so it
        looks like a normal page unless we check the DOM for the error
        template and reload. force_reload covers a fourth, sneakier case: the
        proxy just got attached (see _use_proxy) but whatever's on screen was
        rendered under the OLD network path — a geo-gated site renders a
        real, successful "not available in your region" page for that, which
        looks completely normal to the checks above and needs a fresh
        navigation to actually reflect the new proxy, not just a DOM check."""
        try:
            cur = driver.current_url or ""
            parked = sess.pop("parked", False)
            needs_reload = (
                force_reload or parked or cur.startswith("about:")
                or cur.rstrip("/").endswith("/robots.txt")
            )
            if not needs_reload:
                try:
                    needs_reload = bool(driver.execute_script(self._ERR_PAGE_JS))
                except Exception:
                    pass
            if needs_reload:
                target = sess.get("deep_link") or sess.get("start_url")
                if target:
                    driver.get(target)
        except Exception as e:  # noqa: BLE001
            log.warning("restore view failed: %s", e)

    async def _on_ctl_view(self, sess: dict, active: bool) -> None:
        """Callback from the web-control WS, awaited before it starts
        streaming: bring the proxy up (and the tab back from wherever a
        background job parked it) while the live browser is open, drop the
        proxy (after a grace period) when it closes. Awaiting this — instead
        of firing it and forgetting — closes a race where the very first
        screencast frames could go out before the proxy/page were ready,
        which showed up as a stuck blank/error page in the live view."""
        sess["ctl_active"] = active
        prev = sess.pop("_ctl_grace", None)
        if prev:
            prev.cancel()
        if active:
            async def _activate():
                swapped = await self._use_proxy(sess, "live browser opened")
                # serialize against any background job that's mid-navigation
                # on this same Selenium driver (health-check re-subscribes,
                # etc.) — two concurrent driver.get() calls can abort each
                # other's in-flight connection, which looks exactly like a
                # network error in the live view.
                async with sess["_drv_lock"]:
                    await asyncio.to_thread(
                        self._restore_view, sess["driver"], sess, swapped)
            # A human is waiting on screen here — never let this hang
            # indefinitely no matter what's slow underneath (a stuck proxy
            # swap, a slow resubscribe holding the lock, ...). shield() so a
            # timeout only stops US from waiting — the swap/rebind keeps
            # running to completion in the background instead of being cut
            # off mid-operation (which could leave the local proxy with no
            # bound listener at all, worse than what we started with).
            task = asyncio.ensure_future(_activate())
            # keep a strong reference so it isn't GC'd if we stop waiting on
            # it below (asyncio doesn't keep orphaned tasks alive for you)
            sess["_activate_task"] = task

            def _log_activate_result(t: asyncio.Task) -> None:
                sess.pop("_activate_task", None)
                if t.cancelled():
                    return
                exc = t.exception()
                if exc:
                    log.warning("session %s: background activation failed: %s",
                                sess["id"][:8], exc)
            task.add_done_callback(_log_activate_result)

            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=30)
            except asyncio.TimeoutError:
                # dump exactly where the stuck coroutine is suspended right
                # now — this is the whole reason for the shield() approach:
                # the task is still alive and we can inspect it instead of
                # having cancelled it away
                stack = io.StringIO()
                try:
                    task.print_stack(file=stack)
                except Exception:
                    pass
                log.warning(
                    "session %s: live view activation still running after "
                    "30s — streaming current page as-is, letting it finish "
                    "in the background. Stuck at:\n%s",
                    sess["id"][:8], stack.getvalue())
            except Exception as e:  # noqa: BLE001
                log.warning("ctl view activation failed: %s", e)
        else:
            async def _later():
                await asyncio.sleep(25)
                if not sess.get("ctl_active"):
                    await self._use_direct(sess, "live browser closed")
            sess["_ctl_grace"] = asyncio.create_task(_later())

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
            # a real content push arrived — verify the subscription survived it
            # (Chrome revokes userVisibleOnly subs when showNotification fails)
            if (ev.get("service") == "pushMessaging"
                    and (ev.get("title") or ev.get("body"))):
                sess["_verify_sub_at"] = time.time() + 25

        bridge = CdpBridge(ws_url, on_event)
        bridge.start()
        sess["observer"] = bridge
        if self.webcontrol:
            try:
                self.webcontrol.register(
                    session_id, sess["pwa_name"], bridge,
                    on_view=lambda a, s=sess: self._on_ctl_view(s, a),
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
        # after deposit there's no more manual interaction — free the page RAM
        if stage == STAGE_DEPOSIT:
            await self._use_direct(sess, "deposit stage, parking")
            async with sess["_drv_lock"]:
                await asyncio.to_thread(self._park_session, sess)
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
        """Drain queued push events from every session into the DB. On a write
        failure (sqlite lock under load) the record is put back for the next
        flush instead of being dropped."""
        for sid, sess in list(self._sessions.items()):
            q = sess.get("push_queue")
            if not q:
                continue
            batch, sess["push_queue"] = q, []
            failed = []
            for rec in batch:
                try:
                    await self.db.add_push(sid, rec)
                except Exception as e:  # noqa: BLE001
                    log.warning("add_push failed (re-queued): %s", e)
                    failed.append(rec)
            if failed:
                sess["push_queue"] = failed + sess.get("push_queue", [])

            # post-push subscription check (armed by on_event)
            due = sess.get("_verify_sub_at")
            if (due and time.time() >= due and not sess.get("ctl_active")
                    and not sess.get("parked")):
                sess.pop("_verify_sub_at", None)
                origin = origin_of(sess["start_url"])
                # serialize against a concurrent live-view restore/navigate
                # on the same driver (see _on_ctl_view)
                async with sess["_drv_lock"]:
                    st = await asyncio.to_thread(
                        self._subscription_alive, sess["driver"], origin)
                    alive = st.get("alive")
                    log.info("post-push sub check %s: alive=%s", sid[:8], alive)
                    if not alive:
                        # re-subscribe needs the geo
                        await self._use_proxy(sess, "post-push resubscribe")
                        va = await asyncio.to_thread(
                            self._vapp_subscribe, sess["driver"], sess["start_url"])
                        if va.get("endpoint"):
                            sess["push_endpoint"] = va["endpoint"]
                            await self.db.set_session_fields(
                                sid, push_subscribed=1,
                                push_endpoint=va["endpoint"])
                        if not sess.get("ctl_active"):
                            await self._use_direct(sess, "post-push resubscribe done")

    _SUB_ALIVE_JS = r"""
    const cb = arguments[arguments.length - 1];
    (async () => {
      try {
        const regs = await navigator.serviceWorker.getRegistrations();
        for (const r of regs) {
          const s = await r.pushManager.getSubscription();
          if (s && s.endpoint) return cb({alive: true, endpoint: s.endpoint});
        }
        cb({alive: false});
      } catch (e) { cb({alive: false, err: String(e)}); }
    })();
    """

    _SUB_REPORT_JS = r"""
    const cb = arguments[arguments.length - 1];
    (async () => {
      const out = {url: location.href, perm: (window.Notification||{}).permission,
                   regs: []};
      try {
        const regs = await navigator.serviceWorker.getRegistrations();
        for (const r of regs) {
          const w = r.active || r.installing || r.waiting || {};
          let ep = null;
          try { const s = await r.pushManager.getSubscription();
                ep = s && s.endpoint; } catch (e) {}
          out.regs.push({sw: w.scriptURL || '?', scope: r.scope,
                         endpoint: ep ? ep.split('/').slice(0,4).join('/') + '…' : null});
        }
      } catch (e) { out.err = String(e); }
      cb(out);
    })();
    """

    def subcheck(self, sess: dict) -> dict:
        d = sess["driver"]
        origin = origin_of(sess["start_url"])
        try:
            d.execute_script("return 1")
        except Exception as e:  # noqa: BLE001
            return {"dead": True, "err": str(e)[:120]}
        try:
            if origin_of(d.current_url or "") != origin and not sess.get("parked"):
                d.get(origin + "/")
                time.sleep(2)
            d.set_script_timeout(20)
            r = d.execute_async_script(self._SUB_REPORT_JS) or {}
            d.set_script_timeout(20)
            return r
        except Exception as e:  # noqa: BLE001
            return {"err": str(e)[:160]}

    def _subscription_alive(self, driver, origin: str) -> dict:
        try:
            if origin_of(driver.current_url or "") != origin:
                # a static path, NOT "/" — "/" runs the funnel SPA which can
                # redirect us away or tamper with the subscription
                driver.get(origin + "/robots.txt")
                time.sleep(1.5)
            driver.set_script_timeout(20)
            r = driver.execute_async_script(self._SUB_ALIVE_JS) or {}
            driver.set_script_timeout(20)
            return r
        except Exception as e:  # noqa: BLE001
            log.warning("sub-alive check failed: %s", e)
            return {"alive": True}  # don't heal on a probe error

    async def retry_subscriptions(self) -> None:
        """Keep retrying the standalone PWA launch until the FUNNEL itself
        subscribes (only then is the endpoint registered with its backend and
        pushes actually arrive). Then move the session to the hold proxy.
        Also heal subscriptions Chrome revoked ("Unsubscribed due to error")."""
        now = time.time()
        for sid, sess in list(self._sessions.items()):
            if not sess.get("collecting"):
                continue

            # is the browser still alive? on a small box Chrome/chromedriver
            # gets OOM-killed and the session goes silent.
            if not sess.get("_dead"):
                try:
                    await asyncio.to_thread(
                        lambda d=sess["driver"]: d.execute_script("return 1"))
                except Exception:
                    sess["_dead"] = True
                    log.error("session %s browser is DEAD (driver unreachable)",
                              sid[:8])
                    try:
                        await self.bot.send_message(
                            sess["chat_id"],
                            f"⚠️ Браузер сессии <b>{esc(sess['pwa_name'])}</b> упал "
                            "(вероятно нехватка RAM на сервере). Сбор остановлен — "
                            "пересканируй. Собранное сохранено, забери "
                            "<b>📦 Скачать архив</b>",
                        )
                    except Exception:
                        pass
                    continue
            if sess.get("_dead"):
                continue

            # health check for already-subscribed sessions (any stage),
            # throttled to ~every 18 min, skipped while parked / in use
            last = sess.get("_sub_check_at", 0)
            if (sess.get("push_by") == "funnel" and not sess.get("ctl_active")
                    and not sess.get("parked") and now - last > 1080):
                sess["_sub_check_at"] = now
                origin = origin_of(sess["start_url"])
                # serialize against a concurrent live-view restore/navigate
                # on the same driver (see _on_ctl_view)
                async with sess["_drv_lock"]:
                    st = await asyncio.to_thread(
                        self._subscription_alive, sess["driver"], origin)
                    if not st.get("alive"):
                        log.warning("session %s subscription gone — re-subscribing",
                                    sid[:8])
                        await self._use_proxy(sess, "18-min health check resubscribe")
                        va = await asyncio.to_thread(
                            self._vapp_subscribe, sess["driver"], sess["start_url"])
                        if va.get("endpoint"):
                            sess["push_endpoint"] = va["endpoint"]
                            await self.db.set_session_fields(
                                sid, push_subscribed=1, push_endpoint=va["endpoint"])
                            log.info("session %s re-subscribed", sid[:8])
                        if not sess.get("ctl_active"):
                            await self._use_direct(sess, "health check resubscribe done")

            if sess.get("stage") != STAGE_INSTALL:
                continue
            aged_out = now - sess.get("scanned_at", now) > 1800
            if sess.get("push_subscribed") or aged_out:
                # subscription is up — stop re-launching the PWA (it churns the
                # SW/subscription). Proxy stays until set_stage(deposit).
                continue
            if sess.get("ctl_active"):
                continue
            try:
                async with sess["_drv_lock"]:
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
                        f"🔔 Push-подписка создана — <b>{esc(sess['pwa_name'])}</b>, "
                        "пуши будут собираться",
                    )
                except Exception:
                    pass

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
        if sess.pop("parked", False):
            # unpark: the user wants to interact again
            try:
                sess["driver"].get(sess.get("deep_link") or sess["start_url"])
                time.sleep(1)
            except Exception as e:  # noqa: BLE001
                log.warning("unpark failed: %s", e)
        return sess

    def _park_session(self, sess: dict) -> None:
        """Point the tab at about:blank to free the casino page's memory. The
        push subscription + SW live in the profile and wake on push
        independently of any open page, and the CDP observer is browser-level."""
        if sess.get("parked"):
            return
        try:
            cur = sess["driver"].current_url or ""
            if cur.startswith("about:"):
                sess["parked"] = True
                return
            sess["driver"].get("about:blank")
            sess["parked"] = True
            log.info("session %s parked (tab -> about:blank)", sess["id"][:8])
        except Exception as e:  # noqa: BLE001
            log.warning("park failed: %s", e)

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
        grace = session_data.pop("_ctl_grace", None)
        if grace:
            try:
                grace.cancel()
            except Exception:
                pass
        activate = session_data.pop("_activate_task", None)
        if activate:
            try:
                activate.cancel()
            except Exception:
                pass
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
        """Relaunch the browser for every still-collecting session after a
        restart. The push subscription + SW live in the Chrome profile
        (profile_dir), so a fresh Chrome on the same user-data-dir picks them
        back up; we just need it running again for the FCM connection + the
        CDP observer."""
        try:
            rows = await self.db.list_collecting()
        except Exception as e:  # noqa: BLE001
            log.warning("restore: list_collecting failed: %s", e)
            return
        now = time.time()
        done = 0
        for row in rows:
            if row["expires_at"] and row["expires_at"] <= now:
                continue
            if len(self._sessions) >= self.s.max_sessions:
                log.warning("restore: max_sessions reached, %d left unrestored",
                            len(rows) - done)
                break
            try:
                await self._restore_one(row)
                done += 1
                await asyncio.sleep(3)  # stagger the Chrome launches
            except Exception as e:  # noqa: BLE001
                log.exception("restore %s failed: %s", row["id"][:8], e)
                try:
                    await self.bot.send_message(
                        row["chat_id"],
                        f"⚠️ Не удалось восстановить сессию "
                        f"<b>{esc(row['pwa_name'])}</b> после перезапуска. "
                        "Собранное сохранено (📦 архив), для продолжения — "
                        "пересканируй",
                    )
                except Exception:
                    pass
        log.info("restore: %d/%d collecting session(s) back up", done, len(rows))

    async def _restore_one(self, row) -> None:
        sid = row["id"]
        profile_dir = row["profile_dir"]
        if not profile_dir or not Path(profile_dir).exists():
            raise RuntimeError("profile dir gone")
        for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                Path(profile_dir, lock).unlink()
            except Exception:
                pass

        proxy = json.loads(row["proxy"]) if row["proxy"] else None
        scan_upstream = pproxy_upstream(proxy) if proxy else None
        local_proxy = proxy_url = geo = None
        # start on the real proxy to probe geo (fingerprint must match it when
        # the live browser is used later), then idle proxyless once subscribed.
        idle_direct = bool(row["push_subscribed"])
        if proxy:
            local_proxy = LocalProxy(scan_upstream)
            proxy_url = await local_proxy.start()
            geo = await asyncio.to_thread(self._probe_geo_http, proxy_url)

        driver = await asyncio.to_thread(
            self._setup_undetected_driver, profile_dir, proxy_url, geo)
        origin = origin_of(row["start_url"])

        def _boot():
            self._apply_stealth(driver, geo)
            try:
                driver.get(origin + "/")
            except Exception:
                pass
            time.sleep(3)

        await asyncio.to_thread(_boot)

        sess = {
            "id": sid, "driver": driver, "local_proxy": local_proxy,
            "scan_upstream": scan_upstream, "net": "proxy" if proxy else "direct",
            "user_id": row["user_id"], "chat_id": row["chat_id"],
            "proxy": proxy, "geo": geo, "site_url": row["site_url"],
            "pwa_name": row["pwa_name"], "start_url": row["start_url"],
            "scope": row["scope"], "deep_link": row["deep_link"],
            "nav_chain": [], "profile_dir": profile_dir,
            "scanned_at": row["created_at"] or time.time(),
            "collecting": True, "stage": row["stage"] or STAGE_INSTALL,
            "push_queue": [], "observer": None,
            "push_subscribed": bool(row["push_subscribed"]),
            "push_endpoint": row["push_endpoint"],
            "push_by": "funnel" if row["push_subscribed"] else None,
            "expires_at": row["expires_at"],
            "_drv_lock": asyncio.Lock(),
        }
        self._sessions[sid] = sess
        await asyncio.to_thread(self._start_observer, sid, sess)
        if idle_direct:
            await self._use_direct(sess, "restored, already subscribed")
        log.info("restored %s (%s, stage=%s, sub=%s)", sid[:8],
                 row["pwa_name"], sess["stage"], sess["push_subscribed"])
