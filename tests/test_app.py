import httpx
from fastapi.testclient import TestClient
from test_core import RAW

from fnmusic.app import create_app
from fnmusic.config import Config


def test_search_pages_are_consistent_snapshots(tmp_path):
    def official(req):
        if req.url.path.endswith("user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": "a"}})
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"list": [{"guid": "local", "title": "本地"}], "total": 1},
            },
        )

    def lx(req):
        return httpx.Response(200, json=[dict(RAW, songmid=str(i)) for i in range(3)])

    oc = httpx.AsyncClient(
        base_url="http://official", transport=httpx.MockTransport(official)
    )
    lc = httpx.AsyncClient(base_url="http://lx", transport=httpx.MockTransport(lx))
    mc = httpx.AsyncClient()
    app = create_app(
        Config(data_dir=tmp_path, platforms=["kw"]), oc, lc, mc, worker=False
    )
    with TestClient(app) as client:
        first = client.get(
            "/music/api/v1/search/track?keyword=x&page=1&size=2",
            headers={"cookie": "x"},
        ).json()["data"]
        second = client.get(
            "/music/api/v1/search/track?keyword=x&page=2&size=2",
            headers={"cookie": "x"},
        ).json()["data"]
        assert len(first["list"]) == len(second["list"]) == 2
        assert first["total"] == second["total"] == 4
        assert {r["guid"] for r in first["list"]}.isdisjoint(
            {r["guid"] for r in second["list"]}
        )


def test_official_app_search_like_and_default_playlist_flow(tmp_path):
    calls = []

    def official(req):
        calls.append(req)
        path = req.url.path
        if path.endswith("user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": "a"}})
        return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})

    def lx(req):
        if req.url.path.endswith("/search"):
            return httpx.Response(200, json=[RAW])
        return httpx.Response(200, json=[])

    oc = httpx.AsyncClient(
        base_url="http://official", transport=httpx.MockTransport(official)
    )
    lc = httpx.AsyncClient(base_url="http://lx", transport=httpx.MockTransport(lx))
    mc = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    app = create_app(
        Config(data_dir=tmp_path, download_dir=tmp_path / "music", platforms=["kw"]),
        oc,
        lc,
        mc,
        worker=False,
    )
    with TestClient(app) as client:
        response = client.get(
            "/music/api/v1/search/track?keyword=歌曲", headers={"cookie": "valid"}
        )
        assert response.status_code == 200
        track = response.json()["data"]["list"][0]
        guid = track["guid"]
        assert len(guid) == 32
        metadata = client.get(
            f"/music/api/v1/track/metadata/{guid}", headers={"cookie": "valid"}
        )
        assert metadata.json()["data"]["guid"] == guid
        assert metadata.json()["data"]["track"]["genres"] == []
        response = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": guid},
            headers={"cookie": "valid"},
        )
        assert response.json()["code"] == 0
        assert app.state.store.jobs("a")[0]["state"] == "pending"
        assert not (tmp_path / "music").exists()
        response = client.get(
            "/music/api/v1/favorite-track/list", headers={"cookie": "valid"}
        )
        assert response.json()["data"]["list"][0]["isFavorite"]
        client.post(
            "/music/api/v1/favorite-track/delete",
            json={"trackGUID": guid},
            headers={"cookie": "valid"},
        )
        assert app.state.store.jobs("a")[0]["state"] == "cancelled"
        cards = client.get(
            "/music/api/v1/playlist/list", headers={"cookie": "valid"}
        ).json()["data"]["list"]
        assert any(x["name"] == "热门推荐" for x in cards)
        details = client.get(
            "/music/api/v1/playlist/batch-detail",
            params={"guids": cards[0]["guid"]},
            headers={"cookie": "valid"},
        )
        assert details.json()["data"]["list"][0]["guid"] == cards[0]["guid"]
        assert client.get("/_ext/jobs").status_code == 401


def test_cancel_bound_local_guid_clears_online_intent(tmp_path):
    def official(req):
        if req.url.path.endswith("user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": "a"}})
        return httpx.Response(200, json={"code": 0, "data": None})

    oc = httpx.AsyncClient(
        base_url="http://official", transport=httpx.MockTransport(official)
    )
    app = create_app(
        Config(data_dir=tmp_path),
        oc,
        httpx.AsyncClient(),
        httpx.AsyncClient(),
        worker=False,
    )
    with TestClient(app) as client:
        store = app.state.store
        t = store.save_track(RAW)
        store.like("a", t["guid"])
        store.update_job("a", t["guid"], state="done", official_guid="local-bound")
        response = client.post(
            "/music/api/v1/favorite-track/delete",
            json={"trackGUID": "local-bound"},
            headers={"cookie": "valid"},
        )
        assert response.json()["code"] == 0
        assert not store.is_liked("a", t["guid"])
        assert store.jobs("a")[0]["state"] == "cancelled"
