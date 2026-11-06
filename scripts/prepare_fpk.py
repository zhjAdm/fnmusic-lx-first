"""Prepare a clean Linux CI workspace using checksum-pinned public downloads."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from urllib.parse import urlsplit

from .runtime import ARCHIVE, SHA256, URL

ROOT = Path(__file__).resolve().parent.parent


def download_verified(url, digest, destination):
    destination = Path(destination)
    if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == digest:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.part', delete=False) as output:
            temporary = Path(output.name)
            sha = hashlib.sha256()
            total = 0
            with urllib.request.urlopen(url, timeout=120) as response:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > 500 * 1024 * 1024:
                        raise RuntimeError('Public archive exceeds size limit')
                    sha.update(chunk)
                    output.write(chunk)
        if sha.hexdigest() != digest:
            raise RuntimeError('Public archive SHA256 mismatch: ' + destination.name)
        temporary.replace(destination)
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare(work, arch='both'):
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    assets = json.loads((ROOT / 'packaging/fpk/assets.lock.json').read_text())
    shutil.copy2(ROOT / 'packaging/fpk/assets.lock.json', work / 'assets.json')
    tools = json.loads((ROOT / 'packaging/fpk/build-tools.lock.json').read_text())
    tool = tools['fnpack']
    fnpack = download_verified(tool['url'], tool['sha256'], work / 'cache' / Path(urlsplit(tool['url']).path).name)
    fnpack.chmod(0o755)
    download_verified(URL, SHA256, work / 'cache' / ARCHIVE)
    for target in ('x86', 'arm') if arch == 'both' else (arch,):
        for asset in assets[target].values():
            download_verified(asset['url'], asset['sha256'], work / 'cache' / Path(urlsplit(asset['url']).path).name)
        machine = 'x86_64' if target == 'x86' else 'aarch64'
        wheels = work / 'wheels' / machine
        wheels.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            sys.executable, '-m', 'pip', 'download', '--only-binary=:all:',
            '--implementation', 'cp', '--python-version', '313', '--abi', 'cp313',
            '--platform', 'manylinux_2_28_' + machine,
            '--platform', 'manylinux2014_' + machine,
            '--dest', str(wheels), '-r', str(ROOT / 'requirements.lock'),
        ], check=True)
    return fnpack


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, default=ROOT / 'work/fpk')
    parser.add_argument('--arch', choices=('x86', 'arm', 'both'), default='both')
    args = parser.parse_args()
    print('Verified build tool: ' + str(prepare(args.work, args.arch)))


if __name__ == '__main__':
    main()
