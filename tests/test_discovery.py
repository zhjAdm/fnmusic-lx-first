import time

import pytest

from fnmusic.config import Config
from fnmusic.logs import activity


def test_playback_history_persists_and_isolates_users_after_logs_removed(tmp_path):
    from fnmusic.history import History

    track = {"title": "A", "artist": "Singer", "style": "Rock", "guid": "native"}
    activity(tmp_path, "play.redirect", user="a", track=track)
    activity(tmp_path, "play.failed", user="a", track=dict(track, title="Failed"))
    activity(tmp_path, "play.requested", user="a", track=dict(track, title="Requested"))
    activity(tmp_path, "play.redirect", user="b", track=dict(track, title="B"))
    activity(tmp_path, "play.local_forwarded", track=dict(track, title="Unknown user"))
    for path in (tmp_path / "logs").glob("*"):
        path.unlink()
    rows = History(tmp_path).songs("a")
    assert [(r["title"], r["artist"], r["count"]) for r in rows] == [("A", "Singer", 1)]
    assert rows[0]["last_at"] > 0 and rows[0]["style"] == "Rock"
    assert History(tmp_path).songs("b")[0]["title"] == "B"
    assert History(tmp_path).songs("c") == []


def test_history_imports_retained_logs_once(tmp_path):
    from fnmusic.logs import event
    from fnmusic.history import History, user_key

    event(tmp_path, "history", "play.redirect", user=user_key("a"), title="Old", artist="S")
    assert History(tmp_path).songs("a")[0]["count"] == 1
    assert History(tmp_path).songs("a")[0]["count"] == 1


def test_history_migration_retains_latest_style(tmp_path):
    from fnmusic.history import History, user_key
    from fnmusic.logs import event

    for style in ("Rock", "Jazz"):
        event(tmp_path, "history", "play.redirect", user=user_key("a"),
              title="Old", artist="S", style=style)
    row = History(tmp_path).songs("a")[0]
    assert row["count"] == 2 and row["style"] == "Jazz"


@pytest.mark.asyncio
async def test_exploration_modes_exclude_existing_and_change_new_style_ratio(tmp_path):
    from fnmusic.discovery import Discovery
    from fnmusic.history import History
    from fnmusic.store import Store
    from test_core import RAW

    config = Config(data_dir=tmp_path)
    activity(tmp_path, "play.redirect", user="a",
             track={"title": "Heard", "artist": "Known", "style": "Rock"})

    class LX:
        async def styles(self, styles, sources):
            return [dict(RAW, songmid=f"{style}-{i}", name=f"{style}-{i}",
                         singer="New", recommendation_style=style)
                    for style in styles for i in range(40)] + [
                        dict(RAW, songmid="heard", name="Heard", singer="Known")]

    store = Store(tmp_path / "state.db")
    discovery = Discovery(config, store, LX())
    totals = []
    for mode in ("comfort", "balanced", "bold"):
        discovery.save_settings(mode)
        rows = await discovery.explore("a", [])
        assert len(rows) == 30
        assert all(r["title"] != "Heard" for r in rows)
        assert all(r["reason"] for r in rows)
        totals.append(sum(r["style"] != "摇滚" for r in rows))
    assert totals[0] < totals[1] < totals[2]
    assert History(tmp_path).songs("a")
    assert store.jobs("a") == []
    store.close()


@pytest.mark.asyncio
async def test_exploration_cold_start_fallback_and_live_setting_change(tmp_path):
    from fnmusic.playlists import Playlists
    from fnmusic.store import Store
    from test_core import RAW

    class LX:
        calls = 0

        async def hot(self, source, board):
            return [dict(RAW, songmid="hot", name="Hot", singer="S")]

        async def styles(self, styles, sources):
            self.calls += 1
            return [dict(RAW, songmid=f'{s}{i}', name=f'{s}{i}', singer='S',
                         recommendation_style=s) for s in styles for i in range(35)]

    store = Store(tmp_path / 'state.db')
    playlists = Playlists(store, LX(), Config(data_dir=tmp_path))
    guid = playlists.guid('a', 'explore')
    cold = await playlists.tracks('a', guid)
    assert len(cold) == 1 and cold[0]['title'] == 'Hot'
    activity(tmp_path, 'play.redirect', user='a', track={'title': 'Heard', 'artist': 'S', 'style': 'Rock'})
    playlists.invalidate('a')
    balanced = await playlists.tracks('a', guid)
    playlists.discovery.save_settings('bold')
    bold = await playlists.tracks('a', guid)
    assert sum(r['style'] != '摇滚' for r in bold) > sum(r['style'] != '摇滚' for r in balanced)
    assert playlists.discovery.lx.calls == 2
    assert await playlists.tracks('a', guid) == bold
    assert store.jobs('a') == []
    store.close()


