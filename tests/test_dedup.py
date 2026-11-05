import pytest
from test_core import RAW
from test_jobs import Official as FakeOfficial, FakeLX, Download
from fnmusic.config import Config
from fnmusic.store import Store
from fnmusic.jobs import Jobs
from fnmusic.library import Library
from library_fixtures import song


@pytest.mark.asyncio
@pytest.mark.parametrize("different", ["", "artist", "album", "duration"])
async def test_duplicate_uses_local_tags_and_version_metadata(tmp_path, different):
    music = tmp_path / "music"
    song(music)
    lib = Library(Config(data_dir=tmp_path / "state"), validator=lambda roots: roots)
    lib.save([str(music)], True)
    await lib.scan()
    track = {"title": "歌曲", "artist": "歌手", "album": "专辑", "duration": 1000}
    if different == "artist":
        track["artist"] = "另一个歌手"
    if different == "album":
        track["album"] = "现场版"
    if different == "duration":
        track["duration"] = 200000
    assert bool(lib.find_duplicate(track)) == (different == "")


@pytest.mark.asyncio
async def test_existing_file_skips_source_and_download_waits_for_official_binding(
    tmp_path,
):
    store = Store(tmp_path / "state.db")
    track = store.save_track(dict(RAW, interval="00:01"))
    store.like("a", track["guid"])
    music = tmp_path / "music"
    song(music)
    lib = Library(Config(data_dir=tmp_path / "state"), validator=lambda roots: roots)
    lib.save([str(music)], True)
    await lib.scan()

    class NoSource:
        async def qualities(self, *args):
            pytest.fail("Must not request online source")

    class Official(FakeOfficial):
        async def scan(self, *args):
            pytest.fail("Must not call official scan")

        async def find_duplicate(self, *args):
            pytest.fail("Must not use official dedup")

    official = Official()
    official.ready = True
    jobs = Jobs(store, NoSource(), None, official, Config(data_dir=tmp_path / "state"))
    jobs.library = lib
    await jobs.run_once("a", {})
    assert store.jobs("a")[0]["state"] == "done"
    assert "scan" not in official.calls
    assert len(list(music.iterdir())) == 1
    store.close()


@pytest.mark.asyncio
async def test_cross_platform_reuses_extension_file(tmp_path):
    store = Store(tmp_path / "state.db")
    first = store.save_track(RAW)
    second = store.save_track(dict(RAW, source="wy", songmid="second"))
    for track in (first, second):
        store.like("a", track["guid"])
    downloader = Download(tmp_path)
    jobs = Jobs(
        store, FakeLX(), downloader, FakeOfficial(), Config(data_dir=tmp_path / "state")
    )
    await jobs.run_once("a", {})
    assert downloader.calls == ["master", "flac"]
    assert store.file(first["guid"])["path"] == store.file(second["guid"])["path"]
    store.close()


@pytest.mark.asyncio
async def test_unlike_while_local_index_is_updating_cannot_download(tmp_path):
    store = Store(tmp_path / "state.db")
    track = store.save_track(RAW)
    store.like("a", track["guid"])

    class CancelIndex:
        async def ensure_scanned(self):
            store.unlike("a", track["guid"])

        def find_duplicate(self, track):
            return None

    class NoSource:
        async def qualities(self, *args):
            pytest.fail("Cancelled favorite must not download")

    jobs = Jobs(
        store, NoSource(), None, FakeOfficial(), Config(data_dir=tmp_path / "state")
    )
    jobs.library = CancelIndex()
    await jobs.run_once("a", {})
    assert store.jobs("a")[0]["state"] == "cancelled"
    store.close()


@pytest.mark.asyncio
async def test_cancelling_directory_scan_keeps_favorite_worker_alive(
    tmp_path, monkeypatch
):
    import asyncio
    import time
    import fnmusic.library as module

    music = tmp_path / "music"
    for n in range(8):
        song(music, str(n) + ".wav")
    config = Config(data_dir=tmp_path / "state")
    lib = Library(config, validator=lambda roots: roots)
    lib.save([str(music)], True)
    original = module.metadata

    def slow(path):
        time.sleep(0.03)
        return original(path)

    monkeypatch.setattr(module, "metadata", slow)
    store = Store(tmp_path / "state.db")
    track = store.save_track(RAW)
    store.like("a", track["guid"])
    jobs = Jobs(store, FakeLX(), Download(tmp_path), FakeOfficial(), config)
    jobs.library = lib
    jobs.touch("a", {"cookie": "valid"})
    jobs.start()
    try:
        for _ in range(100):
            if lib.report()["state"] == "scanning":
                break
            await asyncio.sleep(0.005)
        lib.cancel()
        await asyncio.sleep(0.15)
        assert not jobs.task.done()
        assert store.jobs("a")[0]["state"] == "waiting_index"
    finally:
        await jobs.stop()
        store.close()
