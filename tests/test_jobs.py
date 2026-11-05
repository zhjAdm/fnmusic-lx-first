import asyncio
import wave
import os

import httpx
import pytest
from test_core import RAW

from fnmusic.audio import Audio
from fnmusic.config import Config
from fnmusic.jobs import Jobs
from fnmusic.store import Store


class FakeLX:
    async def qualities(self, source):
        return ["master", "flac", "320k"]

    async def resolve(self, raw, quality):
        return {"url": "https://93.184.216.34/a"}


class Download:
    def __init__(self, folder):
        self.calls = []
        self.folder = folder

    async def download(self, track, quality):
        self.calls.append(quality)
        if quality == "master":
            raise ValueError("not available")
        path = self.folder / (track["guid"] + ".flac")
        path.write_bytes(b"checked-audio")
        return path


class Official:
    def __init__(self):
        self.ready = False
        self.calls = []

    async def user(self, headers):
        return "a"

    async def scan(self, headers):
        self.calls.append("scan")

    async def find_local(self, track, headers, path=None):
        return {"guid": "local"} if self.ready else None

    async def default_playlist(self, headers):
        return "default"

    async def api(self, method, path, headers, **kwargs):
        self.calls.append((path, kwargs))


@pytest.mark.asyncio
async def test_highest_quality_download_before_binding(tmp_path):
    store = Store(tmp_path / "s.db")
    track = store.save_track(RAW)
    store.like("a", track["guid"])
    downloader = Download(tmp_path)
    official = Official()
    jobs = Jobs(store, FakeLX(), downloader, official, Config())
    await jobs.run_once("a", {"cookie": "not-persisted"})
    assert downloader.calls == ["master", "flac"]
    assert store.jobs("a")[0]["state"] == "waiting_scan"
    assert not any(isinstance(x, tuple) for x in official.calls)
    official.ready = True
    store.update_job("a", track["guid"], next_at=0)
    await jobs.run_once("a", {"cookie": "not-persisted"})
    assert store.jobs("a")[0]["state"] == "done"
    assert official.calls[-1][0].endswith("playlist/add-track")
    assert official.calls[-1][1]["json"] == {"guid": "default", "trackGUIDs": ["local"]}
    assert downloader.calls == ["master", "flac"]
    assert b"not-persisted" not in (tmp_path / "s.db").read_bytes()
    store.close()


@pytest.mark.asyncio
async def test_concurrent_workers_do_not_duplicate_download(tmp_path):
    store = Store(tmp_path / "s.db")
    t = store.save_track(RAW)
    store.like("a", t["guid"])
    downloader = Download(tmp_path)
    official = Official()
    official.ready = True
    jobs = Jobs(store, FakeLX(), downloader, official, Config())
    await asyncio.gather(*(jobs.run_once("a", {}) for _ in range(3)))
    assert downloader.calls == ["master", "flac"]
    assert store.jobs("a")[0]["state"] == "done"
    store.close()


@pytest.mark.asyncio
async def test_cancel_during_download_prevents_binding(tmp_path):
    store = Store(tmp_path / "s.db")
    t = store.save_track(RAW)
    store.like("a", t["guid"])

    class CancelDownload(Download):
        async def download(self, track, quality):
            store.unlike("a", track["guid"])
            p = self.folder / "x.flac"
            p.write_bytes(b"audio")
            return p

    official = Official()
    official.ready = True
    jobs = Jobs(store, FakeLX(), CancelDownload(tmp_path), official, Config())
    await jobs.run_once("a", {})
    assert store.jobs("a")[0]["state"] == "cancelled"
    assert official.calls == []
    store.close()


@pytest.mark.asyncio
async def test_download_failure_is_retryable(tmp_path):
    store = Store(tmp_path / "s.db")
    t = store.save_track(RAW)
    store.like("a", t["guid"])

    class Broken:
        async def download(self, *args):
            raise ValueError("secret URL must not persist")

    jobs = Jobs(store, FakeLX(), Broken(), Official(), Config())
    await jobs.run_once("a", {})
    job = store.jobs("a")[0]
    assert job["state"] == "retry" and job["attempts"] == 1
    assert "secret" not in job["error"]
    store.close()


@pytest.mark.asyncio
async def test_real_download_validates_audio_and_cleans_temp(tmp_path):
    sample = tmp_path / "sample.wav"
    with wave.open(str(sample), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(8000)
        f.writeframes(b"\0\0" * 8000)
    body = sample.read_bytes()

    def handle(req):
        return httpx.Response(200, content=body, headers={"content-type": "audio/wav"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
        config = Config(download_dir=tmp_path / "music")
        audio = Audio(c, FakeLX(), config)
        track = {
            "guid": "a" * 32,
            "source": "kw",
            "raw": RAW,
            "title": "歌曲",
            "artist": "歌手",
            "album": "专辑",
            "duration": 1000,
        }
        path = await audio.download(track, "flac")
        assert path.suffix == ".wav"
        assert path.exists()
        assert not list(config.download_dir.glob("*.part"))


@pytest.mark.asyncio
async def test_html_and_preview_never_published(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                content=b"<html>error</html>",
                headers={"content-type": "audio/mpeg"},
            )
        )
    ) as c:
        audio = Audio(c, FakeLX(), Config(download_dir=tmp_path / "music"))
        track = {"guid": "a" * 32, "source": "kw", "raw": RAW, "duration": 200000}
        with pytest.raises(ValueError):
            await audio.download(track, "320k")
        assert not list((tmp_path / "music").iterdir())


@pytest.mark.asyncio
async def test_new_download_directory_is_traversable_under_service_umask(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=b"bad", headers={"content-type": "audio/mpeg"}
            )
        )
    ) as c:
        folder = tmp_path / "new" / "music"
        audio = Audio(c, FakeLX(), Config(download_dir=folder))
        old = os.umask(0o077)
        try:
            with pytest.raises(ValueError):
                await audio.download({"guid": "a" * 32, "raw": RAW}, "320k")
        finally:
            os.umask(old)
        assert folder.stat().st_mode & 0o005 == 0o005
        assert folder.parent.stat().st_mode & 0o005 == 0o005


@pytest.mark.asyncio
async def test_interrupted_scan_retries_after_worker_restart(tmp_path):
    store = Store(tmp_path / "s.db")
    track = store.save_track(RAW)
    store.like("a", track["guid"])

    class Interrupted(Official):
        async def scan(self, headers):
            self.calls.append("scan")
            if self.calls.count("scan") == 1:
                raise asyncio.CancelledError()

    official = Interrupted()
    downloader = Download(tmp_path)
    jobs = Jobs(store, FakeLX(), downloader, official, Config())
    with pytest.raises(asyncio.CancelledError):
        await jobs.run_once("a", {})
    store.update_job("a", track["guid"], next_at=0)
    restored = Jobs(store, FakeLX(), downloader, official, Config())
    await restored.run_once("a", {})
    assert official.calls.count("scan") == 2
    store.close()
