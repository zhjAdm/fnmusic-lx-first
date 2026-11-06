import hashlib
import io

import pytest

from scripts.prepare_fpk import download_verified


def test_verified_download_is_atomic_and_reuses_matching_cache(tmp_path, monkeypatch):
    body = b'fixed public tool'
    digest = hashlib.sha256(body).hexdigest()
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        return io.BytesIO(body)

    monkeypatch.setattr('urllib.request.urlopen', fetch)
    path = tmp_path / 'tool'
    download_verified('https://fixture.invalid/tool', digest, path)
    assert path.read_bytes() == body
    download_verified('https://fixture.invalid/tool', digest, path)
    assert len(calls) == 1


def test_checksum_failure_never_replaces_cache_or_leaves_partial_file(tmp_path, monkeypatch):
    path = tmp_path / 'tool'
    path.write_bytes(b'old')
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **kw: io.BytesIO(b'bad'))
    with pytest.raises(RuntimeError, match='SHA256'):
        download_verified('https://fixture.invalid/tool', '0' * 64, path)
    assert path.read_bytes() == b'old'
    assert not list(tmp_path.glob('*.part'))


@pytest.mark.parametrize('fault', ['', 'private', 'cpu', 'checksum'])
def test_package_verifier_rejects_private_inputs_wrong_cpu_and_corruption(tmp_path, fault):
    import tarfile
    from scripts.build_fpk import ROOT, VERSION
    from scripts.verify_fpk import verify

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode='w:gz') as inner:
        for directory in ('fnmusic', 'scripts'):
            for path in (ROOT / directory).rglob('*'):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                    inner.add(path, arcname='code/' + path.relative_to(ROOT).as_posix())
        for name in ('runtime/python/bin/python3.13', 'runtime/node/bin/node'):
            binary = bytearray(64)
            binary[:4] = b'\x7fELF'
            binary[18:20] = (183 if fault == 'cpu' else 62).to_bytes(2, 'little')
            item = tarfile.TarInfo(name)
            item.size = len(binary)
            inner.addfile(item, io.BytesIO(binary))
        if fault == 'private':
            item = tarfile.TarInfo('code/private-source.js')
            item.size = 1
            inner.addfile(item, io.BytesIO(b'x'))
    data = payload.getvalue()
    checksum = '0' * 32 if fault == 'checksum' else hashlib.md5(data).hexdigest()
    manifest = f'version = {VERSION}\nplatform = x86\nchecksum = {checksum}\n'.encode()
    package = tmp_path / 'fixture.fpk'
    with tarfile.open(package, 'w') as outer:
        for name, body in [('manifest', manifest), ('app.tgz', data)]:
            item = tarfile.TarInfo(name)
            item.size = len(body)
            outer.addfile(item, io.BytesIO(body))
    package.with_suffix('.fpk.sha256').write_text(hashlib.sha256(package.read_bytes()).hexdigest() + ' fixture.fpk\n')
    if fault:
        with pytest.raises(ValueError):
            verify(package)
    else:
        assert verify(package)['arch'] == 'x86'
