"""Administrator-requested public release checks and verified FPK downloads."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import uuid
from urllib.parse import urljoin, urlsplit

import httpx

from . import __version__
from .logs import event
from .control import atomic_text, validate_directory

REPOSITORY = 'zhjAdm/fnmusic-lx-first'
RELEASES = f'https://github.com/{REPOSITORY}/releases'
API = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
MAX_PACKAGE = 256 * 1024 * 1024
HOSTS = {'api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise ValueError('更新版本格式无效')
    return tuple(map(int, value.split('.')))


def digest(source):
    sha = hashlib.sha256()
    while block := source.read(1024 * 1024):
        sha.update(block)
    return sha.hexdigest()


def verified_file(path, expected, mode=None):
    """Hash and use one regular-file descriptor, never a later pathname target."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    source = os.fdopen(fd, 'rb')
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('安装包目标不是普通文件')
        if digest(source) != expected:
            source.close()
            return None
        after = os.fstat(fd)
        named = path.stat(follow_symlinks=False)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino)):
            raise ValueError('安装包文件已变化，请重新下载')
        if mode is not None:
            os.fchmod(fd, mode)
        source.seek(0)
        return source
    except BaseException:
        source.close()
        raise


class Updater:
    def __init__(self, config, client=None, *, arch=None, current=__version__, validate_dir=validate_directory):
        self.config = config
        self.current = current
        self.arch = arch or {'x86_64': 'x86', 'AMD64': 'x86', 'aarch64': 'arm', 'arm64': 'arm'}.get(platform.machine(), '')
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30, read=60), trust_env=False, headers={'User-Agent': 'fnmusic-lx-first'})
        self.owns_client = client is None
        self.latest = None
        self.download = {'state': 'idle', 'received': 0, 'total': 0}
        self.task = None
        self.check_lock = asyncio.Lock()
        self.download_lock = asyncio.Lock()
        self.path = config.data_dir / 'updates/update.fpk'
        self.settings_path = config.data_dir / 'update-settings.json'
        self.directory = json.loads(self.settings_path.read_text()).get('download_dir', '') if self.settings_path.exists() else ''
        self.validate_dir = validate_dir
        self.package_mode = 0o600

    def state(self):
        return {'current': self.current, 'arch': self.arch, 'release_url': RELEASES,
                'latest': self.latest, 'available': bool(self.latest and version(self.latest['version']) > version(self.current)),
                'download_dir': self.directory,
                'effective_dir': self.directory or str((self.config.data_dir / 'updates').resolve()),
                'download': dict(self.download)}

    def validate_folder(self, value):
        if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise ValueError('请输入 NAS 上的绝对目录路径，留空使用默认目录')
        if not value:
            return None
        if not value.startswith('/') or '..' in Path(value).parts:
            raise ValueError('请输入 NAS 上的绝对目录路径')
        try:
            return self.validate_dir(value)
        except (OSError, RuntimeError):
            raise ValueError('安装包目录不可用，请检查存储卷和写入权限') from None

    async def save_directory(self, value):
        async with self.download_lock:
            if self.task and not self.task.done():
                raise ValueError('安装包正在下载，请完成后再修改目录')
            folder = await asyncio.to_thread(self.validate_folder, value)
            directory = str(folder) if folder is not None else ''
            try:
                atomic_text(self.settings_path, json.dumps({'download_dir': directory}, ensure_ascii=False) + '\n')
            except OSError:
                raise ValueError('无法保存安装包目录，请检查扩展数据目录权限') from None
            self.directory = directory
            return self.state()

    async def open(self, url):
        for _ in range(6):
            parts = urlsplit(url)
            if parts.scheme != 'https' or parts.hostname not in HOSTS or parts.port not in (None, 443) or parts.username or parts.password:
                raise ValueError('更新下载地址不受信任')
            response = await self.client.send(self.client.build_request('GET', url), stream=True, follow_redirects=False)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get('location')
                await response.aclose()
                if not location:
                    raise ValueError('更新下载跳转无效')
                url = urljoin(url, location)
                continue
            try:
                response.raise_for_status()
            except BaseException:
                await response.aclose()
                raise
            return response
        raise ValueError('更新下载跳转次数过多')

    async def small(self, url, limit):
        response = await self.open(url)
        data = bytearray()
        try:
            async for block in response.aiter_bytes():
                data.extend(block)
                if len(data) > limit:
                    raise ValueError('更新信息超过大小限制')
            return bytes(data)
        finally:
            await response.aclose()

    async def check(self):
        async with self.check_lock:
            if self.arch not in ('x86', 'arm'):
                raise ValueError('当前设备架构暂不支持在线更新')
            try:
                data = json.loads(await self.small(API, 256 * 1024))
                if not isinstance(data, dict) or data.get('draft') or data.get('prerelease'):
                    raise ValueError('更新版本不可用')
                tag = data.get('tag_name', '')
                if not isinstance(tag, str) or not tag.startswith('v'):
                    raise ValueError('更新版本格式无效')
                number = tag[1:]
                version(number)
                name = f'fnmusic-lx-first-{number}-{self.arch}.fpk'
                assets = data.get('assets', [])
                if not isinstance(assets, list) or not {name, name + '.sha256'}.issubset({a.get('name') for a in assets if isinstance(a, dict) and isinstance(a.get('name'), str)}):
                    raise ValueError('此版本缺少当前架构安装包或SHA256文件')
                self.latest = {'version': number, 'tag': tag, 'filename': name,
                               'notes': str(data.get('body') or '')[:10000],
                               'url': RELEASES + '/tag/' + tag}
            except (httpx.HTTPError, ValueError) as exc:
                event(self.config.data_dir, 'updates', 'check.failed', level='WARNING', error=exc)
                raise ValueError('无法检查更新，请稍后重试或打开GitHub版本页') from exc
            return self.state()

    async def start_download(self):
        async with self.download_lock:
            if self.task and not self.task.done():
                return self.state()
            if not self.latest or version(self.latest['version']) <= version(self.current):
                raise ValueError('请先检查更新，目前没有可下载的新版本')
            release = dict(self.latest)
            folder = await asyncio.to_thread(self.validate_folder, self.directory)
            self.path = folder / release['filename'] if folder is not None else self.config.data_dir / 'updates/update.fpk'
            if self.path.is_symlink():
                raise ValueError('安装包目标文件不能是符号链接，请使用其他目录')
            self.package_mode = 0o644 if folder is not None else 0o600
            self.download = {'state': 'downloading', 'received': 0, 'total': 0, 'version': release['version']}
            self.task = asyncio.create_task(self.fetch(release))
            return self.state()

    async def fetch(self, release):
        temporary = None
        response = None
        try:
            base = RELEASES + '/download/' + release['tag'] + '/'
            name = release['filename']
            checksum = (await self.small(base + name + '.sha256', 1024)).decode('ascii').strip()
            match = re.fullmatch(r'([a-fA-F0-9]{64})[ \t]+\*?' + re.escape(name), checksum)
            if not match:
                raise ValueError('安装包校验文件无效')
            expected = match.group(1).lower()
            folder = self.path.parent
            if not self.directory:
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                folder.chmod(0o700)
            cached = await asyncio.to_thread(verified_file, self.path, expected, self.package_mode)
            if cached is not None:
                with cached:
                    size = os.fstat(cached.fileno()).st_size
                self.download.update(state='ready', filename=name, path=str(self.path), sha256=expected, received=size, total=size)
                return
            response = await self.open(base + name)
            total = int(response.headers.get('content-length') or 0)
            if total < 0 or total > MAX_PACKAGE or response.status_code != 200:
                raise ValueError('安装包响应大小或状态无效')
            self.download['total'] = total
            temporary = folder / (uuid.uuid4().hex + '.part')
            sha = hashlib.sha256()
            count = 0
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as output:
                async for block in response.aiter_bytes(1024 * 1024):
                    count += len(block)
                    if count > MAX_PACKAGE:
                        raise ValueError('安装包超过大小限制')
                    sha.update(block)
                    output.write(block)
                    self.download['received'] = count
                output.flush()
                os.fsync(output.fileno())
                if not count or (total and count != total) or sha.hexdigest() != expected:
                    raise ValueError('安装包SHA256校验失败或下载不完整')
                actual = os.fstat(output.fileno())
                named = temporary.stat(follow_symlinks=False)
                if (named.st_dev, named.st_ino) != (actual.st_dev, actual.st_ino):
                    raise ValueError('安装包临时文件已变化，请重新下载')
                os.fchmod(output.fileno(), self.package_mode)
                temporary.replace(self.path)
                named = self.path.stat(follow_symlinks=False)
                if (named.st_dev, named.st_ino) != (actual.st_dev, actual.st_ino):
                    raise ValueError('安装包文件已变化，请重新下载')
            self.download.update(state='ready', filename=name, path=str(self.path), sha256=expected, received=count, total=count)
            event(self.config.data_dir, 'updates', 'download.completed')
        except asyncio.CancelledError:
            self.download.update(state='error', error='下载已取消，请重新下载')
            raise
        except Exception as exc:
            self.download.update(state='error', error='下载或校验失败，请重试或从GitHub版本页下载')
            event(self.config.data_dir, 'updates', 'download.failed', level='WARNING', error=exc)
        finally:
            if response:
                await response.aclose()
            if temporary:
                temporary.unlink(missing_ok=True)

    def file(self):
        if self.download['state'] != 'ready' or self.path.is_symlink() or not self.path.is_file():
            raise ValueError('暂无已校验的安装包，请先下载更新')
        return self.path, self.download['filename']

    def open_file(self):
        path, name = self.file()
        try:
            source = verified_file(path, self.download['sha256'])
        except (OSError, ValueError):
            raise ValueError('安装包文件已变化，请重新下载') from None
        if source is None:
            raise ValueError('安装包文件已变化，请重新下载')
        return source, name

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.owns_client:
            await self.client.aclose()
