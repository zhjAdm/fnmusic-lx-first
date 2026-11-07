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


@pytest.mark.asyncio
@pytest.mark.parametrize("move", [False, True])
async def test_wait_for_organized_library_before_binding_after_restart(tmp_path, move):
    from fnmusic.library import Library
    from library_fixtures import song

    downloads = tmp_path / "downloads"
    downloads.mkdir()
    library_dir = tmp_path / "library"
    library_dir.mkdir()
    config = Config(data_dir=tmp_path / "data", download_dir=downloads)
    library = Library(config, validator=lambda roots: roots)
    library.save([str(library_dir)], True)
    store = Store(tmp_path / "state.db")
    track = store.save_track(dict(RAW, interval="00:01"))
    store.like("a", track["guid"])
    downloader = Download(downloads)

    class ReadyOfficial(Official):
        lookups = []

        async def find_local(self, track, headers, path=None):
            self.lookups.append(str(path))
            return {"guid": "organized-guid"} if self.ready else None

    official = ReadyOfficial()
    official.ready = True  # Even a scanned source file must not be bound.
    jobs = Jobs(store, FakeLX(), downloader, official, config)
    jobs.library = library
    await jobs.run_once("a", {})
    assert store.jobs("a")[0]["state"] == "waiting_scan"
    assert official.calls == [] and official.lookups == []
    source = store.file(track["guid"])["path"]
    if move:
        os.unlink(source)
    store.close()
    store = Store(tmp_path / "state.db")
    jobs = Jobs(store, FakeLX(), downloader, official, config)
    jobs.library = library
    for _ in range(2):
        store.update_job("a", track["guid"], next_at=0)
        await jobs.run_once("a", {})
    assert downloader.calls == ["master", "flac"]
    assert store.jobs("a")[0]["attempts"] == 0
    assert official.lookups == []

    # An unrelated recording in the destination must not satisfy the wait.
    song(library_dir / "其他专辑", "other.wav", album="现场版")
    await library.scan()
    store.update_job("a", track["guid"], next_at=0)
    await jobs.run_once("a", {})
    assert official.lookups == [] and official.calls == []
    assert downloader.calls == ["master", "flac"]

    destination = song(library_dir / "歌手" / "专辑", "01 - 歌曲.wav")
    # Simulate the next automatic refresh after the 60-second scan cache expires.
    import json
    report = library.report()
    report["finished_at"] = 0
    library.report_path.write_text(json.dumps(report))
    official.ready = False
    store.update_job("a", track["guid"], next_at=0)
    await jobs.run_once("a", {})
    assert store.jobs("a")[0]["state"] == "waiting_scan"
    assert official.lookups == [str(destination)]
    assert official.calls == []
    official.ready = True
    store.update_job("a", track["guid"], next_at=0)
    await jobs.run_once("a", {})
    assert store.jobs("a")[0]["state"] == "done"
    assert official.calls[-1][1]["json"]["trackGUIDs"] == ["organized-guid"]
    assert downloader.calls == ["master", "flac"]
    from fnmusic.logs import read_events
    events = read_events(config.data_dir)
    assert len([row for row in events if row['event'] == 'playlist.organized']) == 1
    assert not any(row['event'] == 'download.skipped' for row in events)
    store.close()


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

    async def find_duplicate(self, track, headers):
        return None

    async def scan(self, headers):
        self.calls.append("scan")
        return True

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
async def test_interrupted_binding_lookup_retries_after_worker_restart(tmp_path):
    store = Store(tmp_path / "s.db")
    track = store.save_track(RAW)
    store.like("a", track["guid"])

    class Interrupted(Official):
        async def find_local(self, track, headers, path=None):
            self.calls.append("lookup")
            if self.calls.count("lookup") == 1:
                raise asyncio.CancelledError()
            return None

    official = Interrupted()
    downloader = Download(tmp_path)
    jobs = Jobs(store, FakeLX(), downloader, official, Config())
    with pytest.raises(asyncio.CancelledError):
        await jobs.run_once("a", {})
    store.update_job("a", track["guid"], next_at=0)
    restored = Jobs(store, FakeLX(), downloader, official, Config())
    await restored.run_once("a", {})
    assert official.calls.count("lookup") == 2
    store.close()


