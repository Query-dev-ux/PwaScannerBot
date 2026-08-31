from __future__ import annotations

import logging
import socket

import pproxy

log = logging.getLogger(__name__)


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _redact(uri: str) -> str:
    return uri.split("#", 1)[0] + ("#***" if "#" in uri else "")


class LocalProxy:
    """Unauthenticated local HTTP proxy that forwards to an upstream proxy.

    Chromium cannot authenticate against SOCKS proxies (and prompts awkwardly for
    HTTP ones), so the browser is pointed at 127.0.0.1 and this forwarder carries
    the real credentials on to the upstream (`socks5://user:pass@host:port`).
    One instance per browser session; lifetime tied to the session.
    """

    def __init__(self, upstream: str) -> None:
        self.upstream = upstream
        self._handler = None
        self.url: str | None = None

    async def start(self) -> str:
        port = _free_port()
        server = pproxy.Server(f"http://127.0.0.1:{port}/")
        remote = pproxy.Connection(self.upstream)

        def _verbose(*args, **_kwargs):
            if args and isinstance(args[0], str):
                log.debug("pproxy: %s", args[0])

        self._handler = await server.start_server(
            {"rserver": [remote], "verbose": _verbose}
        )
        self.url = f"http://127.0.0.1:{port}"
        log.info("local proxy %s -> %s", self.url, _redact(self.upstream))
        return self.url

    async def stop(self) -> None:
        if not self._handler:
            return
        self._handler.close()
        try:
            await self._handler.wait_closed()
        except Exception:
            pass
        self._handler = None
        self.url = None
