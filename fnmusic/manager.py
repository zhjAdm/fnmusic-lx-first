"""Desktop administrator UI; bind only to the private fnOS gateway Unix socket."""

import argparse
from contextlib import asynccontextmanager
import json
from pathlib import Path
import httpx
from fastapi import FastAPI, APIRouter, Request, HTTPException
from starlette.responses import FileResponse, JSONResponse
import uvicorn
from .config import Config, load_env
from .control import Settings, ProxyControl
from .lx import LX

STATIC = Path(__file__).parent / "static"
BASE = "/app/fnmusic-lx-first"


def create_manager(config, env_path, control=None, lx=None, validate_dir=None):
    client = None
    if lx is None:
        client = httpx.AsyncClient(base_url=config.lx_url, timeout=30, trust_env=False)
        lx = LX(client, config.lx_password, config.platforms)
    kwargs = {"validate_dir": validate_dir} if validate_dir else {}
    settings = Settings(config, env_path, control or ProxyControl(), lx, **kwargs)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await settings.close()
            if client:
                await client.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings

    @app.middleware("http")
    async def administrator(request, call_next):
        if request.headers.get("x-trim-isadmin", "").lower() != "true":
            return JSONResponse(
                {"error": "请从飞牛桌面使用管理员账号打开扩展管理"}, status_code=403
            )
        if request.method not in ("GET", "HEAD"):
            if request.headers.get(
                "x-fnmusic-request"
            ) != "1" or not request.headers.get("content-type", "").startswith(
                "application/json"
            ):
                return JSONResponse({"error": "请求来源校验失败"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"error": "不接受跨站请求"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=422)

    @app.exception_handler(LookupError)
    async def not_ready(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.exception_handler(httpx.HTTPError)
    async def upstream_error(request, exc):
        return JSONResponse({"error": "音源服务不可用，请稍后重试"}, status_code=503)

    async def body(request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 3 * 1024 * 1024:
                raise HTTPException(413, "上传文件过大")
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            raise ValueError("请求内容无效") from None
        if not isinstance(value, dict):
            raise ValueError("请求内容无效")
        return value

    router = APIRouter()

    @router.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @router.get("/admin.css")
    async def css():
        return FileResponse(STATIC / "admin.css", media_type="text/css")

    @router.get("/admin.js")
    async def js():
        return FileResponse(STATIC / "admin.js", media_type="text/javascript")

    @router.get("/api/state")
    async def state():
        return await settings.state()

    @router.post("/api/source")
    async def source(request: Request):
        data = await body(request)
        return await settings.upload(data.get("name"), data.get("content"))

    @router.post("/api/settings")
    async def save(request: Request):
        data = await body(request)
        return await settings.save_settings(
            data.get("download_dir"), data.get("play_mode")
        )

    @router.post("/api/takeover")
    async def takeover(request: Request):
        data = await body(request)
        return await settings.takeover(data.get("enabled"))

    app.include_router(router)
    app.include_router(router, prefix=BASE)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True)
    parser.add_argument("--socket", required=True)
    args = parser.parse_args()
    load_env(args.env)
    config = Config.from_env()
    # The socket directory is application-owned and only root may connect. The fnOS
    # desktop gateway supplies the admin header; this service never opens a TCP port.
    import os
    import socket
    import fcntl
    import stat

    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (config.data_dir / "manager.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    path = Path(args.socket)
    journal = config.data_dir / "manager-listener.json"
    if path.is_symlink():
        parser.exit(1, "Manager socket is a symlink; preserving it.\n")
    if path.exists():
        known = json.loads(journal.read_text()) if journal.exists() else {}
        original = path.stat()
        if (
            not stat.S_ISSOCK(original.st_mode)
            or known.get("identity") != [original.st_dev, original.st_ino]
            or known.get("path") != str(path.absolute())
        ):
            parser.exit(1, "Unknown manager endpoint; preserving it.\n")
        with socket.socket(socket.AF_UNIX) as probe:
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                path.unlink()
            else:
                parser.exit(1, "Manager already running.\n")
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    os.chmod(path, 0o600)
    info = path.stat()
    identity = [info.st_dev, info.st_ino]
    from .control import atomic_text

    atomic_text(
        journal, json.dumps({"path": str(path.absolute()), "identity": identity})
    )
    listener.listen(128)
    listener.setblocking(False)
    app = create_manager(config, args.env)
    original_lifespan = app.router.lifespan_context

    def clean():
        if (
            path.exists()
            and not path.is_symlink()
            and [path.stat().st_dev, path.stat().st_ino] == identity
        ):
            path.unlink()
            journal.unlink(missing_ok=True)

    @asynccontextmanager
    async def owned_lifespan(application):
        try:
            async with original_lifespan(application):
                yield
        finally:
            clean()

    app.router.lifespan_context = owned_lifespan
    try:
        uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning")).run(
            sockets=[listener]
        )
    finally:
        listener.close()
        clean()
        lock.close()


if __name__ == "__main__":
    main()
