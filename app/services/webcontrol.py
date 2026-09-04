from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets

from aiohttp import WSMsgType, web

log = logging.getLogger(__name__)

_PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>Session browser</title>
<style>
 *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
 html,body{margin:0;background:#111;height:100%;height:100dvh;overflow:hidden;font:14px system-ui;overscroll-behavior:none}
 #wrap{display:flex;flex-direction:column;height:100%;height:100dvh;
  padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)}
 #bar{display:flex;gap:6px;padding:8px;background:#1c1c1c;flex-wrap:wrap;flex:0 0 auto}
 #bar button{font:15px system-ui;min-width:44px;min-height:44px;padding:8px 12px;border:0;
  border-radius:8px;background:#333;color:#eee;-webkit-user-select:none;user-select:none}
 #bar button:active{background:#444}
 #bar input{font:15px system-ui;min-height:44px;padding:8px 10px;border:0;border-radius:8px;
  background:#333;color:#eee;flex:1;min-width:100px}
 #stage{flex:1;display:flex;align-items:center;justify-content:center;overflow:hidden;min-height:0}
 #screen{max-width:100%;max-height:100%;touch-action:none;background:#000;cursor:crosshair}
 #st{color:#8c8;padding:2px 6px;align-self:center}
</style></head><body><div id=wrap>
<div id=bar>
 <button onclick="nav('back')">◀</button>
 <button onclick="nav('reload')">⟳</button>
 <input id=url placeholder="https://…" onkeydown="if(event.key==='Enter')go()">
 <button onclick="go()">GO</button>
 <span id=st>connecting…</span>
</div>
<div id=stage><canvas id=screen></canvas></div>
</div><script>
const cv=document.getElementById('screen'),cx=cv.getContext('2d'),st=document.getElementById('st');
let fw=390,fh=844,ws,img=new Image();
img.onload=()=>{cv.width=fw;cv.height=fh;cx.drawImage(img,0,0,fw,fh)};
function connect(){
 ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+location.pathname+'/ws');
 ws.onopen=()=>st.textContent='live';
 ws.onclose=()=>{st.textContent='reconnecting…';setTimeout(connect,1500)};
 ws.onmessage=e=>{const m=JSON.parse(e.data);
  if(m.t==='frame'){if(m.w){fw=m.w;fh=m.h}img.src='data:image/jpeg;base64,'+m.d}
  else if(m.t==='info'){st.textContent=m.s}};
}
function pt(e){const r=cv.getBoundingClientRect();return{
 x:Math.round((e.clientX-r.left)/r.width*fw),
 y:Math.round((e.clientY-r.top)/r.height*fh)};}
function sendMouse(type,e,btn){const p=pt(e);ws&&ws.send(JSON.stringify(
 {t:'mouse',type,x:p.x,y:p.y,button:btn||'left'}))}
cv.addEventListener('pointerdown',e=>{cv.setPointerCapture(e.pointerId);sendMouse('mousePressed',e)});
cv.addEventListener('pointerup',e=>sendMouse('mouseReleased',e));
cv.addEventListener('pointermove',e=>{if(e.buttons)sendMouse('mouseMoved',e)});
cv.addEventListener('wheel',e=>{e.preventDefault();const p=pt(e);
 ws&&ws.send(JSON.stringify({t:'wheel',x:p.x,y:p.y,dx:e.deltaX,dy:e.deltaY}))},{passive:false});
addEventListener('keydown',e=>{if(e.target.id==='url')return;e.preventDefault();
 ws&&ws.send(JSON.stringify({t:'key',type:'keyDown',key:e.key,code:e.code,
  keyCode:e.keyCode,text:e.key.length===1?e.key:''}))});
addEventListener('keyup',e=>{if(e.target.id==='url')return;
 ws&&ws.send(JSON.stringify({t:'key',type:'keyUp',key:e.key,code:e.code,keyCode:e.keyCode}))});
