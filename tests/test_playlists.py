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
