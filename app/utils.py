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


def session_card(
    name: str,
    download_url: str | None,
    deep_link: str | None,
    stage_label: str | None = None,
    pushes: int | None = None,
    until: str | None = None,
) -> str:
    lines = [
        f"Название PWA: <b>{esc(name)}</b>",
        f"Ссылка на PWA: {esc(download_url or '—')}",
        f"Ссылка внутри PWA: {esc(deep_link or download_url or '—')}",
    ]
    if stage_label is not None:
        lines.append(f"Стадия: <b>{esc(stage_label)}</b>")
    if pushes is not None:
        row = f"Собрано пушей: <b>{pushes}</b>"
        if until:
            row += f" · до {until}"
        lines.append(row)
    return "\n".join(lines)
