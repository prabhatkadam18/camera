"""Secure web UI: login-gated live view (with audio), pan/tilt, and recorded clips.

Only this port is exposed (over Tailscale). go2rtc runs bound to localhost and is
reached only through the authenticated /mse WebSocket proxy, so the video channel
is gated by the same login. Installable as a PWA (manifest + service worker).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
import websockets
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

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

    # -- auth --------------------------------------------------------------
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


def build(cfg: Config) -> FastAPI:
    state = App(cfg)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        threading.Thread(target=state.ptz.connect, daemon=True).start()  # ~fast handshake replay
        yield
        state.ptz.close()

    app = FastAPI(lifespan=lifespan)

    def require(request: Request):
        if not state.authed(request):
            raise HTTPException(status_code=401, detail="login required")

    @app.get("/login", response_class=HTMLResponse)
    def login_page():
        return (STATIC / "login.html").read_text()

    @app.post("/login")
    async def login(request: Request):
        body = await request.json()
        if not cfg.web_password or body.get("password") != cfg.web_password:
            return JSONResponse({"ok": False}, status_code=401)
        resp = JSONResponse({"ok": True})
        state.issue(resp)
        return resp

    @app.post("/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("session")
        return resp

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if not state.authed(request):
            return HTMLResponse('<meta http-equiv="refresh" content="0; url=/login">')
        return (STATIC / "index.html").read_text()

    @app.post("/app/ptz/{direction}")
    def ptz(direction: str, request: Request, steps: int = 1):
        require(request)
        try:
            state.ptz.pan(direction, max(1, min(steps, 10)))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "direction": direction, "steps": steps}

    @app.get("/app/events")
    def events(request: Request):
        require(request)
        d = cfg.clips_dir
        items = []
        if d.exists():
            for f in sorted(d.glob("*.mp4"), reverse=True)[:100]:
                st = f.stat()
                thumb = f.with_suffix(".jpg")
                items.append({"name": f.name, "size": st.st_size, "mtime": int(st.st_mtime),
                              "thumb": thumb.name if thumb.exists() else None})
        return {"events": items}

    @app.get("/clips/{name}")
    def clip(name: str, request: Request):
        require(request)
        f = (cfg.clips_dir / name).resolve()
        if f.parent != cfg.clips_dir.resolve() or not f.exists():
            raise HTTPException(status_code=404)
        from fastapi.responses import FileResponse
        return FileResponse(f)

    # -- PWA ---------------------------------------------------------------
    @app.get("/manifest.webmanifest")
    def manifest():
        return JSONResponse({
            "name": "Backyard Camera", "short_name": "Camera", "start_url": "/",
            "display": "standalone", "background_color": "#101014", "theme_color": "#101014",
            "icons": [
                {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            ],
        })

    @app.get("/sw.js")
    def sw():
        return PlainTextResponse((STATIC / "sw.js").read_text(), media_type="application/javascript")

    @app.get("/static/{name}")
    def static(name: str):
        f = (STATIC / name).resolve()
        if f.parent != STATIC.resolve() or not f.exists():
            raise HTTPException(status_code=404)
        from fastapi.responses import FileResponse
        return FileResponse(f)

    # -- live view: authenticated WS proxy to localhost go2rtc -------------
    @app.websocket("/mse")
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

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    if not cfg.web_password:
        log.warning("WEB_PASSWORD is not set in .env — login will reject everyone. Set it before use.")
    uvicorn.run(build(cfg), host="0.0.0.0", port=cfg.web_port, log_level="warning")


if __name__ == "__main__":
    main()