function nav(a){ws&&ws.send(JSON.stringify({t:'nav',a}))}
function go(){const u=document.getElementById('url').value.trim();if(u)ws&&ws.send(JSON.stringify({t:'nav',a:'open',url:u}))}
connect();
</script></body></html>"""


class WebControl:
    def __init__(self, public_url: str) -> None:
        self.public_url = public_url.rstrip("/")
        self._by_token: dict[str, dict] = {}
        self._by_session: dict[str, str] = {}
        self.app = web.Application()
        self.app.add_routes(
            [
                web.get("/b/{token}", self._page),
                web.get("/b/{token}/ws", self._ws),
                web.get("/healthz", lambda r: web.Response(text="ok")),
            ]
        )
        self._runner: web.AppRunner | None = None

    # ---- lifecycle
    async def start(self, host: str, port: int) -> None:
        # behind Caddy every request's direct peer is the proxy container —
        # show the real client IP (X-Forwarded-For) in the access log instead
        access_log_format = '%{X-Forwarded-For}i %l %u %t "%r" %s %b "%{Referer}i" "%{User-Agent}i"'
        self._runner = web.AppRunner(self.app, access_log_format=access_log_format)
        await self._runner.setup()
        await web.TCPSite(self._runner, host, port).start()
        log.info("web control listening on %s:%s (public %s)", host, port, self.public_url)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # ---- registry
    def register(self, session_id: str, name: str, bridge, on_view=None) -> str:
        self.unregister(session_id)
        token = secrets.token_urlsafe(12)
        self._by_token[token] = {
            "session_id": session_id, "name": name, "bridge": bridge,
            "on_view": on_view,
        }
        self._by_session[session_id] = token
        return token

    def unregister(self, session_id: str) -> None:
        token = self._by_session.pop(session_id, None)
        if token:
            self._by_token.pop(token, None)

    def url_for(self, session_id: str) -> str | None:
        token = self._by_session.get(session_id)
        return f"{self.public_url}/b/{token}" if token else None

    # ---- handlers
    async def _page(self, request: web.Request) -> web.Response:
        if request.match_info["token"] not in self._by_token:
            raise web.HTTPNotFound()
        return web.Response(text=_PAGE, content_type="text/html")

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        entry = self._by_token.get(request.match_info["token"])
        if not entry:
            raise web.HTTPNotFound()
        bridge = entry["bridge"]
        ws = web.WebSocketResponse(heartbeat=25)
        try:
            await ws.prepare(request)
        except web.HTTPBadRequest as e:
            # behind Caddy, request.remote is the proxy's own container IP —
            # the real client is in X-Forwarded-For
            remote = request.headers.get("X-Forwarded-For", request.remote)
            log.warning(
                "ws handshake rejected for %s (%s): %s | headers: %s",
                remote, request.headers.get("User-Agent", "?"),
                e.text, dict(request.headers),
            )
            raise
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=1)

        def sink(b64: str, meta: dict) -> None:
            frame = {
                "t": "frame", "d": b64,
                "w": int(meta.get("deviceWidth") or 0),
                "h": int(meta.get("deviceHeight") or 0),
            }
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(_offer, q, frame)

        on_view = entry.get("on_view")
        if on_view:
            # awaited: the proxy attach + page-restore this triggers must
            # finish before we start streaming, or the first frames can show
            # a stale/parked page (see SessionManager._on_ctl_view)
            with contextlib.suppress(Exception):
                await on_view(True)
        bridge.set_frame_sink(sink)
        sender = asyncio.create_task(_pump(ws, q))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                with contextlib.suppress(Exception):
                    _dispatch(bridge, msg.json())
        finally:
            sender.cancel()
            bridge.set_frame_sink(None)
            if on_view:
                with contextlib.suppress(Exception):
                    await on_view(False)
        return ws


def _offer(q: asyncio.Queue, frame: dict) -> None:
    if q.full():
        with contextlib.suppress(asyncio.QueueEmpty):
            q.get_nowait()
    with contextlib.suppress(asyncio.QueueFull):
        q.put_nowait(frame)


async def _pump(ws: web.WebSocketResponse, q: asyncio.Queue) -> None:
    while not ws.closed:
        frame = await q.get()
        with contextlib.suppress(Exception):
            await ws.send_json(frame)


_MOUSE = {"mousePressed", "mouseReleased", "mouseMoved"}


def _dispatch(bridge, m: dict) -> None:
    t = m.get("t")
    if t == "mouse" and m.get("type") in _MOUSE:
        p = {"type": m["type"], "x": m["x"], "y": m["y"]}
        if m["type"] != "mouseMoved":
            p.update(button=m.get("button", "left"), clickCount=1)
        else:
            p["button"] = "none"
        bridge.send_input([{"method": "Input.dispatchMouseEvent", "params": p}])
    elif t == "wheel":
        bridge.send_input([{
            "method": "Input.dispatchMouseEvent",
            "params": {"type": "mouseWheel", "x": m["x"], "y": m["y"],
                       "deltaX": m.get("dx", 0), "deltaY": m.get("dy", 0)},
        }])
    elif t == "key" and m.get("type") in ("keyDown", "keyUp"):
        p = {
            "type": m["type"],
            "key": m.get("key", ""),
            "code": m.get("code", ""),
            "windowsVirtualKeyCode": m.get("keyCode", 0),
            "nativeVirtualKeyCode": m.get("keyCode", 0),
        }
        if m.get("text"):
            p["text"] = m["text"]
        bridge.send_input([{"method": "Input.dispatchKeyEvent", "params": p}])
    elif t == "nav":
        a = m.get("a")
        if a == "open" and m.get("url"):
            bridge.send_input([{"method": "Page.navigate", "params": {"url": m["url"]}}])
        elif a == "reload":
            bridge.send_input([{"method": "Page.reload", "params": {}}])
        elif a == "back":
            # best-effort history back
            bridge.send_input([{
                "method": "Runtime.evaluate",
                "params": {"expression": "history.back()"},
            }])
