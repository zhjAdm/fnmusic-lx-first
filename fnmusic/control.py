"""Configuration transactions and fixed service controls for the desktop manager."""

import asyncio
import anyio
from functools import wraps
import json
import os
from pathlib import Path
import re
import shlex
import uuid
from .tools import MediaTools


def transaction(method):
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        task = asyncio.create_task(method(self, *args, **kwargs))
        self.transactions.add(task)

        def finished(done):
            self.transactions.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        with anyio.CancelScope(shield=True):
            return await asyncio.shield(task)

    return wrapped


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def validate_directory(value):
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or ".." in Path(value).parts
    ):
        raise ValueError("请输入 NAS 上的绝对目录路径")
    path = Path(value).resolve()
    if not re.fullmatch(r"/vol[0-9]+/.+", str(path)):
        raise ValueError("下载目录必须位于 /vol 开头的存储卷中")
    volume = Path("/" + path.parts[1])
    if not volume.is_dir() or not os.path.ismount(volume):
        raise ValueError("存储卷不存在或未挂载，请检查下载目录")
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for parent in reversed(missing):
        parent.mkdir()
        parent.chmod(0o755)
    if not path.is_dir():
        raise ValueError("下载位置不是文件夹")
    probe = path / (".fnmusic-check-" + uuid.uuid4().hex)
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except OSError:
        raise ValueError("下载目录不可写") from None
    finally:
        probe.unlink(missing_ok=True)
    return path


class ProxyControl:
    unit = "fnmusic-lx-proxy.service"

    async def command(self, action, *, check=True):
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            action,
            self.unit,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            code = await asyncio.wait_for(proc.wait(), 40)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise
        if check and code:
            raise ValueError("操作失败，请检查官方音乐是否运行及扩展服务状态")
        return code == 0

    async def running(self):
        return await self.command("is-active", check=False)

    async def enable(self):
        await self.command("start")

    async def disable(self):
        await self.command("stop")