@pytest.mark.asyncio
async def test_missing_probe_waits_without_consuming_attempts_then_resumes(tmp_path):
    from fnmusic.tools import MissingTool

    store = Store(tmp_path / "s.db")
    track = store.save_track(RAW)
    store.like("a", track["guid"])

    class Waiting(Download):
        missing = True

        async def download(self, track, quality):
            if self.missing:
                raise MissingTool("FFmpeg missing")
            return await super().download(track, quality)

    audio = Waiting(tmp_path)
    official = Official()
    official.ready = True
    jobs = Jobs(store, FakeLX(), audio, official, Config())
    for _ in range(10):
        await jobs.run_once("a", {})
        store.update_job("a", track["guid"], next_at=0)
    job = store.jobs("a")[0]
    assert job["state"] == "waiting_tools" and job["attempts"] == 0
    assert audio.calls == []
    audio.missing = False
    await jobs.run_once("a", {})
    assert store.jobs("a")[0]["state"] == "done"
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('local_file', [False, True])
async def test_waiting_for_official_binding_logs_transition_once(tmp_path, local_file):
    from fnmusic.logs import read_events

    store = Store(tmp_path / 'state.db')
    track = store.save_track(RAW)
    store.like('a', track['guid'])
    config = Config(data_dir=tmp_path / 'data')
    path = tmp_path / 'existing.flac'
    path.write_bytes(b'checked-audio')
    official = Official()
    downloader = Download(tmp_path)
    jobs = Jobs(store, FakeLX(), downloader, official, config)

    class Index:
        def read(self):
            return {"directories": []}

        async def ensure_scanned(self):
            pass

        def find_duplicate(self, track):
            return {'path': str(path)} if local_file else None

    jobs.library = Index()
    await jobs.run_once('a', {})
    for _ in range(3):
        store.update_job('a', track['guid'], next_at=0)
        # A process restart must not restart repetitive status messages.
        jobs = Jobs(store, FakeLX(), downloader, official, config)
        jobs.library = Index()
        await jobs.run_once('a', {})
    rows = read_events(config.data_dir)
    if local_file:
        assert store.jobs('a')[0]['path'] == ''
    assert len([r for r in rows if r['event'] == 'download.skipped']) == int(local_file)
    assert len([r for r in rows if r['event'] == 'playlist.waiting']) == 1
    assert downloader.calls == ([] if local_file else ['master', 'flac'])
    official.ready = True
    store.update_job('a', track['guid'], next_at=0)
    await jobs.run_once('a', {})
    assert store.jobs('a')[0]['state'] == 'done'
    assert len([r for r in read_events(config.data_dir) if r['event'] == 'playlist.completed']) == 1
    store.close()


@pytest.mark.asyncio
async def test_missing_download_is_diagnosed_without_hiding_new_download(tmp_path):
    from fnmusic.logs import read_events

    store = Store(tmp_path / 'state.db')
    track = store.save_track(RAW)
    store.like('a', track['guid'])
    config = Config(data_dir=tmp_path / 'data')
    downloader = Download(tmp_path)
    jobs = Jobs(store, FakeLX(), downloader, Official(), config)
    await jobs.run_once('a', {})
    downloaded = store.file(track['guid'])
    os.unlink(downloaded['path'])
    store.update_job('a', track['guid'], next_at=0)
    await jobs.run_once('a', {})
    rows = read_events(config.data_dir)
    assert len([r for r in rows if r['event'] == 'audio.downloaded']) == 2
    missing = [r for r in rows if r['event'] == 'download.file_unavailable']
    assert len(missing) == 1 and missing[0]['state'] == 'missing_or_empty'
    assert 'path' not in missing[0]
    store.close()
