"""Desktop administrator UI; bind only to the private fnOS gateway Unix socket."""

import argparse
from contextlib import asynccontextmanager
import json
from pathlib import Path
import httpx
from fastapi import FastAPI, APIRouter, Request, HTTPException
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
import uvicorn
from .config import Config, load_env
from .control import Settings, ProxyControl
from .lx import LX
from .logs import event, collect, format_text
import asyncio
from .library import Library
from .updates import Updater

STATIC = Path(__file__).parent / "static"
BASE = "/app/fnmusic-lx-first"


def create_manager(config, env_path, control=None, lx=None, validate_dir=None):
    client = None
    if lx is None:
        client = httpx.AsyncClient(base_url=config.lx_url, timeout=30, trust_env=False)
        lx = LX(client, config.lx_password, config.platforms)
    kwargs = {"validate_dir": validate_dir} if validate_dir else {}
    settings = Settings(
        config, env_path, control or ProxyControl(config.data_dir), lx, **kwargs
    )
    library = Library(config)
    updater = Updater(config)
    scan_task = None

    def start_scan():
        nonlocal scan_task
        if scan_task is None or scan_task.done():

            async def run():
                try:
                    await library.scan()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    event(
                        config.data_dir,
                        "library",
                        "scan.failed",
                        level="ERROR",
                        error=exc,
                    )

            scan_task = asyncio.create_task(run())

    @asynccontextmanager
    async def lifespan(app):
        event(config.data_dir, "manager", "service.started")
        try:
            if library.read()["auto_scan"] and library.read()["directories"]:
                start_scan()
        except (OSError, ValueError) as exc:
            event(
                config.data_dir, "library", "settings.invalid", level="ERROR", error=exc
            )
        try:
            yield
        finally:
            if scan_task:
                scan_task.cancel()
                await asyncio.gather(scan_task, return_exceptions=True)
            await updater.close()
            await settings.close()
            if client:
                await client.aclose()
            event(config.data_dir, "manager", "service.stopped")

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.library = library
    app.state.updater = updater

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
        operation = request.url.path.removeprefix(BASE)
        mutations = {
            "/api/source": "source.upload",
            "/api/settings": "settings.save",
            "/api/takeover": "takeover.change",
            "/api/tools/download": "tools.download",
            "/api/update/check": "update.check",
            "/api/update/download": "update.download",
            "/api/library": "library.save",
            "/api/library/scan": "library.scan",
            "/api/library/cancel": "library.cancel",
        }
        try:
            response = await call_next(request)
        except Exception as exc:
            event(
                config.data_dir, "manager", "request.failed", level="ERROR", error=exc
            )
            raise
        if request.method == "POST" and operation in mutations:
            event(
                config.data_dir,
                "manager",
                mutations[operation],
                level="ERROR"
                if response.status_code >= 500
                else "WARNING"
                if response.status_code >= 400
                else "INFO",
                status=response.status_code,
            )
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=422)

    @app.exception_handler(LookupError)
    async def not_ready(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.exception_handler(httpx.HTTPError)
    async def upstream_error(request, exc):
        event(config.data_dir, "manager", "upstream.failed", level="ERROR", error=exc)
        return JSONResponse(
            {"error": "上游服务不可用，请查看运行日志后重试"}, status_code=503
        )

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

    @router.post("/api/tools/download")
    async def download_tools(request: Request):
        await body(request)
        return await settings.download_tools()

    @router.get("/api/library")
    async def library_state():
        return JSONResponse(library.state(), headers={"Cache-Control": "no-store"})

    @router.get("/api/library/songs")
    async def library_songs(q: str = "", page: int = 1, size: int = 25):
        result = await asyncio.to_thread(library.songs, q, page, size)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post("/api/library")
    async def save_library(request: Request):
        data = await body(request)
        result = library.save(data.get("directories"), data.get("auto_scan"))
        return result

    @router.post("/api/library/scan")
    async def scan_library(request: Request):
        await body(request)
        if not library.read()["directories"]:
            raise ValueError("请先设置本地音乐目录")
        start_scan()
        await asyncio.sleep(0)
        return library.state()

    @router.post("/api/library/cancel")
    async def cancel_library(request: Request):
        await body(request)
        library.cancel()
        if scan_task and not scan_task.done():
            scan_task.cancel()
            await asyncio.gather(scan_task, return_exceptions=True)
        return library.state()

    @router.get("/api/update")
    async def update_state():
        return JSONResponse(updater.state(), headers={"Cache-Control": "no-store"})

    @router.post("/api/update/check")
    async def check_update(request: Request):
        await body(request)
        return await updater.check()

    @router.post("/api/update/download")
    async def download_update(request: Request):
        await body(request)
        return await updater.start_download()

    @router.get("/api/update/file")
    async def update_file():
        path, filename = updater.file()
        return FileResponse(path, filename=filename, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})

    async def log_result(source, level, limit):
        return await collect(
            config.data_dir, source, level, limit, secrets=(config.lx_password,)
        )

    @router.get("/api/logs")
    async def logs(source: str = "events", level: str = "ALL", limit: int = 200):
        return JSONResponse(
            await log_result(source, level, limit),
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/api/logs/download")
    async def download_logs(
        source: str = "events", level: str = "ALL", limit: int = 500
    ):
        result = await log_result(source, level, limit)
        return PlainTextResponse(
            format_text(result),
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="fnmusic-{source}.log"',
            },
        )

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
    try:
        main()
    except Exception as exc:
        import os

        event(
            Path(os.environ.get("DATA_DIR", "data")),
            "manager",
            "process.failed",
            level="ERROR",
            error=exc,
        )
        raise
