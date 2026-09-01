from __future__ import annotations

import html
import re
from urllib.parse import urlparse

_URL_RE = re.compile(r"^https?://", re.I)


def is_http_url(text: str) -> bool:
    return bool(_URL_RE.match(text.strip()))


def origin_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def esc(text: str | None) -> str:
    return html.escape(str(text)) if text is not None else ""


def extract_push_fields(ev: dict) -> dict:
    """Pull title/body/icon/url out of a CDP BackgroundService event dict
    (works both live and on a stored `raw` event)."""
    import json as _json

    meta = {}
    for m in ev.get("eventMetadata", []) or []:
        k = (m.get("key") or "").strip()
        if k:
            meta[k] = m.get("value")
    low = {k.lower(): v for k, v in meta.items()}

    def pick(*subs):
        for sub in subs:
            for k, v in low.items():
                if sub in k and v not in (None, ""):
                    return v
        return None

    svc = ev.get("service")
    title = pick("title")
    body = pick("body", "message", "text", "content")
    icon = pick("icon", "image", "badge")
    url = pick("click_action", "landing url", "notification data", "url", "link")

    payload = meta.get("Payload") or low.get("payload")
    if isinstance(payload, str) and payload and (not title or not body):
        try:
            d = _json.loads(payload)
            cands = [d]
            if isinstance(d, dict):
                cands += [d.get("notification"), d.get("data"), d.get("aps")]
                if isinstance(d.get("data"), dict):
                    cands.append(d["data"].get("notification"))
            for c in cands:
                if not isinstance(c, dict):
                    continue
                alert = c.get("alert")
                title = title or c.get("title") or (
                    alert.get("title") if isinstance(alert, dict) else None)
                body = (body or c.get("body") or c.get("message") or c.get("text")
                        or (alert if isinstance(alert, str) else None)
                        or (alert.get("body") if isinstance(alert, dict) else None))
                icon = icon or c.get("icon") or c.get("image")
                url = (url or c.get("url") or c.get("click_action")
                       or c.get("link") or c.get("landing_url"))
        except Exception:
            pass

    if not body and svc == "pushMessaging" and isinstance(payload, str):
        body = payload
    return {"title": title, "body": body, "icon": icon, "url": url}


_STAGE_LABEL = {
    "install": "После установки",
    "registration": "После регистрации",
    "deposit": "После депозита",
    None: "Без стадии",
}


def _row_get(row, key):
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


def pack_caption(row, pushes, final: bool = False) -> str:
    """Caption for the pushes archive — same shape as the session card."""
    real = [dict(p) for p in pushes if (_row_get(p, "service") or "") != "stage"]
    by_stage: dict = {}
    for p in real:
        by_stage[p.get("stage")] = by_stage.get(p.get("stage"), 0) + 1

    head = "📦 <b>Финальный архив пушей</b>" if final else "📦 <b>Архив пушей</b>"
    lines = [
        head,
        "",
        session_card(
            _row_get(row, "pwa_name") or _row_get(row, "site_url"),
            _row_get(row, "start_url"),
            _row_get(row, "deep_link"),
            push_subscribed=bool(_row_get(row, "push_subscribed")),
        ),
        "",
    ]
    for st in ("install", "registration", "deposit", None):
        if st in by_stage:
            lines.append(f"{_STAGE_LABEL[st]}: <b>{by_stage[st]}</b>")
    lines.append(f"Всего пушей: <b>{len(real)}</b>")
    return "\n".join(lines)


def session_card(
    name: str,
    download_url: str | None,
    deep_link: str | None,
    stage_label: str | None = None,
    pushes: int | None = None,
    until: str | None = None,
    push_subscribed: bool | None = None,
) -> str:
    lines = [
        f"Название PWA: <b>{esc(name)}</b>",
        f"Ссылка на PWA: {esc(download_url or '—')}",
        f"Ссылка внутри PWA: {esc(deep_link or download_url or '—')}",
    ]
    if push_subscribed is not None:
        lines.append(
            "Push-подписка: "
            + ("✅ есть" if push_subscribed else "⚠️ нет (воронка не подписала)")
        )
    if stage_label is not None:
        lines.append(f"Стадия: <b>{esc(stage_label)}</b>")
    if pushes is not None:
        row = f"Собрано пушей: <b>{pushes}</b>"
        if until:
            row += f" · до {until}"
        lines.append(row)
    return "\n".join(lines)