class Settings:
    def __init__(self, config, env_path, control, lx, validate_dir=validate_directory):
        self.config, self.env_path, self.control, self.lx = (
            config,
            Path(env_path),
            control,
            lx,
        )
        self.path = config.data_dir / "settings.json"
        self.source_path = config.data_dir / "private-source.js"
        self.validate_dir = validate_dir
        self.lock = asyncio.Lock()
        self.transactions = set()
        self.tools = MediaTools(config)
        self.download_task = None

    async def close(self):
        with anyio.CancelScope(shield=True):
            if self.download_task:
                self.download_task.cancel()
                await asyncio.gather(self.download_task, return_exceptions=True)
            if self.transactions:
                await asyncio.gather(*list(self.transactions), return_exceptions=True)

    def read(self):
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {
            "takeover": False,
            "download_dir": "",
            "play_mode": "direct",
            "source_id": "",
            "source_name": "未配置音源",
            "source_version": "",
        }

    def write(self, data):
        atomic_text(self.path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    async def state(self):
        data = self.read()
        data["takeover"] = await self.control.running()
        data["configured"] = bool(data.get("source_id") and data.get("download_dir"))
        data["source_ready"] = False
        try:
            data["source_ready"] = (
                any([bool(await self.lx.qualities(p)) for p in self.config.platforms])
                if data.get("source_id")
                else False
            )
        except Exception:
            pass
        # Never expose script text, the runtime password, or source service URLs.
        result = {
            k: data[k]
            for k in (
                "takeover",
                "download_dir",
                "play_mode",
                "source_name",
                "source_version",
                "source_ready",
                "configured",
            )
        }
        result["audio_tools"] = await self.tools.status()
        return result

    async def download_tools(self):
        if not self.download_task or self.download_task.done():

            async def install():
                try:
                    await self.tools.install()
                except Exception:
                    # Error details remain in component state, without download URLs.
                    pass

            self.download_task = asyncio.create_task(install())
        return {"started": True}

    @transaction
    async def takeover(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("接管状态必须是布尔值")
        async with self.lock:
            data = self.read()
            if enabled:
                if not data.get("source_id") or not data.get("download_dir"):
                    raise LookupError("请先上传音源脚本并设置下载目录")
                if not any([await self.lx.qualities(p) for p in self.config.platforms]):
                    raise LookupError("音源尚未就绪，请先检查音源脚本")
                await self.control.enable()
            else:
                await self.control.disable()
            data["takeover"] = enabled
            self.write(data)
        return await self.state()

    @transaction
    async def save_settings(self, directory, mode):
        if mode not in ("direct", "proxy"):
            raise ValueError("播放方式无效")
        async with self.lock:
            candidate = (
                Path(directory).resolve() if isinstance(directory, str) else None
            )
            protected = [self.config.data_dir.resolve()]
            if self.config.install_root:
                protected.append(self.config.install_root.resolve())
            if candidate and any(
                candidate.is_relative_to(root) or root.is_relative_to(candidate)
                for root in protected
            ):
                raise ValueError(
                    "请选择应用安装和数据目录之外的曲库，避免升级或卸载影响音乐"
                )
            folder = self.validate_dir(directory)
            data = self.read()
            was_active = await self.control.running()
            original = self.env_path.read_text()
            if was_active:
                await self.control.disable()
            lines = [
                line
                for line in original.splitlines()
                if not line.startswith(("DOWNLOAD_DIR=", "PLAY_MODE="))
            ]
            lines += [
                "DOWNLOAD_DIR=" + shlex.quote(str(folder)),
                "PLAY_MODE=" + shlex.quote(mode),
            ]
            atomic_text(self.env_path, "\n".join(lines) + "\n")
            try:
                if was_active:
                    await self.control.enable()
            except Exception:
                atomic_text(self.env_path, original)
                data["takeover"] = False
                self.write(data)
                raise ValueError("设置保存后启动失败，已恢复原设置并关闭接管") from None
            data.update(download_dir=str(folder), play_mode=mode, takeover=was_active)
            self.write(data)
        return await self.state()

    @transaction
    async def upload(self, name, content):
        if (
            not isinstance(name, str)
            or not name.lower().endswith(".js")
            or not isinstance(content, str)
            or "globalThis.lx" not in content
            or len(content.encode()) > 2 * 1024 * 1024
        ):
            raise ValueError(
                "请选择不超过 2MB 的 LX 自定义音源 .js 文件，不支持 Ceru 插件"
            )
        async with self.lock:
            data = self.read()
            active = await self.control.running()
            old = data.copy()
            temp = self.config.data_dir / ("upload-" + uuid.uuid4().hex + ".js")
            atomic_text(temp, content)
            if active:
                await self.control.disable()
            try:
                if data.get("source_id"):
                    await self.lx.call(
                        "POST",
                        "/api/custom-source/delete",
                        json={"id": data["source_id"]},
                    )
                source_id = await self.lx.import_script(temp)
            except Exception:
                recovered = False
                if self.source_path.exists():
                    try:
                        old["source_id"] = await self.lx.import_script(self.source_path)
                        if active:
                            await self.control.enable()
                        recovered = True
                    except Exception:
                        pass
                old["takeover"] = active and recovered
                self.write(old)
                raise ValueError(
                    "音源加载失败。请确认是有效的 LX 脚本，原有设置已保留"
                ) from None
            finally:
                temp.unlink(missing_ok=True)
            atomic_text(self.source_path, content)

            def metadata(key, default):
                match = re.search(r"@" + key + r"\s+([^\r\n]+)", content)
                value = match.group(1).replace("*/", "").strip() if match else default
                return re.sub(r"https?://\S+", "[链接已隐藏]", value)[:180]

            data.update(
                source_id=source_id,
                source_name=metadata("name", "LX 自定义音源"),
                source_version=metadata("version", ""),
                takeover=False,
            )
            self.write(data)
            if active:
                try:
                    await self.control.enable()
                    data["takeover"] = True
                    self.write(data)
                except Exception:
                    raise ValueError(
                        "音源已保存，接管未能重新开启，请检查官方音乐状态"
                    ) from None
        return await self.state()
