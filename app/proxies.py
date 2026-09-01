from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_SCHEME = "socks5"


def load_proxies(path: str) -> list[dict]:
    """Read the configurable proxy list (proxies.json)."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("server"):
            continue
        server = _normalize_server(str(item["server"]))
        entry = {
            "name": item.get("name") or _redacted(server),
            "server": server,
            "username": item.get("username") or "",
            "password": item.get("password") or "",
        }
        # optional cheap proxy to hold the collecting session on
        # ("direct" = drop the proxy entirely once subscribed)
        if item.get("hold"):
            h = str(item["hold"]).strip()
            entry["hold"] = "direct" if h.lower() == "direct" else _normalize_server(h)
        out.append(entry)
    return out


def parse_proxy_string(text: str) -> dict | None:
    """Parse `scheme://user:pass@host:port` (scheme optional -> socks5)."""
    text = text.strip()
    if "://" not in text:
        text = f"{DEFAULT_SCHEME}://" + text
    u = urlparse(text)
    if not u.hostname or not u.port:
        return None
    return {
        "name": f"{u.hostname}:{u.port}",
        "server": f"{u.scheme}://{u.hostname}:{u.port}",
        "username": u.username or "",
        "password": u.password or "",
    }


def _normalize_server(server: str) -> str:
    return server if "://" in server else f"{DEFAULT_SCHEME}://{server}"


def _redacted(server: str) -> str:
    at = server.rfind("@")
    if at == -1:
        return server
    scheme = server.split("://", 1)[0]
    return f"{scheme}://***@{server[at + 1:]}"


def pproxy_upstream(proxy: dict) -> str:
    """Build the upstream URI for pproxy's local forwarder.

    pproxy expects credentials as a URL fragment: ``scheme://host:port#user:pass``.
    The source creds may live inside ``server`` (``socks5://user:pass@host:port``)
    or in separate ``username`` / ``password`` fields.
    """
    server = _normalize_server(proxy["server"])
    u = urlparse(server)
    user = u.username or proxy.get("username") or ""
    pw = u.password or proxy.get("password") or ""
    base = f"{u.scheme}://{u.hostname}:{u.port}"
    return f"{base}#{user}:{pw}" if (user or pw) else base
