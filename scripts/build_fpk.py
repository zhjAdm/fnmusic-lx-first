"""Build self-contained native x86/ARM FPKs from verified public runtime caches."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
import tempfile
from urllib.parse import urlsplit
import zipfile
import zlib
from scripts.runtime import (
    expose_capabilities,
    restrict_listener,
    native,
    SHA256 as LX_SHA,
)

ROOT = Path(__file__).resolve().parent.parent
VERSION = "0.1.15"


def icon(path, size):
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            nx, ny = x / size, y / size
            color = (37, 65, 51)
            if (nx - 0.5) ** 2 + (ny - 0.5) ** 2 < 0.32**2:
                color = (227, 227, 199)
            if (nx - 0.5) ** 2 + (ny - 0.5) ** 2 < 0.24**2:
                color = (49, 84, 61)
            if (nx - 0.5) ** 2 + (ny - 0.5) ** 2 < 0.16**2:
                color = (227, 227, 199)
            if (nx - 0.5) ** 2 + (ny - 0.5) ** 2 < 0.06**2:
                color = (37, 65, 51)
            if 0.63 < nx < 0.675 and 0.20 < ny < 0.59:
                color = (218, 174, 106)
            if ((nx - 0.60) / 0.085) ** 2 + ((ny - 0.59) / 0.05) ** 2 < 1:
                color = (218, 174, 106)
            raw.extend(color)

    def chunk(name, data):
        return (
            struct.pack(">I", len(data))
            + name
            + data
            + struct.pack(">I", zlib.crc32(name + data) & 0xFFFFFFFF)
        )

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def skeleton(stage, arch):
    stage = Path(stage)
    stage.mkdir(parents=True, exist_ok=True)
    # --repack must also purge the previous full package's bundled binaries.
    shutil.rmtree(stage / "app/runtime/ffmpeg", ignore_errors=True)
    template = ROOT / "packaging/fpk"
    for name in ("cmd", "config", "wizard", "app"):
        shutil.copytree(template / name, stage / name, dirs_exist_ok=True)
    ui_path = stage / "app/ui/config"
    ui = json.loads(ui_path.read_text())
    ui[".url"]["fnmusic-lx-first.main"]["url"] = f"/app/fnmusic-lx-first/?v={VERSION}"
    ui_path.write_text(json.dumps(ui, ensure_ascii=False) + "\n")
    (stage / "manifest").write_text(
        (template / "manifest.in").read_text().replace("@ARCH@", arch)
    )
    code = stage / "app/code"
    code.mkdir(parents=True, exist_ok=True)
    for name in ("fnmusic", "scripts"):
        shutil.copytree(
            ROOT / name,
            code / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            dirs_exist_ok=True,
        )
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "requirements.lock"):
        shutil.copy2(ROOT / name, stage / "app" / name)
    for script in (stage / "cmd").iterdir():
        script.chmod(0o755)
    images = stage / "app/ui/images"
    images.mkdir(parents=True, exist_ok=True)
    for size in (64, 256):
        icon(stage / ("ICON.PNG" if size == 64 else "ICON_256.PNG"), size)
        shutil.copy2(
            stage / ("ICON.PNG" if size == 64 else "ICON_256.PNG"),
            images / f"icon_{size}.png",
        )


def verified_file(cache, item):
    path = cache / urlsplit(item["url"]).path.split("/")[-1]
    if (
        not path.is_file()
        or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]
    ):
        raise RuntimeError("Missing or unverified runtime cache: " + path.name)
    return path


def untar(path, dest):
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path) as bundle:
        # The unused curses terminfo database contains case-distinct aliases that
        # become self-referencing symlinks on default macOS filesystems.
        members = [
            m for m in bundle.getmembers() if "/share/terminfo/" not in "/" + m.name
        ]
        bundle.extractall(dest, members=members, filter="data")


def install_runtimes(stage, arch, assets, cache):
    runtime = stage / "app/runtime"
    runtime.mkdir()
    with tempfile.TemporaryDirectory(dir=stage.parent) as folder:
        tmp = Path(folder)
        untar(verified_file(cache, assets["python"]), tmp / "python")
        shutil.move(str(tmp / "python/python"), runtime / "python")
        untar(verified_file(cache, assets["node"]), tmp / "node")
        binary = next((tmp / "node").rglob("bin/node"))
        (runtime / "node/bin").mkdir(parents=True)
        shutil.copy2(binary, runtime / "node/bin/node")
        shutil.copy2(binary.parent.parent / "LICENSE", runtime / "node/LICENSE")
    lxzip = cache / "lx-music-sync-server-v2.1.2-server.zip"
    if hashlib.sha256(lxzip.read_bytes()).hexdigest() != LX_SHA:
        raise RuntimeError("LX runtime checksum mismatch")
    with tempfile.TemporaryDirectory(dir=stage.parent) as folder:
        temp = Path(folder)
        with zipfile.ZipFile(lxzip) as archive:
            for name in archive.namelist():
                if not (temp / name).resolve().is_relative_to(temp.resolve()):
                    raise RuntimeError("Unsafe LX archive")
            archive.extractall(temp)
        index = next(
            p
            for p in temp.rglob("index.js")
            if (p.parent / "package.json").exists() and "node_modules" not in p.parts
        )
        shutil.move(str(index.parent), stage / "app/lxserver")
    expose_capabilities(stage / "app/lxserver")
    native(
        stage / "app/lxserver",
        "linux-" + ("x64" if arch == "x86" else "arm64") + "-gnu",
    )
    # Record exact public provenance inside the package; no configuration is embedded.
    (stage / "app/runtime-provenance.json").write_text(
        json.dumps({k: v for k, v in assets.items() if k != "ffmpeg"}, indent=2)
    )


def install_wheels(stage, arch, wheels):
    site = stage / "app/site"
    site.mkdir()
    expected = 62 if arch == "x86" else 183
    for path in wheels.glob("*.whl"):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not (site / name).resolve().is_relative_to(site.resolve()):
                    raise RuntimeError("Unsafe wheel entry")
            archive.extractall(site)
    if not (site / "fastapi").is_dir():
        raise RuntimeError("Linux Python wheels are missing")
    for path in site.rglob("*.so"):
        header = path.read_bytes()[:20]
        if (
            header[:4] != b"\x7fELF"
            or int.from_bytes(header[18:20], "little") != expected
        ):
            raise RuntimeError("Wheel architecture mismatch")


def build(arch, work, out, fnpack, repack=False):
    stage = work / ("stage-" + arch)
    if stage.exists() and not repack:
        shutil.rmtree(stage)
    if repack and not (stage / "app/runtime/python/bin/python3").exists():
        raise RuntimeError("Stage runtimes missing")
    if repack:
        shutil.rmtree(stage / "app/code")
    skeleton(stage, arch)
    assets = json.loads((work / "assets.json").read_text())[arch]
    if not repack:
        install_runtimes(stage, arch, assets, work / "cache")
        install_wheels(
            stage, arch, work / "wheels" / ("x86_64" if arch == "x86" else "aarch64")
        )
    restrict_listener(stage / "app/lxserver")
    (stage / "app/runtime-provenance.json").write_text(
        json.dumps({k: v for k, v in assets.items() if k != "ffmpeg"}, indent=2)
    )
    # Check that no private source or local state entered the payload.
    if list(stage.rglob("*music-source-paid*")) or list(stage.rglob(".env")):
        raise RuntimeError("Private input in FPK stage")
    output = work / ("pack-" + arch)
    output.mkdir(exist_ok=True)
    raw = output / "fnmusic-lx-first.fpk"
    raw.unlink(missing_ok=True)
    result = subprocess.run(
        [str(fnpack), "build", "--directory", str(stage)],
        cwd=output,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode or not raw.exists() or raw.stat().st_size < 1024:
        raise RuntimeError(
            "fnpack did not create a package: "
            + result.stdout[-1500:]
            + result.stderr[-1500:]
        )
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"fnmusic-lx-first-{VERSION}-{arch}.fpk"
    shutil.move(raw, dest)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    dest.with_suffix(".fpk.sha256").write_text(digest + "  " + dest.name + "\n")
    print(dest.name + " built: " + str(dest.stat().st_size) + " bytes", flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--arch", choices=("x86", "arm", "both"), default="both")
    p.add_argument(
        "--repack",
        action="store_true",
        help="Refresh source and metadata while reusing verified staged runtimes",
    )
    p.add_argument("--work", type=Path, default=ROOT / "work/fpk")
    p.add_argument("--out", type=Path, default=ROOT / "dist")
    p.add_argument(
        "--fnpack", type=Path, default=ROOT / "work/fpk/cache/fnpack-1.2.3-darwin-arm64"
    )
    args = p.parse_args()
    for arch in ("x86", "arm") if args.arch == "both" else (args.arch,):
        build(
            arch,
            args.work.resolve(),
            args.out.resolve(),
            args.fnpack.resolve(),
            args.repack,
        )


if __name__ == "__main__":
    main()
