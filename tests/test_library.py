import asyncio
import hashlib
from pathlib import Path
import pytest
from fnmusic.config import Config
from fnmusic.library import Library, IndexNotReady
from library_fixtures import song


def library(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    lib = Library(
        Config(data_dir=tmp_path / "state"),
        validator=lambda roots: [str(Path(p).resolve()) for p in roots],
    )
    lib.save([str(music)], True)
    return lib, music


@pytest.mark.asyncio
async def test_direct_scan_reads_audio_tags_without_modifying_music(tmp_path):
    lib, music = library(tmp_path)
    path = song(music)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert await lib.scan()
    state = lib.state()["scan"]
    assert state["state"] == "completed" and state["indexed"] == 1
    found = lib.find_duplicate(
        {"title": "歌曲", "artist": "歌手", "album": "专辑", "duration": 1000}
    )
    assert found["path"] == str(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert lib.database.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_removed_or_modified_files_cannot_be_false_duplicates(tmp_path):
    lib, music = library(tmp_path)
    path = song(music)
    await lib.scan()
    path.unlink()
    track = {"title": "歌曲", "artist": "歌手", "album": "专辑", "duration": 1000}
    assert lib.find_duplicate(track) is None
    await lib.scan()
    assert lib.state()["scan"]["indexed"] == 0
    song(music, title="另一首歌")
    await lib.scan()
    assert lib.find_duplicate(track) is None


@pytest.mark.asyncio
async def test_symlinks_are_not_followed_outside_selected_roots(tmp_path):
    lib, music = library(tmp_path)
    outside = song(tmp_path / "outside")
    (music / "alias.wav").symlink_to(outside)
    await lib.scan()
    assert lib.state()["scan"]["indexed"] == 0


@pytest.mark.asyncio
async def test_scan_can_be_cancelled_without_touching_music(tmp_path, monkeypatch):
    import fnmusic.library as module
    import time

    lib, music = library(tmp_path)
    for n in range(8):
        song(music, str(n) + ".wav")
    original = module.metadata

    def slow(path):
        time.sleep(0.02)
        return original(path)

    monkeypatch.setattr(module, "metadata", slow)
    task = asyncio.create_task(lib.scan())
    while lib.report().get("state") != "scanning":
        await asyncio.sleep(0.001)
    lib.cancel()
    assert await task is False
    assert lib.report()["state"] == "cancelled"
    assert len(list(music.glob("*.wav"))) == 8
    with pytest.raises(IndexNotReady):
        await lib.ensure_scanned()


def test_library_settings_require_real_local_paths(tmp_path):
    lib = Library(Config(data_dir=tmp_path / "state"))
    for roots, auto in [
        (["https://bad"], True),
        (["/tmp/../root"], True),
        ([], "false"),
    ]:
        with pytest.raises(ValueError):
            lib.save(roots, auto)


@pytest.mark.asyncio
async def test_index_database_failure_is_reported_as_failed_not_scanning(tmp_path):
    import sqlite3

    lib, music = library(tmp_path)
    song(music)
    lib.database.write_bytes(b"not a SQLite database")
    with pytest.raises(sqlite3.DatabaseError):
        await lib.scan()
    assert lib.report()["state"] == "failed"


@pytest.mark.asyncio
async def test_actual_task_shutdown_cancellation_still_propagates(tmp_path):
    lib, music = library(tmp_path)
    song(music)
    task = asyncio.create_task(lib.scan())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lib.report()["state"] == "cancelled"
    assert await lib.scan()


@pytest.mark.asyncio
async def test_download_refresh_respects_directory_changes_during_scan(tmp_path):
    lib, first = library(tmp_path)
    song(first, title="first")
    second = tmp_path / "second"
    song(second, title="second")
    task = asyncio.create_task(lib.ensure_scanned())
    while lib.report().get("state") != "scanning":
        await asyncio.sleep(0.001)
    lib.save([str(second)], True)
    await task
    assert lib.report()["directories"] == [str(second)]
    assert (
        lib.find_duplicate(
            {"title": "second", "artist": "歌手", "album": "专辑", "duration": 1000}
        )
        is not None
    )


@pytest.mark.asyncio
async def test_song_list_search_pagination_and_configured_roots(tmp_path):
    lib, music = library(tmp_path)
    song(music, 'a.wav', title='Alpha', artist='周杰伦')
    song(music, 'b.wav', title='Beta', album='100%_精选')
    song(music, 'c.wav', title='<script>曲目</script>')
    await lib.scan()
    first = lib.songs(page=1, size=2)
    second = lib.songs(page=2, size=2)
    assert first['total'] == second['total'] == 3
    assert len(first['items']) == 2 and len(second['items']) == 1
    assert not ({r['path'] for r in first['items']} & {r['path'] for r in second['items']})
    assert lib.songs(q='周杰伦')['items'][0]['title'] == 'Alpha'
    assert lib.songs(q='%_')['total'] == 1
    assert lib.songs(q="' OR 1=1 --")['total'] == 0
    assert set(first['items'][0]) == {'title','artist','album','duration','path','size'}
    lib.save([], False)
    assert lib.songs()['total'] == 0


def test_song_list_empty_index_and_limits(tmp_path):
    lib, _ = library(tmp_path)
    assert lib.songs() == {'items': [], 'total': 0, 'page': 1, 'size': 25}
    for kwargs in ({'page': 0}, {'size': 101}, {'q': 'x' * 201}):
        with pytest.raises(ValueError):
            lib.songs(**kwargs)
