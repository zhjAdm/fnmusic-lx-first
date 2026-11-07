import hashlib
import io
import json
import tarfile
import subprocess
import sys

import pytest

from fnmusic.config import Config
from fnmusic.updates import Updater


def package(*, appname='fnmusic-lx-first', version='0.1.13', arch='arm', checksum=None):
    payload = b'fixture-app-payload'
    manifest = f'appname = {appname}\nversion = {version}\nplatform = {arch}\nchecksum = {checksum or hashlib.md5(payload).hexdigest()}\n'
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        for name, data in [('manifest', manifest.encode()), ('app.tgz', payload)]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def upgrade(tmp_path):
    from fnmusic.self_upgrade import SelfUpgrade

    root = tmp_path / 'installed'
    python = root / 'runtime/python/bin/python3'
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text(f'#!/bin/bash\nexec {sys.executable!r} "$@"\n')
    python.chmod(0o700)
    worker = SelfUpgrade(Config(data_dir=tmp_path, install_root=root), current='0.1.12', arch='arm')
    worker.supported = True
    return worker


@pytest.mark.parametrize('replace_source', [False, True])
def test_success_removes_only_its_own_install_packages(tmp_path, replace_source):
    worker = upgrade(tmp_path)
    data = package()
    source = tmp_path / 'download.fpk'
    source.write_bytes(data)
    worker.begin('0.1.13')
    with source.open('rb') as stream:
        job = worker.prepare(stream, hashlib.sha256(data).hexdigest(), download_path=source)
    (job / 'status').write_text('verifying\n')
    assert worker.state()['state'] == 'verifying'
    assert source.exists() and (job / 'package.fpk').exists()
    if replace_source:
        source.unlink()
        source.write_bytes(b'another-package')
    worker.current = '0.1.13'
    assert worker.state()['state'] == 'done'
    assert not (job / 'package.fpk').exists()
    assert source.exists() == replace_source
    assert (job / 'status').exists()
    assert worker.state()['state'] == 'done'


@pytest.mark.parametrize('custom', [False, True])
def test_success_cleans_package_from_upgrade_started_on_previous_version(tmp_path, custom):
    worker = upgrade(tmp_path)
    data = package()
    folder = tmp_path / ('shared' if custom else 'updates')
    folder.mkdir()
    source = folder / ('fnmusic-lx-first-0.1.13-arm.fpk' if custom else 'update.fpk')
    source.write_bytes(data)
    if custom:
        (tmp_path / 'update-settings.json').write_text(json.dumps({'download_dir': str(folder)}))
    worker.begin('0.1.13')
    job = worker.prepare(io.BytesIO(data), hashlib.sha256(data).hexdigest())
    (job / 'status').write_text('verifying\n')
    worker.current = '0.1.13'
    assert worker.state()['state'] == 'done'
    assert not source.exists()
    assert not (job / 'package.fpk').exists()
    assert (job / 'status').exists()
    assert worker.state()['state'] == 'done'


def test_upgrade_stages_private_copy_and_checks_package_identity(tmp_path):
    worker = upgrade(tmp_path)
    data = package()
    worker.begin('0.1.13')
    job = worker.prepare(io.BytesIO(data), hashlib.sha256(data).hexdigest())
    assert (job / 'package.fpk').read_bytes() == data
    assert job.stat().st_mode & 0o777 == 0o700
    assert (job / 'package.fpk').stat().st_mode & 0o777 == 0o600
    assert worker.state()['state'] == 'preparing'
    assert 'install-fpk' not in (job / 'run.sh').read_text()
    assert (job / 'worker.py').is_file()
    assert (job / 'python/bin/python3').is_file()


@pytest.mark.parametrize('changes', [
    {'appname': 'another-app'}, {'arch': 'x86'}, {'version': '0.1.14'},
    {'checksum': '0' * 32},
])
def test_upgrade_rejects_unexpected_fpk(tmp_path, changes):
    worker = upgrade(tmp_path)
    data = package(**changes)
    worker.begin('0.1.13')
    with pytest.raises(ValueError):
        worker.prepare(io.BytesIO(data), hashlib.sha256(data).hexdigest())


def test_upgrade_rejects_modified_shared_package_and_old_version(tmp_path):
    worker = upgrade(tmp_path)
    with pytest.raises(ValueError):
        worker.begin('0.1.12')
    worker.begin('0.1.13')
    with pytest.raises(ValueError):
        worker.prepare(io.BytesIO(package()), '0' * 64)


