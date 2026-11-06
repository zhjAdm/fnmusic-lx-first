"""Private FPK staging and an installer that outlives the management service."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
import tarfile
import time
import uuid

from .control import atomic_text

INSTALLER = '/usr/local/bin/appcenter-cli'
SYSTEMD_RUN = '/usr/bin/systemd-run'
SHA256SUM = '/usr/bin/sha256sum'
MAX_PACKAGE = 256 * 1024 * 1024
ACTIVE = {'downloading', 'preparing', 'queued', 'installing', 'verifying'}


class SelfUpgrade:
    def __init__(self, config, *, current, arch):
        self.folder = config.data_dir.resolve() / 'self-update'
        self.current, self.arch = current, arch
        self.running = False
        self.supported = bool(
            config.install_root and platform.system() == 'Linux' and os.geteuid() == 0
            and all(os.access(path, os.X_OK) for path in (INSTALLER, SYSTEMD_RUN, '/bin/bash', SHA256SUM))
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
            result.update(state=phase, version=data['version'], started=data['started'])
            if phase != 'error' and self.current == data['version']:
                result['state'] = 'done'
            elif phase in {'queued', 'installing', 'verifying'} and time.time() - data.get('submitted', data['started']) > 1800:
                result.update(state='error', error='升级等待超时，请在飞牛应用中心检查应用状态')
            elif phase in {'downloading', 'preparing'} and not self.running:
                result.update(state='error', error='升级准备被中断，请重新下载或手动升级')
            elif phase == 'error':
                result['error'] = '升级未完成，请查看运行日志并在飞牛应用中心检查应用状态'
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

    def prepare(self, source, expected):
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
        atomic_text(self.job / 'run.sh', self.script(expected))
        return self.job

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

    def script(self, expected):
        # Only fixed executable paths and shell-quoted, private paths are used.
        job = shlex.quote(str(self.job))
        return f'''#!/bin/bash
set -euo pipefail
umask 077
cd -- {job}
status() {{ printf '%s\\n' "$1" > status.next; mv -f -- status.next status; }}
failed() {{ code=$?; printf '%s\\n' "$code" > exit-code; status error; }}
trap failed EXIT
trap 'exit 143' TERM INT
status installing
actual=$({shlex.quote(SHA256SUM)} < package.fpk)
[[ "${{actual%% *}}" == {shlex.quote(expected)} ]]
{shlex.quote(INSTALLER)} install-fpk {shlex.quote(str(self.job / 'package.fpk'))} >/dev/null 2>&1
status verifying
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
                '--property=RuntimeMaxSec=900', '--property=TimeoutStopSec=30',
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
