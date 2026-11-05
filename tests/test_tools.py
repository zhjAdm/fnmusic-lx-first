import hashlib
import io
import tarfile

import httpx
import pytest

from fnmusic.config import Config
from fnmusic import tools


def archive(machine=62, traversal=False):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:xz") as bundle:
        for name in ("bin/ffmpeg", "bin/ffprobe", "LICENSE.txt"):
            payload = (
                bytearray(64) if name.startswith("bin/") else bytearray(b"license")
            )
            if name.startswith("bin/"):
                payload[:6] = b"\x7fELF\x02\x01"
                payload[18:20] = machine.to_bytes(2, "little")
            item = tarfile.TarInfo((".." if traversal else "release") + "/" + name)
            item.size = len(payload)
            bundle.addfile(item, io.BytesIO(payload))
    return output.getvalue()


@pytest.mark.asyncio
async def test_discovery_reuses_compatible_existing_tools_before_private(
    tmp_path, monkeypatch
):
    existing = tmp_path / "system"
    existing.mkdir()
    for name in ("ffmpeg", "ffprobe"):
        (existing / name).touch(mode=0o755)
    private = tmp_path / "state/media-tools/bin"
    private.mkdir(parents=True)
    for name in ("ffmpeg", "ffprobe"):
        (private / name).touch(mode=0o755)
    monkeypatch.setenv("PATH", str(existing))
    monkeypatch.setattr(tools, "OFFICIAL_DIRS", ())
    inspected = []

    async def compatible(name, path):
        inspected.append(path)
        return True

    monkeypatch.setattr(tools, "compatible", compatible)
    media = tools.MediaTools(Config(data_dir=tmp_path / "state"))
    status = await media.status()
    assert status["ready"]
    assert status["ffmpeg"] == str(existing / "ffmpeg")
    assert all(p.parent == existing for p in inspected)


@pytest.mark.asyncio
async def test_incompatible_tools_are_skipped_and_missing_is_explicit(
    tmp_path, monkeypatch
):
    path = tmp_path / "bad"
    path.mkdir()
    for name in ("ffmpeg", "ffprobe"):
        (path / name).touch(mode=0o755)
    monkeypatch.setenv("PATH", str(path))
    monkeypatch.setattr(tools, "OFFICIAL_DIRS", ())

    async def incompatible(*args):
        return False

    monkeypatch.setattr(tools, "compatible", incompatible)
    media = tools.MediaTools(Config(data_dir=tmp_path / "state"))
    assert not (await media.status())["ready"]
    with pytest.raises(tools.MissingTool, match="FFmpeg"):
        await media.require("ffmpeg")


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["", "checksum", "architecture", "traversal"])
async def test_optional_download_validates_and_publishes_only_complete_pair(
    tmp_path, monkeypatch, fault
):
    payload = archive(
        machine=183 if fault == "architecture" else 62, traversal=fault == "traversal"
    )
    asset = {
        "url": "https://example.test/fixed.tar.xz",
        "sha256": "0" * 64
        if fault == "checksum"
        else hashlib.sha256(payload).hexdigest(),
    }
    monkeypatch.setattr(tools, "download_asset", lambda: (asset, 62))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(tools, "OFFICIAL_DIRS", ())

    async def compatible(*args):
        return True

    monkeypatch.setattr(tools, "compatible", compatible)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        media = tools.MediaTools(Config(data_dir=tmp_path / "state"))
        if fault:
            with pytest.raises(ValueError):
                await media.install(client)
            assert not media.directory.exists()
            assert not list((tmp_path / "state").glob(".media-*"))
        else:
            await media.install(client)
            assert (await media.status())["ready"]
            assert (media.directory / "bin/ffmpeg").stat().st_mode & 0o111
            assert (media.directory / "LICENSE.txt").read_text() == "license"
            assert not list((tmp_path / "state").glob(".media-*"))


@pytest.mark.asyncio
async def test_existing_components_do_not_trigger_network_download(
    tmp_path, monkeypatch
):
    media = tools.MediaTools(Config(data_dir=tmp_path))

    async def ready(*args, **kwargs):
        return {"ready": True}

    monkeypatch.setattr(media, "status", ready)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("Unexpected download"))
    ) as client:
        await media.install(client)


@pytest.mark.asyncio
async def test_audio_download_checks_probe_before_fetching_audio(tmp_path, monkeypatch):
    from fnmusic.audio import Audio

    media = Audio(None, None, Config(data_dir=tmp_path))

    async def missing(self, name):
        assert name == "ffprobe"
        raise tools.MissingTool("FFmpeg unavailable")

    monkeypatch.setattr(tools.MediaTools, "require", missing)
    with pytest.raises(tools.MissingTool):
        await media.download({}, "flac")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_missing_transcoder_is_explicit_and_does_not_open_source(
    tmp_path, monkeypatch
):
    from fastapi import HTTPException
    from fnmusic.transcode import Transcode

    async def missing(self, name):
        raise tools.MissingTool("FFmpeg unavailable")

    monkeypatch.setattr(tools.MediaTools, "require", missing)
    transcode = Transcode(None)
    with pytest.raises(HTTPException) as exc:
        await transcode.audio({}, "user", {})
    assert exc.value.status_code == 503
    assert "FFmpeg" in str(exc.value.detail)
    assert transcode.slots._value == 2


@pytest.mark.asyncio
async def test_concurrent_status_requests_share_discovery(tmp_path, monkeypatch):
    import asyncio

    for name in ("ffmpeg", "ffprobe"):
        (tmp_path / name).touch(mode=0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(tools, "OFFICIAL_DIRS", ())
    calls = []

    async def compatible(name, path):
        calls.append(name)
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(tools, "compatible", compatible)
    media = tools.MediaTools(Config(data_dir=tmp_path / "state"))
    await asyncio.gather(*(media.status() for _ in range(6)))
    assert calls == ["ffmpeg", "ffprobe"]
