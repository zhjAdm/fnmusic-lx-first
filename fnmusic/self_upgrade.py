"""Private FPK staging and an installer that outlives the management service."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import time
import uuid

from .control import atomic_text
from .upgrade_worker import DAEMON_SOCKET

SYSTEMD_RUN = '/usr/bin/systemd-run'
MAX_PACKAGE = 256 * 1024 * 1024
ACTIVE = {'downloading', 'preparing', 'queued', 'installing', 'verifying', 'unknown'}


class SelfUpgrade:
    def __init__(self, config, *, current, arch):
        self.folder = config.data_dir.resolve() / 'self-update'
        self.install_root = config.install_root.resolve() if config.install_root else None
        self.data_dir = config.data_dir.resolve()
        self.current, self.arch = current, arch
        self.running = False
        self.supported = bool(
            self.install_root and platform.system() == 'Linux' and os.geteuid() == 0
            and Path(DAEMON_SOCKET).is_socket()
            and all(os.access(path, os.X_OK) for path in (SYSTEMD_RUN, '/bin/bash', self.install_root / 'runtime/python/bin/python3'))
            and self.volume(self.install_root) and self.volume(self.data_dir)
        )

    def state(self):
        result = {'supported': self.supported, 'state': 'idle'}
        try:
            data = json.loads((self.folder / 'current.json').read_text())
            if not re.fullmatch('[a-f0-9]{32}', data['job']):
                raise ValueError('Invalid job')
            phase = (self.folder / data['job'] / 'status').read_text().strip()
            if phase not in ACTIVE | {'error'}:
                raise ValueError('Invalid phase')
            if (phase in {'queued', 'installing', 'verifying'}
                    and time.time() - data.get('submitted', data['started']) > 1800
                    and (self.folder / data['job'] / 'submitted').exists()):
                phase = 'unknown'
            result.update(state=phase, version=data['version'], started=data['started'])
            from .updates import version
            if phase in {'verifying', 'unknown'} and version(self.current) >= version(data['version']):
                result['state'] = 'done'
                if not self.cleanup_packages(self.folder / data['job']):
                    result['cleanup_error'] = '升级已完成，但安装包清理失败，将稍后重试'
            elif phase in {'queued', 'installing', 'verifying'} and time.time() - data.get('submitted', data['started']) > 1800:
                result.update(state='error', error='升级等待超时，请在飞牛应用中心检查应用状态')
            elif phase in {'downloading', 'preparing'} and not self.running:
                result.update(state='error', error='升级准备被中断，请重新下载或手动升级')
            elif phase in {'error', 'unknown'}:
                messages = {
                    'version_not_updated': '应用中心任务已结束，但磁盘版本未更新，请使用应用中心手动升级',
                    'daemon_rejected': '应用中心拒绝升级，请检查返回代码并使用应用中心手动升级',
                    'volume_unverified': '无法确认原安装卷和数据卷，已停止升级，请使用应用中心手动升级',
                    'daemon_unavailable': '无法连接应用中心升级接口，请使用应用中心手动升级',
                }
                reason_path = self.folder / data['job'] / 'reason'
                reason = reason_path.read_text().strip() if reason_path.exists() else ''
                result['error'] = (messages.get(reason, '升级未完成，请在飞牛应用中心检查应用状态') if phase == 'error'
                                   else '升级任务结果暂时无法确认，请在飞牛应用中心检查；不会自动重复提交')
                daemon_code = self.folder / data['job'] / 'daemon-code'
                if daemon_code.exists() and re.fullmatch('[0-9]{1,10}', daemon_code.read_text().strip()):
                    result['daemon_code'] = int(daemon_code.read_text().strip())
                    if result['daemon_code']:
                        result['error'] += f'（应用中心代码 {result["daemon_code"]}）'
                code_path = self.folder / data['job'] / 'exit-code'
                if code_path.is_file():
                    code = code_path.read_text().strip()
                    if re.fullmatch(r'[0-9]{1,3}', code):
                        result['code'] = int(code)
                        result['error'] += f'（执行返回代码 {code}）'
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            result.update(state='error', error='无法读取升级状态，请在飞牛应用中心检查应用状态')
        return result

    def cleanup_packages(self, job):
        """Remove only the verified source and private package after success."""
        marker = job / 'packages-cleaned'
        if marker.exists():
            return True
        try:
            record_path = job / 'download.json'
            record = json.loads(record_path.read_text()) if record_path.exists() else None
            if record is None:
                # Older initiators saved a checksum but not the source identity.
                # Consider only the exact configured filename, then verify its hash.
                from .updates import version
                request = json.loads((job / 'request.json').read_text())
                version(request['version'])
                settings_path = self.data_dir / 'update-settings.json'
                directory = json.loads(settings_path.read_text()).get('download_dir', '') if settings_path.exists() else ''
                candidate = (Path(directory) / f'fnmusic-lx-first-{request["version"]}-{self.arch}.fpk'
                             if directory else self.data_dir / 'updates/update.fpk')
                try:
                    identity = self.file_identity(candidate.stat(follow_symlinks=False))
                    record = {'path': str(candidate), 'identity': identity, 'sha256': request['sha256']}
                except FileNotFoundError:
                    pass
            if record:
                source_path = Path(record['path'])
                try:
                    named = source_path.stat(follow_symlinks=False)
                except FileNotFoundError:
                    named = None
                if named and stat.S_ISREG(named.st_mode) and self.file_identity(named) == record['identity']:
                    from .updates import verified_file
                    source = verified_file(source_path, record['sha256'])
                    if source:
                        with source:
                            if self.file_identity(source_path.stat(follow_symlinks=False)) == record['identity']:
                                source_path.unlink()
            private = job / 'package.fpk'
            if private.is_file() and not private.is_symlink():
                private.unlink(missing_ok=True)
            atomic_text(marker, 'done\n')
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    @staticmethod
    def file_identity(value):
        return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]

    @staticmethod
    def volume(path):
        if path is None or len(path.parts) < 2:
            return None
        match = re.fullmatch(r'vol([1-9][0-9]*)', path.parts[1])
        return int(match[1]) if match else None

    def begin(self, target):
        from .updates import version

        if not self.supported:
            raise ValueError('当前环境不支持软件内升级，请使用飞牛应用中心手动升级')
        if self.state()['state'] in ACTIVE:
            raise ValueError('升级正在进行，请等待完成')
        if self.arch not in {'x86', 'arm'} or version(target) <= version(self.current):
            raise ValueError('仅支持升级到适配当前设备的新版本')
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.folder.chmod(0o700)
        self.job = self.folder / uuid.uuid4().hex
        self.job.mkdir(mode=0o700)
        self.target = target
        self.running = True
        atomic_text(self.job / 'status', 'downloading\n')
        atomic_text(self.folder / 'current.json', json.dumps({
            'job': self.job.name, 'version': target, 'previous': self.current, 'started': time.time(),
        }) + '\n')

    def fail(self):
        self.running = False
        if hasattr(self, 'job'):
            atomic_text(self.job / 'status', 'error\n')

    def prepare(self, source, expected, *, download_path=None):
        """Copy a verified descriptor into a private directory and recheck it."""
        atomic_text(self.job / 'status', 'preparing\n')
        path = self.job / 'package.fpk'
        sha, count = hashlib.sha256(), 0
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as output:
            while block := source.read(1024 * 1024):
                count += len(block)
                if count > MAX_PACKAGE:
                    raise ValueError('安装包超过大小限制')
                sha.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if not count or not re.fullmatch('[a-f0-9]{64}', expected) or sha.hexdigest() != expected:
            raise ValueError('安装包文件已变化，请重新下载')
        self.validate_package(path)
        if download_path is not None:
            original = Path(os.path.abspath(download_path))
            identity = self.file_identity(os.fstat(source.fileno()))
            if self.file_identity(original.stat(follow_symlinks=False)) != identity:
                raise ValueError('安装包文件已变化，请重新下载')
            atomic_text(self.job / 'download.json', json.dumps({
                'path': str(original), 'identity': identity, 'sha256': expected,
            }) + '\n')
        self.snapshot_runtime()
        atomic_text(self.job / 'request.json', json.dumps({
            'version': self.target, 'sha256': expected,
            'installed_file': str(self.install_root / 'code/fnmusic/__init__.py'),
            'install_volume': self.volume(self.install_root), 'data_volume': self.volume(self.data_dir),
        }) + '\n')
        atomic_text(self.job / 'run.sh', self.script())
        return self.job

    def snapshot_runtime(self):
        source = self.install_root / 'runtime/python'
        for path in source.rglob('*'):
            if path.is_symlink() and not path.resolve().is_relative_to(source.resolve()):
                raise ValueError('升级运行环境包含外部链接，请使用应用中心手动升级')
        shutil.copytree(source, self.job / 'python', symlinks=True, ignore=shutil.ignore_patterns('__pycache__'))
        atomic_text(self.job / 'worker.py', Path(__file__).with_name('upgrade_worker.py').read_text())

    def validate_package(self, path):
        fields, payload, size, seen = {}, None, 0, set()
        try:
            with tarfile.open(path, mode='r|*') as archive:
                for index, member in enumerate(archive):
                    name = member.name.removeprefix('./')
                    size += member.size
                    if index > 1024 or size > MAX_PACKAGE or Path(name).is_absolute() or '..' in Path(name).parts:
                        raise ValueError('安装包结构无效')
                    if name not in {'manifest', 'app.tgz'}:
                        continue
                    if not member.isfile() or name in seen:
                        raise ValueError('安装包结构无效')
                    seen.add(name)
                    with archive.extractfile(member) as stream:
                        if name == 'manifest':
                            if fields or member.size > 16384:
                                raise ValueError('安装包清单无效')
                            for line in stream.read().decode('utf-8').splitlines():
                                match = re.fullmatch(r'(appname|version|platform|checksum)\s*=\s*(\S+)\s*', line)
                                if match:
                                    if match[1] in fields:
                                        raise ValueError('安装包清单无效')
                                    fields[match[1]] = match[2]
                        else:
                            if payload is not None:
                                raise ValueError('安装包内容重复')
                            md5 = hashlib.md5()
                            while block := stream.read(1024 * 1024):
                                md5.update(block)
                            payload = md5.hexdigest()
            if (fields.get('appname') != 'fnmusic-lx-first' or fields.get('version') != self.target
                    or fields.get('platform') != self.arch or payload is None or fields.get('checksum') != payload):
                raise ValueError('安装包的应用、版本、架构或内容校验不匹配')
        except (tarfile.TarError, UnicodeError, OSError) as exc:
            raise ValueError('安装包格式无效') from exc

    def script(self):
        # Only fixed executable paths and shell-quoted, private paths are used.
        job = shlex.quote(str(self.job))
        return f'''#!/bin/bash
set -euo pipefail
umask 077
cd -- {job}
status() {{ printf '%s\\n' "$1" > status.next; mv -f -- status.next status; }}
failed() {{
    code=$?; printf '%s\\n' "$code" > exit-code
    phase=$(cat status)
    if [[ "$phase" != error && "$phase" != unknown ]]; then
        if [[ -f submitted ]]; then
            printf '%s\\n' worker_interrupted > reason; status unknown
        else
            printf '%s\\n' worker_launch_failed > reason; status error
        fi
    fi
}}
trap failed EXIT
trap 'exit 143' TERM INT
{shlex.quote(str(self.job / 'python/bin/python3'))} -I -S -B {shlex.quote(str(self.job / 'worker.py'))}
trap - EXIT
'''

    async def launch(self, job):
        data = json.loads((self.folder / 'current.json').read_text())
        data['submitted'] = time.time()
        atomic_text(self.folder / 'current.json', json.dumps(data) + '\n')
        atomic_text(job / 'status', 'queued\n')
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                SYSTEMD_RUN, '--quiet', '--collect', '--unit=fnmusic-lx-update-' + job.name,
                '--property=RuntimeMaxSec=1500', '--property=TimeoutStopSec=30',
                '--property=WorkingDirectory=/', '--property=UMask=0077',
                '--property=StandardOutput=null', '--property=StandardError=null',
                '/bin/bash', str(job / 'run.sh'),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            code = await asyncio.wait_for(process.wait(), 15)
            if code:
                raise ValueError('无法启动独立升级任务，请使用飞牛应用中心手动升级')
        except TimeoutError:
            # The unit may already be accepted. Keep it queued until its worker
            # reports a result or the persisted deadline expires; do not retry.
            if process:
                process.kill()
                await process.wait()
        except (OSError, ValueError):
            self.fail()
            raise ValueError('无法启动独立升级任务，请使用飞牛应用中心手动升级') from None
        finally:
            self.running = False
