from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

import websocket  # websocket-client

log = logging.getLogger(__name__)

_SERVICES = ("notifications", "pushMessaging", "backgroundFetch", "backgroundSync")

FrameSink = Callable[[str, dict], None]  # (base64_jpeg, metadata)


class CdpBridge:
    """One raw CDP websocket per browser session.

    - observes `BackgroundService` push / notification events (`on_event`)
    - on demand: streams `Page.screencastFrame` to a sink and relays input
      (`Input.*`) back to the active page — i.e. a live, controllable browser.
    """

    def __init__(self, ws_url: str, on_event: Callable[[dict], None]) -> None:
        self._ws_url = ws_url
        self._on_event = on_event
        self._ws: websocket.WebSocket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._mid = 0
        self._armed: set[str] = set()
        self._page_sessions: list[str] = []
        self._frame_sink: FrameSink | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cdp-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    @property
    def _page(self) -> str | None:
        return self._page_sessions[-1] if self._page_sessions else None

    # -------- screencast / input (called from the web-control task) -----
    def set_frame_sink(self, sink: FrameSink | None) -> None:
        with self._lock:
            self._frame_sink = sink
        if sink:
            self._start_screencast()
        else:
            self._stop_screencast()

    def _start_screencast(self) -> None:
        sid = self._page
        if not sid:
            return
        self._send("Page.enable", {}, sid)
        self._send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 55, "maxWidth": 820, "maxHeight": 1700,
             "everyNthFrame": 1},
            sid,
        )

    def _stop_screencast(self) -> None:
        sid = self._page
        if sid:
            self._send("Page.stopScreencast", {}, sid)

    def send_input(self, events: list[dict]) -> None:
        sid = self._page
        if not sid:
            return
        for ev in events:
            self._send(ev["method"], ev.get("params") or {}, sid)

    # ------------------------------------------------------------------
    def _next_id(self) -> int:
        self._mid += 1
        return self._mid

    def _send(self, method: str, params: dict | None = None, session_id: str | None = None) -> None:
        if not self._ws:
            return
        msg = {"id": self._next_id(), "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        try:
            self._ws.send(json.dumps(msg))
        except Exception as e:  # noqa: BLE001
            log.warning("cdp-bridge send %s failed: %s", method, e)

    def _run(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                self._ws = websocket.create_connection(
                    self._ws_url, timeout=20, max_size=None, suppress_origin=True
                )
                self._ws.settimeout(2)
                backoff = 1
                self._armed.clear()
                self._page_sessions.clear()
                log.info("cdp-bridge connected")
                self._send("Target.setDiscoverTargets", {"discover": True})
                self._send(
                    "Target.setAutoAttach",
                    {"autoAttach": True, "flatten": True, "waitForDebuggerOnStart": False},
                )
                self._send("Target.getTargets")
                self._loop()
            except Exception as e:  # noqa: BLE001
                if self._stop.is_set():
                    break
                log.warning("cdp-bridge connection error: %s (retry in %ss)", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                try:
                    if self._ws:
                        self._ws.close()
                except Exception:
                    pass
        log.info("cdp-bridge stopped")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                if self._stop.is_set():
                    return
                raise
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            self._handle(msg)

    def _arm(self, session_id: str) -> None:
        if session_id in self._armed:
            return
        self._armed.add(session_id)
        for svc in _SERVICES:
            self._send("BackgroundService.setRecording",
                       {"shouldRecord": True, "service": svc}, session_id)
            self._send("BackgroundService.startObserving", {"service": svc}, session_id)

    def _handle(self, msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "Target.targetCreated":
            ti = params.get("targetInfo") or {}
            if ti.get("type") == "page":
                self._send("Target.attachToTarget", {"targetId": ti["targetId"], "flatten": True})
        elif method == "Target.attachedToTarget":
            ti = params.get("targetInfo") or {}
            sid = params.get("sessionId")
            if not sid:
                return
            if ti.get("type") in ("page", "service_worker"):
                self._arm(sid)
            if ti.get("type") == "page":
                self._page_sessions.append(sid)
                with self._lock:
                    active = self._frame_sink is not None
                if active:
                    self._start_screencast()
        elif method == "Target.detachedFromTarget":
            sid = params.get("sessionId")
            if sid in self._page_sessions:
                self._page_sessions.remove(sid)
        elif method == "Page.screencastFrame":
            data = params.get("data")
            meta = params.get("metadata") or {}
            target_session = msg.get("sessionId")
            self._send(
                "Page.screencastFrameAck",
                {"sessionId": params.get("sessionId", 0)},
                target_session,
            )
            with self._lock:
                sink = self._frame_sink
            if sink and data:
                try:
                    sink(data, meta)
                except Exception as e:  # noqa: BLE001
                    log.debug("frame sink error: %s", e)
        elif method == "BackgroundService.backgroundServiceEventReceived":
            ev = params.get("backgroundServiceEvent") or {}
            try:
                self._on_event(self._normalize(ev))
            except Exception as e:  # noqa: BLE001
                log.warning("cdp-bridge on_event failed: %s", e)
        elif "result" in msg and isinstance(msg["result"], dict):
            for ti in msg["result"].get("targetInfos", []) or []:
                if ti.get("type") == "page":
                    self._send("Target.attachToTarget", {"targetId": ti["targetId"], "flatten": True})

    @staticmethod
    def _normalize(ev: dict) -> dict:
        from app.utils import extract_push_fields

        f = extract_push_fields(ev)
        return {
            "ts": ev.get("timestamp") or time.time(),
            "service": ev.get("service"),
            "event": ev.get("eventName"),
            "title": f["title"],
            "body": f["body"],
            "icon": f["icon"],
            "url": f["url"],
            "raw": json.dumps(ev, ensure_ascii=False),
        }
