from __future__ import annotations

import asyncio
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


def _verbose(*args, **_kwargs):
    if args and isinstance(args[0], str):
        log.debug("pproxy: %s", args[0])


class LocalProxy:
    """Unauthenticated local HTTP proxy that forwards to an upstream proxy.

    Chromium cannot authenticate against SOCKS proxies (and prompts awkwardly for
    HTTP ones), so the browser is pointed at 127.0.0.1 and this forwarder carries
    the real credentials on to the upstream (`socks5://user:pass@host:port`).
    One instance per browser session; lifetime tied to the session.

    The upstream can be swapped at runtime (`swap`) — the local port stays the
    same so the browser keeps working; only in-flight connections drop and
    reconnect through the new upstream.
    """

    def __init__(self, upstream: str) -> None:
        self.upstream = upstream
        self._handler = None
        self._port: int | None = None
        self.url: str | None = None

    async def _bind(self) -> None:
        server = pproxy.Server(f"http://127.0.0.1:{self._port}/")
        # upstream == "direct" -> pproxy makes direct connections (no proxy)
        rservers = [] if self.upstream in ("direct", "", None) \
            else [pproxy.Connection(self.upstream)]
        self._handler = await server.start_server(
            {"rserver": rservers, "verbose": _verbose}
        )

    async def start(self) -> str:
        self._port = _free_port()
        await self._bind()
        self.url = f"http://127.0.0.1:{self._port}"
        log.info("local proxy %s -> %s", self.url, _redact(self.upstream))
        return self.url

    async def swap(self, new_upstream: str) -> None:
        if not self._port or new_upstream == self.upstream:
            return
        old = self.upstream
        self.upstream = new_upstream
        if self._handler:
            self._handler.close()
            try:
                await self._handler.wait_closed()
            except Exception:
                pass
            self._handler = None
        last_err: Exception | None = None
        for attempt in range(5):
            try:
                await self._bind()
                log.info("local proxy %s swapped %s -> %s",
                          self.url, _redact(old), _redact(new_upstream))
                return
            except OSError as e:
                last_err = e
                await asyncio.sleep(0.5)
        # every rebind attempt failed — the local port has NO listener now.
        # Restore the old upstream so the browser isn't left completely
        # disconnected, then tell the caller the swap itself didn't happen
        # (previously this fell through silently and logged a fake success,
        # leaving Chrome pointed at a dead local proxy with no error anywhere).
        self.upstream = old
        try:
            await self._bind()
        except Exception:
            pass
        raise RuntimeError(
            f"local proxy rebind to {_redact(new_upstream)} failed: {last_err}")

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
