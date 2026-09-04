from __future__ import annotations

import asyncio
import logging
import socket

import pproxy
from pproxy.server import stream_handler as _pproxy_stream_handler

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
    same so the browser keeps working. `asyncio.Server.close()` only stops
    NEW connections though; a connection accepted before the swap (e.g.
    Chrome's already-open HTTPS CONNECT tunnel to a site it visited while on
    the old upstream) is left running and would otherwise keep relaying
    through the stale upstream indefinitely — the browser has no reason to
    open a fresh connection for an origin it still holds a live tunnel to.
    So `swap()` also force-closes every connection accepted so far.
    """

    def __init__(self, upstream: str) -> None:
        self.upstream = upstream
        self._handler = None
        self._port: int | None = None
        self.url: str | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def _tracked_handler(self, reader, writer, **kw) -> None:
        # NOTE: pproxy's stream_handler spawns the actual relay as background
        # tasks (asyncio.ensure_future) and returns almost immediately after
        # the handshake — it does NOT stay alive for the connection's real
        # lifetime. So this writer is deliberately never removed here; only
        # _close_active_connections() (called on swap/stop) clears the set,
        # which is exactly when we want to force-close everything regardless
        # of whether it happens to still be open.
        self._writers.add(writer)
        await _pproxy_stream_handler(reader, writer, **kw)

    async def _bind(self) -> None:
        server = pproxy.Server(f"http://127.0.0.1:{self._port}/")
        # upstream == "direct" -> pproxy makes direct connections (no proxy)
        rservers = [] if self.upstream in ("direct", "", None) \
            else [pproxy.Connection(self.upstream)]
        self._handler = await server.start_server(
            {"rserver": rservers, "verbose": _verbose},
            stream_handler=self._tracked_handler,
        )

    async def start(self) -> str:
        self._port = _free_port()
        await self._bind()
        self.url = f"http://127.0.0.1:{self._port}"
        log.info("local proxy %s -> %s", self.url, _redact(self.upstream))
        return self.url

    def _close_active_connections(self) -> None:
        n = len(self._writers)
        for w in list(self._writers):
            try:
                w.close()
            except Exception:
                pass
        self._writers.clear()
        if n:
            log.info("local proxy %s dropped %d live connection(s) on swap",
                      self.url, n)

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
        self._close_active_connections()
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
        self._close_active_connections()
        if not self._handler:
            return
        self._handler.close()
        try:
            await self._handler.wait_closed()
        except Exception:
            pass
        self._handler = None
        self.url = None
