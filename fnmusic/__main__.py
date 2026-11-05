import argparse
from contextlib import asynccontextmanager
import json
import os
import socket
import stat
from pathlib import Path

import uvicorn

from .app import create_app
from .config import Config, load_env
from .logs import event


def main():
    parser = argparse.ArgumentParser(
        description="Start the independent extension listener; does not take over official Music"
    )
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    if Path(args.env).exists():
        load_env(args.env)
    config = Config.from_env()
    path = Path(config.listen_socket)
    journal = config.data_dir / "listener.json"
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        parser.exit(1, "Listener is a symlink; preserving endpoint.\n")
    if path.exists():
        recorded = json.loads(journal.read_text()) if journal.exists() else {}
        st = path.stat()
        if (
            recorded.get("path") != str(path.absolute())
            or recorded.get("identity") != [st.st_dev, st.st_ino]
            or not stat.S_ISSOCK(st.st_mode)
        ):
            parser.exit(1, "Unknown listener exists; preserving endpoint.\n")
        with socket.socket(socket.AF_UNIX) as probe:
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                path.unlink()
            else:
                parser.exit(1, "Listener already active.\n")
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(path))
        permission_source = next(
            (
                p
                for p in [
                    Path(config.upstream_socket),
                    Path("/var/run/trim_music.socket"),
                ]
                if p.exists()
            ),
            None,
        )
        if permission_source:
            original = permission_source.stat()
            os.chmod(path, stat.S_IMODE(original.st_mode))
            if os.geteuid() == 0:
                os.chown(path, original.st_uid, original.st_gid)
        else:
            os.chmod(path, 0o600)
        st = path.stat()
        owned = [st.st_dev, st.st_ino]
        journal.write_text(
            json.dumps({"path": str(path.absolute()), "identity": owned})
        )
        sock.listen(128)
        sock.setblocking(False)
        app = create_app(config)
        application_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def owned_lifespan(application):
            try:
                async with application_lifespan(application):
                    yield
            finally:
                # Uvicorn re-raises SIGTERM after lifespan shutdown, before run() returns.
                if (
                    path.exists()
                    and not path.is_symlink()
                    and [path.stat().st_dev, path.stat().st_ino] == owned
                ):
                    path.unlink(missing_ok=True)
                    journal.unlink(missing_ok=True)

        app.router.lifespan_context = owned_lifespan
        server = uvicorn.Server(
            uvicorn.Config(app, access_log=False, log_level="warning")
        )
        server.run(sockets=[sock])
    finally:
        sock.close()
        if (
            path.exists()
            and not path.is_symlink()
            and [path.stat().st_dev, path.stat().st_ino] == locals().get("owned")
        ):
            path.unlink(missing_ok=True)
            journal.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        event(
            Path(os.environ.get("DATA_DIR", "data")),
            "proxy",
            "process.failed",
            level="ERROR",
            error=exc,
        )
        raise
