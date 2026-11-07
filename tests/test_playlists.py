import pytest
from test_core import RAW

from fnmusic.config import Config
from fnmusic.playlists import Playlists
from fnmusic.store import Store


class LX:
    async def hot(self, *args):
        return [RAW]

    async def search(self, *args):
        return [RAW, dict(RAW, songmid="99", name="新歌")]

    async def playlist(self, *args):
        return {"list": [RAW], "total": 1}


@pytest.mark.asyncio
async def test_force_hot_refresh_bypasses_daily_cache_and_reports_failure(tmp_path):
    class Hot(LX):
        calls = 0
        broken = False

        async def hot(self, *args):
            self.calls += 1
            if self.broken:
                raise ValueError("unavailable")
            return [dict(RAW, songmid=str(self.calls))]

    s = Store(tmp_path / "s.db")
    lx = Hot()
    p = Playlists(s, lx, Config(data_dir=tmp_path))
    first = await p.refresh_hot()
    second = await p.refresh_hot(force=True)
    assert first != second and lx.calls == 2
    assert await p.refresh_hot() == second
    lx.broken = True
    with pytest.raises(ValueError):
        await p.refresh_hot(force=True)
    assert s.playlist_snapshot(p.hot_key)["tracks"] == second
    assert s.jobs("a") == []
    s.close()


@pytest.mark.asyncio
async def test_recommendations_exclude_favorites_and_isolate_users(tmp_path):
    s = Store(tmp_path / "s.db")
    t = s.save_track(RAW)
    s.like("a", t["guid"])
    p = Playlists(s, LX(), Config())
    a = await p.tracks("a", p.guid("a", "liked"))
    assert [x["title"] for x in a] == ["新歌"]
    assert await p.tracks("b", p.guid("b", "liked")) == []
    with pytest.raises(KeyError):
        await p.tracks("b", p.guid("a", "liked"))
    s.close()


@pytest.mark.asyncio
async def test_hot_concurrent_refresh_rolls_over_at_beijing_midnight(tmp_path, monkeypatch):
    import asyncio
    from datetime import datetime
    import fnmusic.playlists as module

    now = [datetime.fromisoformat("2026-11-07T15:59:59+00:00").timestamp()]
    monkeypatch.setattr(module.time, "time", lambda: now[0])

    class Hot(LX):
        calls = 0

        async def hot(self, *args):
            self.calls += 1
            await asyncio.sleep(0)
            return [dict(RAW, songmid=str(i)) for i in range(40)]

    s = Store(tmp_path / "s.db")
    lx = Hot()
    p = Playlists(s, lx, Config(data_dir=tmp_path))
    rows = await asyncio.gather(*(p.refresh_hot() for _ in range(5)))
    assert lx.calls == 1 and all(len(row) == 30 for row in rows)
    now[0] += 2
    await p.refresh_hot()
    assert lx.calls == 2
    assert s.playlist_snapshot(p.hot_key)["day"] == "2026-11-08"
    s.close()


@pytest.mark.asyncio
async def test_hot_background_refresh_needs_no_user_and_stops_cleanly(tmp_path):
    import asyncio

    fetched = asyncio.Event()

    class Hot(LX):
        async def hot(self, *args):
            fetched.set()
            return [RAW]

    s = Store(tmp_path / "s.db")
    p = Playlists(s, Hot(), Config(data_dir=tmp_path))
    p.start()
    try:
        await asyncio.wait_for(fetched.wait(), timeout=1)
        assert s.playlist_snapshot(p.hot_key)["tracks"]
        assert s.db.execute("SELECT count(*) FROM job").fetchone()[0] == 0
    finally:
        await p.stop()
        s.close()
    assert p.task is None


@pytest.mark.asyncio
async def test_public_playlist_and_hot_are_available_without_download(tmp_path):
    s = Store(tmp_path / "s.db")
    p = Playlists(
        s,
        LX(),
        Config(public_playlists=[{"source": "kw", "id": "123", "name": "测试歌单"}]),
    )
    cards = p.cards("a")
    assert [x["name"] for x in cards] == ["热门推荐", "猜你喜欢", "测试歌单"]
    assert len(await p.tracks("a", cards[-1]["guid"])) == 1
    assert s.jobs("a") == []
    s.close()


@pytest.mark.asyncio
async def test_hot_daily_snapshot_is_shared_persistent_and_metadata_only(tmp_path, monkeypatch):
    import fnmusic.playlists as module

    now = [1793977200.0]
    monkeypatch.setattr(module.time, "time", lambda: now[0])

    class Hot(LX):
        calls = 0

        async def hot(self, *args):
            self.calls += 1
            return [dict(RAW, songmid=str(self.calls))]

    db = tmp_path / "state.db"
    s = Store(db)
    lx = Hot()
    p = Playlists(s, lx, Config(data_dir=tmp_path))
    first = await p.tracks("a", p.guid("a", "hot"))
    assert await p.tracks("b", p.guid("b", "hot")) == first
    assert lx.calls == 1
    assert p.cards("b")[0]["trackCount"] == 1
    assert p.cards("b")[0]["updatedAt"] > 0
    s.close()

    s = Store(db)
    p = Playlists(s, lx, Config(data_dir=tmp_path))
    assert await p.tracks("a", p.guid("a", "hot")) == first
    assert lx.calls == 1
    now[0] += 86400
    second = await p.refresh_hot()
    assert second != first and lx.calls == 2
    assert await p.tracks("a", p.guid("a", "hot")) == second
    assert s.jobs("a") == [] and s.jobs("b") == []
    assert s.favorites("a") == []
    assert s.db.execute("SELECT count(*) FROM file").fetchone()[0] == 0
    s.close()


@pytest.mark.asyncio
async def test_hot_failure_preserves_snapshot_and_backs_off(tmp_path, monkeypatch):
    import fnmusic.playlists as module

    now = [1793977200.0]
    clock = [0.0]
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    class Hot(LX):
        calls = 0
        broken = False

        async def hot(self, *args):
            self.calls += 1
            if self.broken:
                raise ValueError("unavailable")
            return [RAW]

    s = Store(tmp_path / "s.db")
    lx = Hot()
    p = Playlists(s, lx, Config(data_dir=tmp_path))
    first = await p.refresh_hot()
    updated = p.cards("a")[0]["updatedAt"]
    now[0] += 86400
    lx.broken = True
    assert await p.refresh_hot() == first
    assert await p.refresh_hot() == first
    assert lx.calls == 2
    assert p.cards("a")[0]["updatedAt"] == updated
    clock[0] += 301
    lx.broken = False
    assert await p.refresh_hot() == first
    assert lx.calls == 3
    assert p.cards("a")[0]["updatedAt"] > updated
    s.close()
