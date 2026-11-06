import hashlib

import httpx
import pytest

from fnmusic.config import Config
from fnmusic.updates import Updater


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
