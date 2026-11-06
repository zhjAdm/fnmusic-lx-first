"""Administrator-requested public release checks and verified FPK downloads."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import uuid
from urllib.parse import urljoin, urlsplit

import httpx

from . import __version__
from .logs import event

REPOSITORY = 'zhjAdm/fnmusic-lx-first'
RELEASES = f'https://github.com/{REPOSITORY}/releases'
API = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
MAX_PACKAGE = 256 * 1024 * 1024
HOSTS = {'api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise ValueError('更新版本格式无效')
    return tuple(map(int, value.split('.')))


def digest(path):
    sha = hashlib.sha256()
    with Path(path).open('rb') as source:
        while block := source.read(1024 * 1024):
            sha.update(block)
    return sha.hexdigest()


class Updater:
    def __init__(self, config, client=None, *, arch=None, current=__version__):
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

    def state(self):
        return {'current': self.current, 'arch': self.arch, 'release_url': RELEASES,
                'latest': self.latest, 'available': bool(self.latest and version(self.latest['version']) > version(self.current)),
                'download': dict(self.download)}

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
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            folder.chmod(0o700)
            if self.path.is_file() and await asyncio.to_thread(digest, self.path) == expected:
                self.download.update(state='ready', filename=name, sha256=expected, received=self.path.stat().st_size, total=self.path.stat().st_size)
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
            temporary.replace(self.path)
            self.download.update(state='ready', filename=name, sha256=expected, received=count, total=count)
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
        if self.download['state'] != 'ready' or not self.path.is_file():
            raise ValueError('暂无已校验的安装包，请先下载更新')
        return self.path, self.download['filename']

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.owns_client:
            await self.client.aclose()
