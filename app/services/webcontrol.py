from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets

from aiohttp import WSMsgType, web

log = logging.getLogger(__name__)

_PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
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
 #bar input{font-size:16px;min-height:44px;padding:8px 10px;border:0;border-radius:8px;
  background:#333;color:#eee;flex:1;min-width:100px}
 /* 16px avoids iOS Safari's auto-zoom-on-focus for small inputs; kept even
    though pinch-zoom is no longer blocked, since that auto-zoom is still
    an unwanted jump on every tap. */
 #stage{flex:1;position:relative;overflow:hidden;min-height:0;background:#000}
 #screen{width:100%;height:100%;object-fit:contain;touch-action:none;cursor:crosshair}
 #loading{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  color:#8c8;font:14px system-ui;pointer-events:none}
 #loading::before{content:'';width:22px;height:22px;margin-right:10px;border-radius:50%;
  border:2.5px solid #2a2a2a;border-top-color:#8c8;animation:spin .8s linear infinite}
 @keyframes spin{to{transform:rotate(360deg)}}
 #st{color:#8c8;padding:2px 6px;align-self:center}
 /* invisible proxy input: the streamed page is just pixels on a <canvas>,
    so tapping a form field in it can't focus anything real and iOS never
    shows a keyboard. Tapping the ⌨️ button focuses THIS instead — a real,
    on-screen (if invisible) input — which does trigger the keyboard; its
    keystrokes get relayed to the actual session. pointer-events:none so
    it never itself intercepts a tap meant for the canvas underneath. */
 #kbd{position:absolute;left:0;bottom:0;width:1px;height:1px;padding:0;border:0;
  opacity:0;font-size:16px;pointer-events:none}
</style></head><body><div id=wrap>
<div id=bar>
 <button onclick="nav('back')">◀</button>
 <button onclick="nav('reload')">⟳</button>
 <input id=url placeholder="https://…" onkeydown="if(event.key==='Enter')go()">
 <button onclick="go()">GO</button>
 <button onclick="kbd.focus()" title="Показать клавиатуру">⌨️</button>
 <span id=st>connecting…</span>
</div>
<div id=stage><canvas id=screen></canvas><div id=loading>Ожидание кадра…</div>
 <input id=kbd autocomplete=off autocapitalize=off autocorrect=off spellcheck=false></div>
</div><script>
const cv=document.getElementById('screen'),cx=cv.getContext('2d'),st=document.getElementById('st'),
      loading=document.getElementById('loading'),kbd=document.getElementById('kbd');
let fw=390,fh=844,ws,img=new Image(),first=true;
img.onload=()=>{cv.width=fw;cv.height=fh;cx.drawImage(img,0,0,fw,fh);
 if(first){first=false;loading.style.display='none'}};
function connect(){
 ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+location.pathname+'/ws');
 ws.onopen=()=>st.textContent='live';
 ws.onclose=()=>{st.textContent='reconnecting…';setTimeout(connect,1500)};
 ws.onmessage=e=>{const m=JSON.parse(e.data);
  if(m.t==='frame'){if(m.w){fw=m.w;fh=m.h}img.src='data:image/jpeg;base64,'+m.d}
  else if(m.t==='info'){st.textContent=m.s}};
}
function pt(e){
 // canvas element now fills #stage (object-fit:contain draws the actual
 // frame letterboxed inside it) — map through the letterboxed content box,
 // not the element's full bounding box, or clicks near the edges land on
 // the wrong pixel (or in the empty margin) once the aspect ratios differ.
 const r=cv.getBoundingClientRect();
 const scale=Math.min(r.width/fw,r.height/fh);
 const dispW=fw*scale,dispH=fh*scale;
 const offX=r.left+(r.width-dispW)/2,offY=r.top+(r.height-dispH)/2;
 return{x:Math.round((e.clientX-offX)/scale),y:Math.round((e.clientY-offY)/scale)};
}
function sendMouse(type,e,btn){const p=pt(e);ws&&ws.send(JSON.stringify(
 {t:'mouse',type,x:p.x,y:p.y,button:btn||'left'}))}
cv.addEventListener('pointerdown',e=>{cv.setPointerCapture(e.pointerId);sendMouse('mousePressed',e)});
cv.addEventListener('pointerup',e=>sendMouse('mouseReleased',e));
cv.addEventListener('pointermove',e=>{if(e.buttons)sendMouse('mouseMoved',e)});
cv.addEventListener('wheel',e=>{e.preventDefault();const p=pt(e);
 ws&&ws.send(JSON.stringify({t:'wheel',x:p.x,y:p.y,dx:e.deltaX,dy:e.deltaY}))},{passive:false});
addEventListener('keydown',e=>{if(e.target.id==='url'||e.target.id==='kbd')return;e.preventDefault();
 ws&&ws.send(JSON.stringify({t:'key',type:'keyDown',key:e.key,code:e.code,
  keyCode:e.keyCode,text:e.key.length===1?e.key:''}))});
addEventListener('keyup',e=>{if(e.target.id==='url'||e.target.id==='kbd')return;
 ws&&ws.send(JSON.stringify({t:'key',type:'keyUp',key:e.key,code:e.code,keyCode:e.keyCode}))});
// mobile virtual keyboards often don't fire clean keydown/keyup (autocorrect,
// predictive text, IME composition) — 'input' with e.data is what actually
// carries the typed character reliably there; Enter/Backspace still need
// real key events since they're not "inserted text".
kbd.addEventListener('input',e=>{
 if(e.inputType==='deleteContentBackward'){
  ws&&ws.send(JSON.stringify({t:'key',type:'keyDown',key:'Backspace',code:'Backspace',keyCode:8}));
  ws&&ws.send(JSON.stringify({t:'key',type:'keyUp',key:'Backspace',code:'Backspace',keyCode:8}));
 }else if(e.data){
  ws&&ws.send(JSON.stringify({t:'text',text:e.data}));
 }
 kbd.value='';
});
kbd.addEventListener('keydown',e=>{
 if(e.key==='Enter'){
  e.preventDefault();
  ws&&ws.send(JSON.stringify({t:'key',type:'keyDown',key:'Enter',code:'Enter',keyCode:13,text:'\r'}));
  ws&&ws.send(JSON.stringify({t:'key',type:'keyUp',key:'Enter',code:'Enter',keyCode:13}));
 }
});
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
    elif t == "text" and m.get("text"):
        # mobile virtual keyboards (autocorrect/predictive text/IME) don't
        # give clean per-keystroke key events — insert the composed text
        # from the proxy input's own 'input' event directly instead
        bridge.send_input([{"method": "Input.insertText", "params": {"text": m["text"]}}])
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
