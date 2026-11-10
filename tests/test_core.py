import json

import httpx
import pytest

from fnmusic.lx import LX
from fnmusic.models import quality_order, to_official
from fnmusic.store import Store

RAW = {
    "source": "kw",
    "songmid": "42",
    "name": "歌曲",
    "singer": "歌手",
    "albumName": "专辑",
    "interval": "03:20",
    "types": [{"type": "flac"}, {"type": "320k"}],
}


def test_like_is_durable_and_isolated(tmp_path):
    db = tmp_path / "state.db"
    store = Store(db)
    track = store.save_track(RAW)
    store.like("a", track["guid"])
    store.like("a", track["guid"])
    assert len(store.jobs("a")) == 1
    assert store.favorites("b") == []
    store.close()
    store = Store(db)
    assert len(store.favorites("a")) == 1
    assert store.get_track(track["guid"])["raw"] == RAW
    store.close()


def test_track_id_survives_restart(tmp_path):
    store = Store(tmp_path / "state.db")
    one = store.save_track(RAW)
    assert len(one["guid"]) == 32
    assert store.save_track(RAW)["guid"] == one["guid"]
    vo = to_official(one)
    assert vo["duration"] == 200000
    assert vo["artists"][0]["name"] == "歌手"
    assert len(vo["artists"][0]["guid"]) == 32
    store.close()


def test_quality_order_uses_source_capabilities():
    assert quality_order(["128k", "flac", "master"]) == ["master", "flac", "128k"]
    assert quality_order(["nonsense"]) == []


@pytest.mark.asyncio
async def test_lx_contract(tmp_path):
    calls = []

    def handle(req):
        calls.append(req)
        assert req.headers["x-frontend-auth"] == "private"
        if req.url.path.endswith("/search"):
            assert req.url.params["source"] == "kw"
            return httpx.Response(200, json=[RAW])
        if req.url.path.endswith("/url"):
            assert json.loads(req.content)["songInfo"] == RAW
            return httpx.Response(
                200, json={"url": "https://cdn.example/audio", "type": "flac"}
            )
        if req.url.path.endswith("/list"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "s1",
                        "enabled": True,
                        "sources": {"kw": {"qualitys": ["128k", "flac"]}},
                    }
                ],
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://lx"
    )
    lx = LX(client, "private", ["kw"])
    assert await lx.search("歌曲", 1, 20) == [RAW]
    assert (await lx.resolve(RAW, "flac"))["url"].startswith("https://")
    assert await lx.qualities("kw") == ["flac", "128k"]
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('kg_fails', [False, True])
async def test_search_preserves_platform_priority_when_a_platform_fails(kg_fails):
    from fnmusic.config import Config

    def response(request):
        source = request.url.params['source']
        if kg_fails and source == 'kg':
            return httpx.Response(503)
        return httpx.Response(200, json=[dict(RAW, source=source)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(response), base_url='http://lx') as client:
        lx = LX(client, 'fixture', Config().platforms)
        result = await lx.search('歌曲')
        assert [r['source'] for r in result] == (
            ['tx', 'kw', 'wy', 'mg'] if kg_fails else ['kg', 'tx', 'kw', 'wy', 'mg']
        )


@pytest.mark.asyncio
async def test_style_recommendations_resolve_platform_tags_and_mix_artists():
    calls = []

    def handler(request):
        calls.append(request)
        source = request.url.params["source"]
        if request.url.path.endswith("/tags"):
            return httpx.Response(200, json={"tags": [{"name": "风格", "list": [
                {"id": source + "-rock", "name": "摇滚"}]}],
                "sortList": [{"id": "6" if source == "kg" else "hot"}]})
        if request.url.path.endswith("/list"):
            assert request.url.params["tagId"] == source + "-rock"
            assert request.url.params["sortId"] == ("6" if source == "kg" else "hot")
            return httpx.Response(200, json={"list": [{"id": source + "-playlist"}]})
        return httpx.Response(200, json={"list": [
            dict(RAW, source=source, hash=source, singer=source + "新歌手")]})

    async with httpx.AsyncClient(base_url="http://lx", transport=httpx.MockTransport(handler)) as client:
        rows = await LX(client, "fixture", ["kg", "tx"]).styles(["摇滚"], ["kg", "tx"])
    assert [r["source"] for r in rows] == ["kg", "tx"]
    assert all(r["recommendation_style"] == "摇滚" for r in rows)
    assert not any(r.url.path.endswith("/search") for r in calls)


@pytest.mark.asyncio
async def test_style_playlist_prefers_real_rating_then_popularity():
    selected = []

    def handler(request):
        if request.url.path.endswith("/tags"):
            return httpx.Response(200, json={"tags": [{"name": "风格", "list": [
                {"id": "rock", "name": "摇滚"}]}], "sortList": [{"id": "hot"}]})
        if request.url.path.endswith("/list"):
            return httpx.Response(200, json={"list": [
                {"id": "low", "grade": 2, "play_count": "10亿"},
                {"id": "high", "grade": 9, "play_count": "1万"},
                {"id": "mid", "grade": 8, "play_count": "2万"}]})
        selected.append(request.url.params["id"])
        return httpx.Response(200, json={"list": [
            dict(RAW, songmid="low", name="低分", rating=2),
            dict(RAW, songmid="high", name="高分", rating=9)]})

    async with httpx.AsyncClient(base_url="http://lx", transport=httpx.MockTransport(handler)) as client:
        rows = await LX(client, "fixture", ["kg"]).styles(["摇滚"], ["kg"])
    assert selected == ["high", "mid"]
    assert [r["name"] for r in rows[:2]] == ["高分", "高分"]
