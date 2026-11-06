"""Validate FPK checksums, embedded sources, private-input exclusions and ELF CPUs."""

import argparse
import hashlib
import io
from pathlib import Path
import re
import tarfile

from .build_fpk import ROOT, VERSION


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(path):
    path = Path(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    require(sha == path.with_suffix('.fpk.sha256').read_text().split()[0], 'FPK SHA256 mismatch')
    expected = {
        p.relative_to(ROOT).as_posix()
        for directory in ('fnmusic', 'scripts')
        for p in (ROOT / directory).rglob('*')
        if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'
    }
    with tarfile.open(path) as outer:
        manifest = outer.extractfile('manifest').read().decode()
        def value(key):
            match = re.search(r'^' + key + r'\s*=\s*(\S+)', manifest, re.M)
            require(match is not None, 'Missing manifest field: ' + key)
            return match.group(1)
        require(value('version') == VERSION, 'FPK version mismatch')
        arch = value('platform')
        require(arch in ('x86', 'arm'), 'Unsupported FPK architecture')
        machine = 62 if arch == 'x86' else 183
        payload = outer.extractfile('app.tgz').read()
        require(hashlib.md5(payload).hexdigest() == value('checksum'), 'Payload checksum mismatch')
        matched, binaries = set(), set()
        with tarfile.open(fileobj=io.BytesIO(payload)) as app:
            for member in app:
                name = member.name.removeprefix('./')
                base = Path(name).name
                require(base not in ('.env', 'private-source.js', 'ffmpeg', 'ffprobe') and 'music-source-paid' not in name, 'Private input or bundled FFmpeg in package')
                if not member.isfile():
                    continue
                if base in ('node', 'python3.13'):
                    header = app.extractfile(member).read(20)
                    require(header[:4] == b'\x7fELF' and int.from_bytes(header[18:20], 'little') == machine, 'ELF architecture mismatch')
                    binaries.add(base)
                if name.startswith(('code/fnmusic/', 'code/scripts/')):
                    relative = name[5:]
                    require(relative in expected, 'Unexpected embedded source: ' + relative)
                    require(app.extractfile(member).read() == (ROOT / relative).read_bytes(), 'Embedded source mismatch: ' + relative)
                    matched.add(relative)
        require(matched == expected, 'Missing embedded sources')
        require(binaries == {'node', 'python3.13'}, 'Missing runtime binaries')
    return {'arch': arch, 'version': VERSION, 'sha256': sha, 'bytes': path.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', type=Path, nargs='+')
    for path in parser.parse_args().paths:
        result = verify(path)
        print(f'{path.name}: verified {result["arch"]}, {result["sha256"]}')


if __name__ == '__main__':
    main()
