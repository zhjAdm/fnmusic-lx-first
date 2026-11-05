"""Reuse existing audio tools; install a verified private pair only on request."""

import asyncio
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import tarfile
import tempfile
import time
import wave

import httpx

OFFICIAL_DIRS = tuple(
    Path("/var/apps/trim.music/target") / suffix
    for suffix in ("bin", "ffmpeg/bin", "ffmpeg", "server/bin", "")
) + (Path("/usr/trim/bin"), Path("/usr/lib/ffmpeg"))
MAX_ARCHIVE = 200 * 1024 * 1024
MAX_UNPACKED = 512 * 1024 * 1024


class MissingTool(RuntimeError):
    pass


def download_asset():
    machine = platform.machine().lower()
    if platform.system() != "Linux" or machine not in (
        "x86_64",
        "amd64",
        "aarch64",
        "arm64",
    ):
        raise ValueError("按需下载仅支持 Linux x86_64 / ARM64")
    arch = "x86" if machine in ("x86_64", "amd64") else "arm"
    assets = json.loads(Path(__file__).with_name("ffmpeg-assets.json").read_text())
    return assets[arch], 62 if arch == "x86" else 183


def sample_audio():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 800)
    return output.getvalue()


async def compatible(name, path):
    # Exercise the actual pipe/AAC/ADTS path, rather than trusting a version string.
    args = (
        [
            "-nostdin",
            "-v",
            "error",
            "-protocol_whitelist",
            "pipe",
            "-i",
            "pipe:0",
            "-vn",
            "-c:a",
            "aac",
            "-f",
            "adts",
            "pipe:1",
        ]
        if name == "ffmpeg"
        else ["-v", "error", "-show_streams", "-show_format", "-of", "json", "pipe:0"]
    )
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            str(path),
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        result, _ = await asyncio.wait_for(process.communicate(sample_audio()), 8)
        return (
            process.returncode == 0
            and bool(result)
            and (
                result.startswith(b"\xff")
                if name == "ffmpeg"
                else bool(json.loads(result).get("streams"))
            )
        )
    except (OSError, ValueError, TimeoutError):
        return False
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()


async def unpack(archive, destination, machine):
    total = 0
    seen = set()
    with tarfile.open(archive) as bundle:
        for member in bundle:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or member.issym()
                or member.islnk()
            ):
                raise ValueError("FFmpeg 压缩包路径无效")
            total += max(member.size, 0)
            if total > MAX_UNPACKED:
                raise ValueError("FFmpeg 解压体积超过限制")
            binary = path.name in ("ffmpeg", "ffprobe") and path.parent.name == "bin"
            license_file = path.name.lower().startswith(
                ("license", "copying", "readme")
            )
            if not member.isfile() or not (binary or license_file):
                continue
            if member.size > (180 * 1024 * 1024 if binary else 1024 * 1024):
                raise ValueError("FFmpeg 文件超过大小限制")
            relative = Path("bin") / path.name if binary else Path(path.name)
            if str(relative) in seen:
                raise ValueError("FFmpeg 压缩包包含重复文件")
            seen.add(str(relative))
            target = destination / relative
            target.parent.mkdir(exist_ok=True)
            source = bundle.extractfile(member)
            with source, target.open("xb") as output:
                if binary:
                    header = source.read(20)
                    if (
                        len(header) < 20
                        or header[:6] != b"\x7fELF\x02\x01"
                        or int.from_bytes(header[18:20], "little") != machine
                    ):
                        raise ValueError("FFmpeg 程序架构不匹配")
                    output.write(header)
                while block := source.read(65536):
                    output.write(block)
                    # Keep the manager responsive and allow shutdown to cancel
                    # extraction before publishing any component directory.
                    await asyncio.sleep(0)
            target.chmod(0o700 if binary else 0o600)
    if not {"bin/ffmpeg", "bin/ffprobe"} <= seen:
        raise ValueError("FFmpeg 压缩包缺少音频组件")


