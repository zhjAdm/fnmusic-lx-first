import hashlib
import asyncio
from pathlib import Path

import httpx
import pytest

from fnmusic.config import Config
from fnmusic.updates import Updater


@pytest.mark.asyncio
async def test_update_directory_is_persisted_and_can_reset_to_default(tmp_path):
    config = Config(data_dir=tmp_path / 'state')
    folder = tmp_path / '安装包'
    folder.mkdir(mode=0o755)
    update = Updater(config, validate_dir=lambda value: Path(value).resolve())
    try:
        assert update.state()['download_dir'] == ''
        await update.save_directory(str(folder))
        assert update.state()['effective_dir'] == str(folder)
        assert folder.stat().st_mode & 0o777 == 0o755
        restored = Updater(config, validate_dir=lambda value: Path(value).resolve())
        try:
            assert restored.state()['download_dir'] == str(folder)
            await restored.save_directory('')
            assert restored.state()['effective_dir'] == str(config.data_dir / 'updates')
        finally:
            await restored.close()
    finally:
        await update.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [None, 123, '../downloads', '/vol1/../etc', '/vol1/a\n', '/vol1/' + 'a' * 4096, '/tmp/packages'])
async def test_update_directory_rejects_invalid_paths_without_changing_settings(tmp_path, value):
    update = Updater(Config(data_dir=tmp_path))
    try:
        with pytest.raises(ValueError):
            await update.save_directory(value)
        assert update.state()['download_dir'] == ''
    finally:
        await update.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['', 'checksum'])
