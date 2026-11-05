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