class MediaTools:
    def __init__(self, config):
        self.directory = config.data_dir / "media-tools"
        self.cache = None
        self.checked = 0
        self.progress = {"state": "idle", "received": 0, "total": 0, "error": ""}
        self.lock = asyncio.Lock()
        self.discovery_lock = asyncio.Lock()

    async def status(self, refresh=False):
        async with self.discovery_lock:
            if refresh or self.cache is None or time.monotonic() - self.checked >= 30:
                found = {}
                directories = [Path(p) for p in os.get_exec_path() if p]
                directories += list(OFFICIAL_DIRS) + [self.directory / "bin"]
                for name in ("ffmpeg", "ffprobe"):
                    for directory in dict.fromkeys(directories):
                        candidate = directory / name
                        if (
                            candidate.is_file()
                            and os.access(candidate, os.X_OK)
                            and await compatible(name, candidate)
                        ):
                            found[name] = str(candidate.absolute())
                            break
                self.cache = {
                    name: found.get(name, "") for name in ("ffmpeg", "ffprobe")
                }
                self.cache["ready"] = all(self.cache.values())
                self.checked = time.monotonic()
            return {**self.cache, "download": self.progress.copy()}

    async def require(self, name):
        status = await self.status()
        if not status.get(name):
            raise MissingTool("缺少兼容的 FFmpeg 音频组件，请在扩展管理页面下载")
        return status[name]

    async def install(self, client=None):
        async with self.lock:
            if (await self.status(refresh=True))["ready"]:
                return
            self.progress = {
                "state": "downloading",
                "received": 0,
                "total": 0,
                "error": "",
            }
            try:
                asset, machine = download_asset()
                self.progress["total"] = asset.get("size", 0)
                self.directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tempfile.TemporaryDirectory(
                    prefix=".media-", dir=self.directory.parent
                ) as folder:
                    folder = Path(folder)
                    archive = folder / "download.tar.xz"
                    if client is None:
                        async with httpx.AsyncClient(
                            follow_redirects=True,
                            timeout=httpx.Timeout(30, read=60),
                            trust_env=False,
                        ) as owned:
                            await self.fetch(owned, asset, archive)
                    else:
                        await self.fetch(client, asset, archive)
                    self.progress["state"] = "checking"
                    payload = folder / "payload"
                    payload.mkdir(mode=0o700)
                    await unpack(archive, payload, machine)
                    for name in ("ffmpeg", "ffprobe"):
                        if not await compatible(name, payload / "bin" / name):
                            raise ValueError("下载的 FFmpeg 未通过音频功能检查")
                    # Existing healthy copies were returned above. Publish a complete
                    # replacement and roll back even if rename fails.
                    backup = folder / "previous"
                    if self.directory.exists():
                        self.directory.rename(backup)
                    try:
                        payload.rename(self.directory)
                    except BaseException:
                        if backup.exists():
                            backup.rename(self.directory)
                        raise
                self.cache = None
                self.progress["state"] = "done"
            except asyncio.CancelledError:
                self.progress.update(state="idle", error="下载已中断，可重新下载")
                raise
            except Exception:
                self.progress.update(
                    state="error",
                    error="下载或校验失败，请检查 NAS 到 GitHub 的网络后重试",
                )
                raise

    async def fetch(self, client, asset, target):
        digest = hashlib.sha256()
        async with asyncio.timeout(900):
            async with client.stream("GET", asset["url"]) as response:
                response.raise_for_status()
                if int(response.headers.get("content-length") or 0) > MAX_ARCHIVE:
                    raise ValueError("FFmpeg 下载超过大小限制")
                with target.open("xb") as output:
                    target.chmod(0o600)
                    async for chunk in response.aiter_bytes(65536):
                        self.progress["received"] += len(chunk)
                        if self.progress["received"] > MAX_ARCHIVE:
                            raise ValueError("FFmpeg 下载超过大小限制")
                        digest.update(chunk)
                        output.write(chunk)
        if digest.hexdigest() != asset["sha256"]:
            raise ValueError("FFmpeg 下载校验失败")