async def test_custom_update_download_uses_release_filename_and_keeps_directory_permissions(tmp_path, fault):
    data = b'new-package'
    name = 'fnmusic-lx-first-0.1.9-x86.fpk'
    folder = tmp_path / 'shared'
    folder.mkdir(mode=0o755)
    target = folder / name
    target.write_bytes(b'old-package')
    validated = []

    def validate(value):
        validated.append(value)
        return Path(value)

    def response(request):
        if request.url.host == 'api.github.com':
            return httpx.Response(200, json={'tag_name': 'v0.1.9', 'assets': [{'name': name}, {'name': name + '.sha256'}]})
        if request.url.path.endswith('.sha256'):
            return httpx.Response(200, text=('0' * 64 if fault else hashlib.sha256(data).hexdigest()) + '  ' + name)
        return httpx.Response(200, content=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        update = Updater(Config(data_dir=tmp_path / 'state'), client=client, arch='x86', current='0.1.8', validate_dir=validate)
        await update.save_directory(str(folder))
        await update.check()
        await update.start_download()
        await update.task
        assert len(validated) == 2  # Recheck at download time, including after a restart.
        assert folder.stat().st_mode & 0o777 == 0o755
        assert not list(folder.glob('*.part'))
        if fault:
            assert target.read_bytes() == b'old-package'
            assert update.state()['download']['state'] == 'error'
        else:
            assert update.file() == (target, name)
            assert target.read_bytes() == data
            assert target.stat().st_mode & 0o777 == 0o644
            assert update.state()['download']['path'] == str(target)
            await update.save_directory('')
            assert update.file()[0] == target  # Changing the next destination preserves the last download.
        await update.close()


@pytest.mark.asyncio
async def test_update_directory_cannot_change_while_downloading(tmp_path):
    update = Updater(Config(data_dir=tmp_path))
    update.task = asyncio.create_task(asyncio.Event().wait())
    try:
        with pytest.raises(ValueError, match='下载'):
            await update.save_directory('')
    finally:
        await update.close()


@pytest.mark.asyncio
async def test_cached_package_swap_cannot_chmod_symlink_target(tmp_path, monkeypatch):
    import fnmusic.updates as module

    folder = tmp_path / 'shared'
    folder.mkdir()
    name = 'fnmusic-lx-first-0.1.9-x86.fpk'
    target = folder / name
    data = b'valid-package'
    target.write_bytes(data)
    private = tmp_path / 'private'
    private.write_bytes(b'private-data')
    private.chmod(0o600)
    original = module.digest

    def swap(source):
        result = original(source)
        target.unlink()
        target.symlink_to(private)
        return result

    monkeypatch.setattr(module, 'digest', swap)

    def response(request):
        return httpx.Response(200, text=hashlib.sha256(data).hexdigest() + '  ' + name)

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        update = Updater(Config(data_dir=tmp_path / 'state'), client=client, arch='x86', current='0.1.8', validate_dir=Path)
        await update.save_directory(str(folder))
        update.latest = {'version': '0.1.9', 'tag': 'v0.1.9', 'filename': name}
        await update.start_download()
        await update.task
        assert private.stat().st_mode & 0o777 == 0o600
        assert update.download['state'] == 'error'


def test_open_download_uses_verified_descriptor_even_if_path_is_replaced(tmp_path):
    data = b'checked-package'
    update = Updater(Config(data_dir=tmp_path), client=object())
    update.path.parent.mkdir()
    update.path.write_bytes(data)
    update.download = {'state': 'ready', 'filename': 'fixture.fpk', 'sha256': hashlib.sha256(data).hexdigest()}
    source, name = update.open_file()
    try:
        update.path.unlink()
        private = tmp_path / 'private'
        private.write_bytes(b'private-data')
        update.path.symlink_to(private)
        assert source.read() == data and name == 'fixture.fpk'
        with pytest.raises(ValueError):
            update.open_file()
    finally:
        source.close()


@pytest.mark.asyncio
async def test_partial_package_swap_cannot_chmod_symlink_target(tmp_path):
    folder = tmp_path / 'shared'
    folder.mkdir()
    private = tmp_path / 'private'
    private.write_bytes(b'private-data')
    private.chmod(0o600)
    data = b'new-package'
    name = 'fnmusic-lx-first-0.1.9-x86.fpk'

    class Swap(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield data
            part, = folder.glob('*.part')
            part.unlink()
            part.symlink_to(private)

    def response(request):
        if request.url.path.endswith('.sha256'):
            return httpx.Response(200, text=hashlib.sha256(data).hexdigest() + '  ' + name)
        return httpx.Response(200, stream=Swap())

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        update = Updater(Config(data_dir=tmp_path / 'state'), client=client, arch='x86', current='0.1.8', validate_dir=Path)
        await update.save_directory(str(folder))
        update.latest = {'version': '0.1.9', 'tag': 'v0.1.9', 'filename': name}
        await update.start_download()
        await update.task
        assert private.stat().st_mode & 0o777 == 0o600
        assert update.download['state'] == 'error'


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['', 'checksum', 'redirect', 'asset'])
async def test_public_update_checks_version_arch_and_verified_download(tmp_path, fault):
    audio = b'fixture-fpk'
    filename = 'fnmusic-lx-first-0.1.9-arm.fpk'
    base = 'https://github.com/zhjAdm/fnmusic-lx-first/releases/download/v0.1.9/'
    digest = hashlib.sha256(audio).hexdigest()
    seen = []

    def response(request):
        seen.append(request)
        assert 'authorization' not in request.headers and 'cookie' not in request.headers
        if request.url.host == 'api.github.com':
            names = [filename, filename + '.sha256'] if fault != 'asset' else []
            return httpx.Response(200, json={'tag_name': 'v0.1.9', 'draft': False, 'prerelease': False, 'body': '更新说明', 'assets': [{'name': name} for name in names]})
        if request.url.path.endswith('.sha256'):
            return httpx.Response(200, text=('0' * 64 if fault == 'checksum' else digest) + '  ' + filename)
        assert str(request.url) == base + filename
        if fault == 'redirect':
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
        return httpx.Response(200, content=audio)

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        update = Updater(Config(data_dir=tmp_path), client=client, arch='arm', current='0.1.8')
        if fault == 'asset':
            with pytest.raises(ValueError):
                await update.check()
            assert not list(tmp_path.glob('updates/*.part'))
            return
        state = await update.check()
        assert state['available'] and state['latest']['version'] == '0.1.9'
        await update.start_download()
        await update.task
        if fault:
            assert update.state()['download']['state'] == 'error'
            with pytest.raises(ValueError):
                update.file()
        else:
            path, name = update.file()
            assert name == filename and path.read_bytes() == audio
            assert path.stat().st_mode & 0o777 == 0o600
            assert update.state()['download']['state'] == 'ready'
        assert not list((tmp_path / 'updates').glob('*.part'))
        assert all(r.url.host != '127.0.0.1' for r in seen)
        await update.close()


@pytest.mark.asyncio
async def test_updates_never_download_automatically_and_reject_older_release(tmp_path):
    def response(request):
        assert request.url.host == 'api.github.com'
        return httpx.Response(200, json={'tag_name': 'v0.1.8', 'assets': [
            {'name': 'fnmusic-lx-first-0.1.8-x86.fpk'}, {'name': 'fnmusic-lx-first-0.1.8-x86.fpk.sha256'}
        ]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        update = Updater(Config(data_dir=tmp_path), client=client, arch='x86', current='0.1.9')
        state = await update.check()
        assert not state['available'] and update.task is None
        with pytest.raises(ValueError):
            await update.start_download()
        assert not (tmp_path / 'updates/update.fpk').exists()
        await update.close()


def test_release_versions_are_numeric_and_match_package_metadata():
    from fnmusic import __version__
    from fnmusic.updates import version
    from scripts.build_fpk import ROOT, VERSION
    import tomllib

    assert version('0.1.10') > version('0.1.9')
    assert __version__ == VERSION == tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    for invalid in ('../../evil', '0.1.9-beta', '01.1.9', 'v0.1.9'):
        with pytest.raises(ValueError):
            version(invalid)


@pytest.mark.asyncio
async def test_oversized_package_keeps_previous_cache(tmp_path, monkeypatch):
    import fnmusic.updates as module
    name = 'fnmusic-lx-first-0.1.9-x86.fpk'
    data = b'new-package'
    def response(request):
        if request.url.host == 'api.github.com':
            return httpx.Response(200, json={'tag_name': 'v0.1.9', 'assets': [{'name': name}, {'name': name + '.sha256'}]})
        if request.url.path.endswith('.sha256'):
            return httpx.Response(200, text=hashlib.sha256(data).hexdigest() + '  ' + name)
        return httpx.Response(200, content=data)
    monkeypatch.setattr(module, 'MAX_PACKAGE', 4)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        update = Updater(Config(data_dir=tmp_path), client=client, arch='x86', current='0.1.8')
        update.path.parent.mkdir()
        update.path.write_bytes(b'old')
        await update.check()
        await update.start_download()
        await update.task
        assert update.download['state'] == 'error'
        assert update.path.read_bytes() == b'old'
        assert not list(update.path.parent.glob('*.part'))


@pytest.mark.asyncio
async def test_shutdown_cancels_download_and_closes_response(tmp_path):
    import asyncio
    name = 'fnmusic-lx-first-0.1.9-x86.fpk'
    class Slow(httpx.AsyncByteStream):
        closed = False
        started = asyncio.Event()
        async def __aiter__(self):
            self.started.set()
            yield b'first'
            await asyncio.Future()
        async def aclose(self):
            self.closed = True
    slow = Slow()
    def response(request):
        if request.url.host == 'api.github.com':
            return httpx.Response(200, json={'tag_name': 'v0.1.9', 'assets': [{'name': name}, {'name': name + '.sha256'}]})
        if request.url.path.endswith('.sha256'):
            return httpx.Response(200, text='0' * 64 + '  ' + name)
        return httpx.Response(200, stream=slow)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        update = Updater(Config(data_dir=tmp_path), client=client, arch='x86', current='0.1.8')
        await update.check()
        await update.start_download()
        await asyncio.wait_for(slow.started.wait(), 2)
        await update.close()
        assert update.task.done() and slow.closed
        assert update.download['state'] == 'error'
        assert not list(update.path.parent.glob('*.part'))