@pytest.mark.asyncio
async def test_upgrade_handoff_uses_separate_systemd_unit_and_survives_manager_restart(tmp_path, monkeypatch):
    worker = upgrade(tmp_path)
    data = package()
    worker.begin('0.1.13')
    job = worker.prepare(io.BytesIO(data), hashlib.sha256(data).hexdigest())
    calls = []
    class Process:
        returncode = 0
        async def wait(self):
            return self.returncode
    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()
    monkeypatch.setattr('fnmusic.self_upgrade.asyncio.create_subprocess_exec', spawn)
    await worker.launch(job)
    args, kwargs = calls[0]
    assert args[0].endswith('systemd-run')
    assert '--property=RuntimeMaxSec=1500' in args
    assert '--property=WorkingDirectory=/' in args
    assert '--property=StandardOutput=null' in args
    assert args[-2:] == ('/bin/bash', str(job / 'run.sh'))
    assert not kwargs.get('shell')
    with pytest.raises(ValueError):
        worker.begin('0.1.14')
    old_manager = upgrade(tmp_path)
    (job / 'status').write_text('verifying\n')
    assert old_manager.state()['state'] == 'verifying'
    old_manager.current = '0.1.13'
    assert old_manager.state()['state'] == 'done'


@pytest.mark.asyncio
async def test_upgrade_launch_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    worker = upgrade(tmp_path)
    data = package()
    worker.begin('0.1.13')
    job = worker.prepare(io.BytesIO(data), hashlib.sha256(data).hexdigest())
    class Process:
        returncode = 1
        async def wait(self):
            return 1
    async def spawn(*args, **kwargs):
        return Process()
    monkeypatch.setattr('fnmusic.self_upgrade.asyncio.create_subprocess_exec', spawn)
    with pytest.raises(ValueError):
        await worker.launch(job)
    assert worker.state()['state'] == 'error'


@pytest.mark.asyncio
async def test_only_explicit_upgrade_request_installs_after_verified_download(tmp_path, monkeypatch):
    import httpx

    data = package()
    name = 'fnmusic-lx-first-0.1.13-arm.fpk'
    def response(request):
        if request.url.path.endswith('.sha256'):
            return httpx.Response(200, text=hashlib.sha256(data).hexdigest() + '  ' + name)
        return httpx.Response(200, content=data)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        worker = upgrade(tmp_path)
        updater = Updater(Config(data_dir=tmp_path, install_root=worker.install_root), client=client, arch='arm', current='0.1.12')
        updater.installer.supported = True
        updater.latest = {'version': '0.1.13', 'tag': 'v0.1.13', 'filename': name}
        launched = []
        async def launch(job):
            launched.append(job)
            updater.installer.running = False
            (job / 'status').write_text('queued\n')
        monkeypatch.setattr(updater.installer, 'launch', launch)
        await updater.start_download()
        await updater.task
        assert not launched
        await updater.start_download(install=True)
        await updater.task
        assert len(launched) == 1
        assert updater.state()['upgrade']['state'] == 'queued'
        assert json.loads((tmp_path / 'self-update/current.json').read_text())['version'] == '0.1.13'
        await updater.close()


@pytest.mark.parametrize('code', [0, 17])
def test_shell_runner_invokes_only_fixed_install_command_and_records_result(tmp_path, monkeypatch, code):
    worker = upgrade(tmp_path)
    data = package()
    worker.begin('0.1.13')
    job = worker.prepare(io.BytesIO(data), hashlib.sha256(data).hexdigest())
    (job / 'worker.py').write_text('from pathlib import Path\nimport sys\nPath(__file__).with_name("status").write_text("verifying\\n")\nsys.exit(' + str(code) + ')\n')
    result = subprocess.run(['/bin/bash', str(job / 'run.sh')], capture_output=True)
    assert result.returncode == code
    assert worker.state()['state'] == ('verifying' if code == 0 else 'error')
    if code:
        assert worker.state()['code'] == code


def test_upgrade_deadline_and_interrupted_preparation_never_report_success(tmp_path, monkeypatch):
    worker = upgrade(tmp_path)
    worker.begin('0.1.13')
    worker.running = False
    assert worker.state()['state'] == 'error'
    (worker.job / 'status').write_text('verifying\n')
    monkeypatch.setattr('fnmusic.self_upgrade.time.time', lambda: 10**12)
    assert worker.state()['state'] == 'error'
    worker.current = '0.1.13'
    assert worker.state()['state'] == 'done'


def test_expired_submitted_task_is_unknown_and_cannot_be_retried(tmp_path, monkeypatch):
    worker = upgrade(tmp_path)
    worker.begin('0.1.13')
    (worker.job / 'status').write_text('installing\n')
    (worker.job / 'submitted').write_text('1\n')
    monkeypatch.setattr('fnmusic.self_upgrade.time.time', lambda: 10**12)
    assert worker.state()['state'] == 'unknown'
    with pytest.raises(ValueError, match='升级正在进行'):
        worker.begin('0.1.14')
