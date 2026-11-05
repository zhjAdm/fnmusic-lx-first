"""Download a checksum-pinned LX runtime, configure it, or import a private script."""

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import platform
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "v2.1.2"
ARCHIVE = f"lx-music-sync-server-{VERSION}-server.zip"
SHA256 = "1f89639366a7d0f968bfb0e56d88701a60d500a555afd52ae2cc7e5ff4742a63"
URL = f"https://github.com/XCQ0607/lxserver/releases/download/{VERSION}/{ARCHIVE}"


def native(root, target=None):
    machine = {
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86_64": "x64",
        "AMD64": "x64",
    }.get(platform.machine())
    system = {"Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    if not machine or not system:
        raise RuntimeError("Native runtime supports Linux/macOS x64/ARM64 only")
    suffix = target or f"{system}-{machine}" + ("-gnu" if system == "linux" else "")
    if suffix not in {"linux-arm64-gnu", "linux-x64-gnu", "darwin-arm64", "darwin-x64"}:
        raise RuntimeError("Unsupported native target")
    package = f"music-tag-native-{suffix}"
    module = Path(root) / "node_modules" / package
    if (module / "package.json").exists():
        if json.loads((module / "package.json").read_text()).get("version") != "0.2.5":
            raise RuntimeError("Native package version mismatch")
        return
    with urllib.request.urlopen(
        f"https://registry.npmjs.org/{package}/0.2.5", timeout=30
    ) as response:
        metadata = json.load(response)
    url = metadata["dist"]["tarball"]
    if not url.startswith("https://registry.npmjs.org/"):
        raise RuntimeError("Unexpected native package host")
    with urllib.request.urlopen(url, timeout=60) as response:
        archive = response.read(30 * 1024 * 1024 + 1)
    if len(archive) > 30 * 1024 * 1024:
        raise RuntimeError("Native package exceeds size limit")
    algorithm, expected = metadata["dist"]["integrity"].split("-", 1)
    if (
        algorithm != "sha512"
        or base64.b64encode(hashlib.sha512(archive).digest()).decode() != expected
    ):
        raise RuntimeError("Native package integrity mismatch")
    with tempfile.TemporaryDirectory(dir=Path(root) / "node_modules") as temp:
        temp = Path(temp)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            entries = bundle.getmembers()
            if sum(e.size for e in entries) > 100 * 1024 * 1024:
                raise RuntimeError("Native package too large")
            if any(
                not (temp / e.name).resolve().is_relative_to(temp.resolve())
                or not (e.isfile() or e.isdir())
                for e in entries
            ):
                raise RuntimeError("Unsafe native package entry")
            bundle.extractall(temp, filter="data")
        shutil.move(str(temp / "package"), module)


def expose_capabilities(root):
    path = Path(root) / "server/server/customSourceHandlers.js"
    content = path.read_text()
    marker = "// fnmusic-lx-first: expose registered qualitys without script contents"
    if marker in content:
        return
    anchor = (
        "        const status = (0, userApi_1.getApiStatus)(source.owner, source.id);"
    )
    if content.count(anchor) != 1:
        raise RuntimeError("LX capability patch anchor mismatch")
    patch = """        // fnmusic-lx-first: expose registered qualitys without script contents
        const running = (0, userApi_1.getLoadedApis)().find(api => api.owner === source.owner && api.id === source.id);
        source.sources = {};
        if (running) {
            for (const [platform, info] of Object.entries(running.sources || {})) {
                source.sources[platform] = { qualitys: Array.isArray(info.qualitys) ? info.qualitys : [] };
            }
        }
"""
    path.write_text(content.replace(anchor, patch + anchor))


def prepare(root):
    root = Path(root).resolve()
    if (root / ".version").exists():
        if (root / ".version").read_text() != VERSION:
            raise RuntimeError("Runtime version mismatch")
        expose_capabilities(root)
        return
    if root.exists() and any(root.iterdir()):
        raise RuntimeError("Runtime directory occupied")
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root.parent) as temp:
        temp = Path(temp)
        archive = temp / ARCHIVE
        digest = hashlib.sha256()
        with (
            urllib.request.urlopen(URL, timeout=60) as response,
            archive.open("wb") as output,
        ):
            total = 0
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > 250 * 1024 * 1024:
                    raise RuntimeError("Runtime archive exceeds size limit")
                digest.update(block)
                output.write(block)
        if digest.hexdigest() != SHA256:
            raise RuntimeError("Runtime SHA256 mismatch")
        unpack = temp / "unpack"
        unpack.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            total = 0
            for entry in bundle.infolist():
                path = (unpack / entry.filename).resolve()
                if (
                    not path.is_relative_to(unpack.resolve())
                    or ((entry.external_attr >> 16) & 0o170000) == 0o120000
                ):
                    raise RuntimeError("Unsafe archive entry")
                total += entry.file_size
                if total > 1024 * 1024 * 1024:
                    raise RuntimeError("Unpacked runtime exceeds size limit")
            bundle.extractall(unpack)
        entries = [
            p
            for p in unpack.rglob("index.js")
            if (p.parent / "package.json").is_file() and "node_modules" not in p.parts
        ]
        if len(entries) != 1:
            raise RuntimeError("Unexpected runtime archive structure")
        if root.exists():
            root.rmdir()
        shutil.move(str(entries[0].parent), root)
        expose_capabilities(root)
        (root / ".version").write_text(VERSION)


def configure(path, password, port):
    if len(password) < 16:
        raise ValueError("LX_PASSWORD must have at least 16 characters")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError(
            "Config already exists; edit it locally rather than overwriting"
        )
    config = {
        "serverName": "fnmusic-lx-first",
        "bindIP": "127.0.0.1",
        "port": port,
        "frontend.password": password,
        "player.enableAuth": True,
        "player.password": password,
        "subsonic.enable": False,
        "webdav.enable": False,
        "system.allowUnsafeVM": False,
        "user.enablePublicRestriction": True,
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(
            "module.exports = "
            + json.dumps(config, ensure_ascii=False, indent=2)
            + ";\n"
        )


async def import_source(path):
    import httpx

    from fnmusic.config import Config, load_env
    from fnmusic.lx import LX

    if Path(".env").is_file():
        load_env(".env")
    config = Config.from_env()
    async with httpx.AsyncClient(
        base_url=config.lx_url, timeout=30, trust_env=False
    ) as client:
        lx = LX(client, config.lx_password, config.platforms)
        await lx.import_script(path)
        print("Private source imported; no script content written to project files")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    download = sub.add_parser("prepare")
    download.add_argument("--dir", default="data/runtime/lxserver")
    binding = sub.add_parser("native")
    binding.add_argument("--dir", default="data/runtime/lxserver")
    config = sub.add_parser("configure")
    config.add_argument("--path", default="data/lxserver/config.js")
    config.add_argument("--port", type=int, default=8005)
    source = sub.add_parser("import")
    source.add_argument("path")
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            prepare(args.dir)
            print("Pinned LX runtime ready")
        elif args.action == "native":
            native(args.dir)
            print("Native LX binding ready")
        elif args.action == "configure":
            from fnmusic.config import load_env

            if Path(".env").is_file():
                load_env(".env")
            configure(args.path, os.environ.get("LX_PASSWORD", ""), args.port)
            print("Private LX config created")
        else:
            asyncio.run(import_source(args.path))
    except Exception as exc:
        # HTTP errors can embed URLs containing credentials; print the type only.
        parser.exit(
            1,
            f"Runtime operation failed ({type(exc).__name__}); inspect configuration locally.\n",
        )


if __name__ == "__main__":
    main()
