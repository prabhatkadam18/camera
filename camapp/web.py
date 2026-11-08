"""Secure web UI: login-gated live view (with audio), pan/tilt, and recorded clips.

Only this port is exposed (over Tailscale). go2rtc runs bound to localhost and is
reached only through the authenticated /mse WebSocket proxy, so the video channel
is gated by the same login. Installable as a PWA (manifest + service worker).

Built on Starlette (pure-Python, no compiled deps) so it installs cleanly on Termux.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from pathlib import Path

import uvicorn
import websockets
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from .config import Config, load_config
from .ptz import PTZController

log = logging.getLogger("web")
STATIC = Path(__file__).resolve().parent / "static"
SESSION_MAX_AGE = 7 * 24 * 3600


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.signer = URLSafeTimedSerializer(cfg.secret_key, salt="session")
        self.ptz = PTZController()
        self.go2rtc_ws = cfg.go2rtc_url.replace("http", "ws", 1) + "/api/ws"

    def issue(self, resp: Response) -> None:
        token = self.signer.dumps({"ok": True})
        resp.set_cookie("session", token, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax")

    def authed(self, request: Request) -> bool:
        tok = request.cookies.get("session")
        if not tok:
            return False
        try:
            self.signer.loads(tok, max_age=SESSION_MAX_AGE)
            return True
        except BadSignature:
            return False


def build(cfg: Config) -> Starlette:
    state = App(cfg)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        threading.Thread(target=state.ptz.connect, daemon=True).start()  # ~fast handshake replay
        yield
        state.ptz.close()

    def require(request: Request) -> None:
        if not state.authed(request):
            raise HTTPException(status_code=401, detail="login required")

    async def login(request: Request):
        if request.method == "GET":
            return HTMLResponse((STATIC / "login.html").read_text())
        body = await request.json()
        if not cfg.web_password or body.get("password") != cfg.web_password:
            return JSONResponse({"ok": False}, status_code=401)
        resp = JSONResponse({"ok": True})
        state.issue(resp)
        return resp

    async def logout(request: Request):
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("session")
        return resp

    async def index(request: Request):
        if not state.authed(request):
            return HTMLResponse('<meta http-equiv="refresh" content="0; url=/login">')
        return HTMLResponse((STATIC / "index.html").read_text())

    async def ptz(request: Request):
        require(request)
        direction = request.path_params["direction"]
        try:
            steps = int(request.query_params.get("steps", "1"))
        except ValueError:
            steps = 1
        try:
            state.ptz.pan(direction, max(1, min(steps, 10)))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return JSONResponse({"ok": True, "direction": direction, "steps": steps})

    async def events(request: Request):
        require(request)
        d = cfg.clips_dir
        items = []
        if d.exists():
            for f in sorted(d.glob("*.mp4"), reverse=True)[:100]:
                st = f.stat()
                thumb = f.with_suffix(".jpg")
                items.append({"name": f.name, "size": st.st_size, "mtime": int(st.st_mtime),
                              "thumb": thumb.name if thumb.exists() else None})
        return JSONResponse({"events": items})

    async def clip(request: Request):
        require(request)
        f = (cfg.clips_dir / request.path_params["name"]).resolve()
        if f.parent != cfg.clips_dir.resolve() or not f.exists():
            raise HTTPException(status_code=404)
        return FileResponse(f)

    async def manifest(request: Request):
        return JSONResponse({
            "name": "Backyard Camera", "short_name": "Camera", "start_url": "/",
            "display": "standalone", "background_color": "#101014", "theme_color": "#101014",
            "icons": [
                {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            ],
        })

    async def sw(request: Request):
        return PlainTextResponse((STATIC / "sw.js").read_text(), media_type="application/javascript")

    async def static_file(request: Request):
        f = (STATIC / request.path_params["name"]).resolve()
        if f.parent != STATIC.resolve() or not f.exists():
            raise HTTPException(status_code=404)
        return FileResponse(f)

    async def mse(ws: WebSocket):
        # WebSocket auth: check the session cookie before upgrading traffic.
        tok = ws.cookies.get("session")
        try:
            state.signer.loads(tok or "", max_age=SESSION_MAX_AGE)
        except BadSignature:
            await ws.close(code=1008)
            return
        await ws.accept()
        src = ws.query_params.get("src", "camera")
        upstream_url = f"{state.go2rtc_ws}?src={src}"
        try:
            async with websockets.connect(upstream_url, max_size=None) as up:
                async def c2u():
                    try:
                        while True:
                            await up.send(await ws.receive_text())
                    except Exception:  # noqa: BLE001 — client closed or disconnected
                        pass

                async def u2c():
                    try:
                        async for msg in up:
                            if isinstance(msg, bytes):
                                await ws.send_bytes(msg)
                            else:
                                await ws.send_text(msg)
                    except Exception:  # noqa: BLE001 — upstream closed
                        pass

                # When either side ends, cancel the other instead of sending after close.
                tasks = {asyncio.create_task(c2u()), asyncio.create_task(u2c())}
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
        except Exception as e:  # noqa: BLE001
            log.warning("mse proxy error: %s", e)
        with contextlib.suppress(Exception):
            await ws.close()

    routes = [
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/", index, methods=["GET"]),
        Route("/app/ptz/{direction}", ptz, methods=["POST"]),
        Route("/app/events", events, methods=["GET"]),
        Route("/clips/{name}", clip, methods=["GET"]),
        Route("/manifest.webmanifest", manifest, methods=["GET"]),
        Route("/sw.js", sw, methods=["GET"]),
        Route("/static/{name}", static_file, methods=["GET"]),
        WebSocketRoute("/mse", mse),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    if not cfg.web_password:
        log.warning("WEB_PASSWORD is not set in .env — login will reject everyone. Set it before use.")
    uvicorn.run(build(cfg), host="0.0.0.0", port=cfg.web_port, log_level="warning")


if __name__ == "__main__":
    main()