@pytest.mark.asyncio
async def test_archaeology_checks_official_access_and_keeps_native_objects(tmp_path):
    from fnmusic.discovery import Discovery
    from fnmusic.library import Library
    from fnmusic.store import Store
    from library_fixtures import song

    config = Config(data_dir=tmp_path / "state")
    library = Library(config, validator=lambda roots: roots)
    root = tmp_path / "music"
    first = song(root, "first.wav", title="Allowed")
    song(root, "hidden.wav", title="Hidden")
    library.save([str(root)], False)
    await library.scan()

    class Official:
        calls = []

        async def find_local(self, track, headers, path=None):
            self.calls.append((track["title"], dict(headers)))
            if headers["cookie"] == "a" and path == str(first):
                return {"guid": "official-guid", "title": "Allowed", "artists": [],
                        "audioSpec": {"path": path}, "isFavorite": True}

    store = Store(config.data_dir / "state.db")
    discovery = Discovery(config, store, None)
    official = Official()
    rows = await discovery.archaeology("a", {"cookie": "a"}, official)
    assert len(rows) == 1 and rows[0]["guid"] == "official-guid"
    assert rows[0]["isFavorite"] is True and "raw" not in rows[0]
    assert await discovery.archaeology("b", {"cookie": "b"}, official) == []
    assert store.jobs("a") == []
    store.close()


def test_archaeology_daily_selection_mixes_unheard_forgotten_and_random():
    from fnmusic.discovery import archaeology_candidates

    now = time.time()
    library = [{"title": f"Song{i}", "artist": "S", "path": f"/{i}"} for i in range(100)]
    history = [{"title": f"Song{i}", "artist": "S", "last_at": now - 40 * 86400}
               for i in range(40)]
    rows = archaeology_candidates(library, history, "a", "2026-11-10", now)
    assert rows == archaeology_candidates(library, history, "a", "2026-11-10", now)
    assert rows != archaeology_candidates(library, history, "a", "2026-11-11", now)
    assert {r["reason"] for r in rows[:30]} == {"尚无播放记录", "超过 30 天未播放", "每日翻箱底"}
    assert len({r["path"] for r in rows}) == len(rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("days,reason", [(0, "每日翻箱底"), (40, "超过 30 天未播放")])
async def test_archaeology_recognizes_native_history_with_only_guid(tmp_path, monkeypatch, days, reason):
    import fnmusic.discovery as module
    from fnmusic.discovery import Discovery
    from fnmusic.history import History
    from fnmusic.store import Store

    config = Config(data_dir=tmp_path)
    History(tmp_path).record('a', {'guid': 'native-guid', 'title': '官方曲库歌曲（未取得曲目信息）'},
                             at=time.time() - days * 86400)
    if days == 0:
        History(tmp_path).record('a', {'guid': 'native-guid', 'title': 'Actual song', 'artist': 'Singer'},
                                 at=time.time() - 50 * 86400)
    monkeypatch.setattr(module, 'library_rows', lambda config: [
        {'title': 'Actual song', 'artist': 'Singer', 'path': '/vol1/music/a.flac'}])

    class Official:
        async def find_local(self, *args, **kwargs):
            return {'guid': 'native-guid', 'title': 'Actual song'}

    store = Store(tmp_path / 'state.db')
    rows = await Discovery(config, store, None).archaeology('a', {}, Official())
    assert rows[0]['discoveryReason'] == reason
    store.close()
