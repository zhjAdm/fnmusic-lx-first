import httpx
import pytest
from fastapi.testclient import TestClient
from test_core import RAW

from fnmusic.app import create_app
from fnmusic.config import Config


@pytest.mark.parametrize('configured', ['', 'custom-target'])
@pytest.mark.parametrize('batch_shape', ['list', 'object'])
def test_official_default_playlist_cover_in_list_detail_and_batch(tmp_path, configured, batch_shape):
    default = {'guid': 'custom-target' if configured else 'official-default',
               'name': '自定义收藏' if configured else '默认歌单',
               'coverId': None, 'trackCount': 17, 'owner': 'a'}
    other = {'guid': 'other', 'name': '默认歌单' if configured else '其他歌单',
             'coverId': 'original-cover', 'trackCount': 9}

    def official(req):
        if req.url.path.endswith('user/me'):
            return httpx.Response(200, json={'code': 0, 'data': {'guid': 'a'}})
        if req.url.path.endswith('playlist/detail'):
            data = default if req.url.params.get('guid') == default['guid'] else other
        elif req.url.path.endswith('playlist/batch-detail'):
            data = [default, other] if batch_shape == 'list' else {'list': [default, other], 'total': 2}
        else:
            data = {'list': [default, other], 'total': 2}
        return httpx.Response(200, json={'code': 0, 'data': data})

    app = create_app(Config(data_dir=tmp_path, default_playlist_guid=configured),
                     httpx.AsyncClient(base_url='http://official', transport=httpx.MockTransport(official)),
                     httpx.AsyncClient(), httpx.AsyncClient(), worker=False)
    with TestClient(app) as client:
        headers = {'cookie': 'valid'}
        rows = client.get('/music/api/v1/playlist/list', headers=headers).json()['data']['list']
        actual = next(row for row in rows if row['guid'] == default['guid'])
        cover = actual['coverId']
        assert isinstance(cover, str) and len(cover) == 32
        assert actual == dict(default, coverId=cover)
        assert next(row for row in rows if row['guid'] == 'other') == other
        detail = client.get('/music/api/v1/playlist/detail', params={'guid': default['guid']},
                            headers=headers).json()['data']
        assert detail == actual
        batch = client.get('/music/api/v1/playlist/batch-detail',
                           params={'guids': default['guid']+',other'}, headers=headers).json()['data']
        batch_rows = batch if isinstance(batch, list) else batch['list']
        assert batch_rows == [actual, other]
        built_in = app.state.playlists.guid('a', 'explore')
        mixed = client.get('/music/api/v1/playlist/batch-detail',
                           params={'guids': built_in+','+default['guid']}, headers=headers).json()['data']['list']
        assert next(row for row in mixed if row['guid'] == default['guid']) == actual
        response = client.get('/music/api/v1/static/cover', params={'id': cover}, headers=headers)
        assert response.status_code == 200 and response.headers['content-type'] == 'image/png'
        head = client.head('/music/api/v1/static/cover', params={'id': cover}, headers=headers)
        assert head.status_code == 200 and head.content == b''
        assert app.state.store.jobs('a') == []


@pytest.mark.parametrize('kind', ['hot', 'explore', 'archaeology'])
def test_builtin_playlist_cover_supports_query_path_head_and_auth(tmp_path, kind):
    from pathlib import Path

    upstream = []
    def official(req):
        upstream.append(req.url.path)
        return httpx.Response(200, json={'code': 0, 'data': {'guid': 'a'}})

    app = create_app(Config(data_dir=tmp_path),
                     httpx.AsyncClient(base_url='http://official', transport=httpx.MockTransport(official)),
                     httpx.AsyncClient(), httpx.AsyncClient(), worker=False)
    with TestClient(app) as client:
        card = next(c for c in app.state.playlists.cards('a')
                    if c['guid'] == app.state.playlists.guid('a', kind))
        cover = card['coverId']
        assert isinstance(cover, str) and len(cover) == 32
        body = (Path(__file__).parents[1] / f'fnmusic/static/playlist-{kind}-v1.png').read_bytes()
        for url in (f'/music/api/v1/static/cover?id={cover}',
                    f'/music/api/v1/static/cover?coverId={cover}',
                    f'/music/api/v1/static/cover/{cover}'):
            response = client.get(url, headers={'cookie': 'valid'})
            assert response.status_code == 200 and response.content == body
            assert response.headers['content-type'] == 'image/png'
            assert 'private' in response.headers['cache-control']
            head = client.head(url, headers={'cookie': 'valid'})
            assert head.status_code == 200 and head.content == b''
            assert int(head.headers['content-length']) == len(body)
        assert client.get(f'/music/api/v1/static/cover?id={cover}').status_code == 401
        assert set(upstream) == {'/music/api/v1/user/me'}


def test_archaeology_native_tracks_pagination_playback_and_user_isolation(tmp_path):
    from fnmusic.history import History

    calls = []
    native = {"guid": "local-guid", "title": "Local Song", "artists": [{"name": "Singer"}],
              "album": {"name": "Album"}, "duration": 1000,
              "audioSpec": {"path": "/vol1/music/a.flac", "format": "flac"}, "isFavorite": True}

    def official(req):
        calls.append(req.url.path)
        if req.url.path.endswith('user/me'):
            return httpx.Response(200, json={'code': 0, 'data': {'guid': req.headers.get('cookie')}})
        return httpx.Response(200, content=b'local-audio', headers={'content-type': 'audio/flac'})

    app = create_app(Config(data_dir=tmp_path),
                     httpx.AsyncClient(base_url='http://official', transport=httpx.MockTransport(official)),
                     httpx.AsyncClient(), httpx.AsyncClient(), worker=False)
    with TestClient(app) as client:
        async def archaeology(user, headers, api):
            assert headers['cookie'] == user
            return [native] if user == 'a' else []

        app.state.playlists.discovery.archaeology = archaeology
        guid = app.state.playlists.guid('a', 'archaeology')
        url = '/music/api/v1/track/playlist-detail/list'
        response = client.get(url, params={'playlistGUID': guid, 'size': -1}, headers={'cookie': 'a'})
        assert response.status_code == 200
        row = response.json()['data']['list'][0]
        assert row == native
        assert client.get(url, params={'playlistGUID': guid, 'page': 2, 'size': 1},
                          headers={'cookie': 'a'}).json()['data']['list'] == []
        other_guid = app.state.playlists.guid('b', 'archaeology')
        assert client.get(url, params={'playlistGUID': other_guid},
                          headers={'cookie': 'b'}).json()['data']['total'] == 0
        before = len(calls)
        assert client.get('/music/api/v1/track/stream?guid=local-guid',
                          headers={'cookie': 'a'}).content == b'local-audio'
        assert len(calls) == before + 1
        assert History(tmp_path).songs('a')[0]['title'] == 'Local Song'
        assert History(tmp_path).songs('b') == []
        assert app.state.store.jobs('a') == []


@pytest.mark.parametrize("size,page,expected", [(-1, 1, 30), (0, 1, 30), (10, 1, 10), (10, 2, 10), (10, 4, 0)])
@pytest.mark.parametrize("parameter", ["playlistGUID", "playlistGuid"])
def test_hot_playlist_mobile_all_tracks_and_regular_pagination(tmp_path, size, page, expected, parameter):
    def official(req):
        return httpx.Response(200, json={"code": 0, "data": {"guid": "a"}})

    def lx(req):
        return httpx.Response(200, json=[dict(RAW, songmid=str(i), name=f"歌曲{i}") for i in range(30)])

    app = create_app(
        Config(data_dir=tmp_path, hot_board="test-board"),
        httpx.AsyncClient(base_url="http://official", transport=httpx.MockTransport(official)),
        httpx.AsyncClient(base_url="http://lx", transport=httpx.MockTransport(lx)),
        httpx.AsyncClient(), worker=False,
    )
    with TestClient(app) as client:
        guid = app.state.playlists.guid("a", "hot")
        response = client.get(
            "/music/api/v1/track/playlist-detail/list",
            params={parameter: guid, "page": page, "size": size},
            headers={"cookie": "valid"},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 30
        assert len(data["list"]) == expected
        assert len({row["guid"] for row in data["list"]}) == expected
        assert app.state.store.jobs("a") == []


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


def test_native_q_search_does_not_fail_when_internal_local_query_requires_q(tmp_path):
    def official(req):
        if req.url.path.endswith("user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": "a"}})
        if req.url.params.get("q") != "青花瓷":
            return httpx.Response(200, json={"code": 400, "data": None})
        return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})

    oc = httpx.AsyncClient(
        base_url="http://official", transport=httpx.MockTransport(official)
    )
    lc = httpx.AsyncClient(
        base_url="http://lx",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[RAW])),
    )
    app = create_app(
        Config(data_dir=tmp_path, platforms=["kw"]),
        oc,
        lc,
        httpx.AsyncClient(),
        worker=False,
    )
    with TestClient(app) as c:
        result = c.get(
            "/music/api/v1/search/track?q=青花瓷", headers={"cookie": "valid"}
        )
        assert result.status_code == 200 and len(result.json()["data"]["list"]) == 1


def test_bad_online_row_cannot_break_valid_search_results(tmp_path):
    def official(req):
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"guid": "a"}
                if req.url.path.endswith("user/me")
                else {"list": [], "total": 0},
            },
        )

    oc = httpx.AsyncClient(
        base_url="http://official", transport=httpx.MockTransport(official)
    )
    lc = httpx.AsyncClient(
        base_url="http://lx",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json=[RAW, {"source": "kw", "name": "incomplete"}]
            )
        ),
    )
    app = create_app(
        Config(data_dir=tmp_path, platforms=["kw"]),
        oc,
        lc,
        httpx.AsyncClient(),
        worker=False,
    )
    with TestClient(app) as c:
        result = c.get("/music/api/v1/search/track?q=x", headers={"cookie": "valid"})
        assert result.status_code == 200 and len(result.json()["data"]["list"]) == 1


def test_local_search_failure_does_not_hide_online_results(tmp_path):
    def official(req):
        return (
            httpx.Response(200, json={"code": 0, "data": {"guid": "a"}})
            if req.url.path.endswith("user/me")
            else httpx.Response(200, json={"code": 123, "data": None})
        )

    oc = httpx.AsyncClient(
        base_url="http://official", transport=httpx.MockTransport(official)
    )
    lc = httpx.AsyncClient(
        base_url="http://lx",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[RAW])),
    )
    app = create_app(
        Config(data_dir=tmp_path, platforms=["kw"]),
        oc,
        lc,
        httpx.AsyncClient(),
        worker=False,
    )
    with TestClient(app) as c:
        response = c.get("/music/api/v1/search/track?q=x", headers={"cookie": "valid"})
        assert response.status_code == 200 and response.json()["data"]["total"] == 1
    from fnmusic.logs import read_events

    failures = [r for r in read_events(tmp_path) if r["event"] == "search.local_failed"]
    assert failures[0]["error"]["official_code"] == 123


def test_authenticated_search_history_records_keyword_result_and_cache_not_cookie(
    tmp_path,
):
    from fnmusic.logs import read_events

    def official(req):
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"guid": "private-user-guid"}
                if req.url.path.endswith("user/me")
                else {"list": [], "total": 0},
            },
        )

    oc = httpx.AsyncClient(
        base_url="http://official", transport=httpx.MockTransport(official)
    )
    lc = httpx.AsyncClient(
        base_url="http://lx",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[RAW])),
    )
    app = create_app(
        Config(data_dir=tmp_path, platforms=["kw"]),
        oc,
        lc,
        httpx.AsyncClient(),
        worker=False,
    )
    with TestClient(app) as c:
        for _ in range(2):
            assert (
                c.get(
                    "/music/api/v1/search/track?q=青花瓷",
                    headers={"cookie": "credential-secret"},
                ).status_code
                == 200
            )
    rows = [r for r in read_events(tmp_path) if r["component"] == "history"]
    assert len(rows) == 2
    assert rows[0]["keyword"] == "青花瓷" and rows[0]["total"] == 1
    assert rows[1]["cache_hit"] is True
    assert "credential-secret" not in str(rows) and "private-user-guid" not in str(rows)


def test_timeout_log_identifies_actual_forwarded_official_route(tmp_path):
    from fnmusic.logs import read_events

    def timeout(req):
        raise httpx.ReadTimeout("sensitive raw text", request=req)

    oc = httpx.AsyncClient(
        base_url="http://official", transport=httpx.MockTransport(timeout)
    )
    app = create_app(
        Config(data_dir=tmp_path),
        oc,
        httpx.AsyncClient(),
        httpx.AsyncClient(),
        worker=False,
    )
    with TestClient(app) as c:
        assert c.get("/music/api/v1/events/poll?token=private").status_code == 502
    row = next(r for r in read_events(tmp_path) if r["event"] == "upstream.failed")
    assert row["error"]["upstream"] == "official"
    assert row["error"]["route"] == "/music/api/v1/events/poll"
    assert any(f["file"] == "official.py" for f in row["error"]["frames"])
    assert "private" not in str(row) and "sensitive" not in str(row)


def test_play_history_distinguishes_redirect_and_ignores_head_and_continuation(
    tmp_path,
):
    from fnmusic.logs import read_events

    def official(req):
        return httpx.Response(200, json={"code": 0, "data": {"guid": "user-a"}})

    def lx(req):
        if req.url.path.endswith("/list"):
            return httpx.Response(
                200, json=[{"enabled": True, "sources": {"kw": {"qualitys": ["320k"]}}}]
            )
        return httpx.Response(
            200, json={"url": "https://93.184.216.34/audio?signature=private"}
        )

    oc = httpx.AsyncClient(
        base_url="http://official", transport=httpx.MockTransport(official)
    )
    lc = httpx.AsyncClient(base_url="http://lx", transport=httpx.MockTransport(lx))
    mc = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                206, content=b"a", headers={"content-type": "audio/mpeg"}
            )
        )
    )
    app = create_app(
        Config(data_dir=tmp_path, platforms=["kw"]), oc, lc, mc, worker=False
    )
    with TestClient(app) as c:
        guid = app.state.store.save_track(RAW)["guid"]
        url = "/music/api/v1/track/stream?guid=" + guid
        assert (
            c.get(
                url, headers={"cookie": "private-cookie"}, follow_redirects=False
            ).status_code
            == 302
        )
        assert (
            c.head(
                url, headers={"cookie": "private-cookie"}, follow_redirects=False
            ).status_code
            == 302
        )
        assert (
            c.get(
                url,
                headers={"cookie": "private-cookie", "range": "bytes=1-"},
                follow_redirects=False,
            ).status_code
            == 302
        )
    rows = [r for r in read_events(tmp_path) if r["component"] == "history"]
    assert [r["event"] for r in rows] == ["play.requested", "play.redirect"]
    assert rows[-1]["title"] == RAW["name"] and rows[-1]["quality"] == "320k"
    assert "signature" not in str(rows) and "private-cookie" not in str(rows)


def test_native_local_play_history_preserves_transparent_forward_without_extra_calls(
    tmp_path,
):
    from fnmusic.logs import read_events

    calls = []

    def official(req):
        calls.append(req)
        return httpx.Response(
            200, content=b"local-audio", headers={"content-type": "audio/mpeg"}
        )

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
    with TestClient(app) as c:
        response = c.get(
            "/music/api/v1/track/stream?guid=" + "a" * 32,
            headers={"cookie": "private-cookie"},
        )
        assert response.content == b"local-audio"
    assert len(calls) == 1
    row = next(r for r in read_events(tmp_path) if r["event"] == "play.local_forwarded")
    assert row["source"] == "official" and row["task"] == "a" * 32
    assert "private-cookie" not in str(row)


def test_identity_timeout_logs_interface_without_unauthenticated_search_term(tmp_path):
    from fnmusic.logs import read_events

    def timeout(req):
        raise httpx.ReadTimeout("credential-must-not-appear", request=req)

    oc = httpx.AsyncClient(
        base_url="http://official", transport=httpx.MockTransport(timeout)
    )
    app = create_app(
        Config(data_dir=tmp_path),
        oc,
        httpx.AsyncClient(),
        httpx.AsyncClient(),
        worker=False,
    )
    with TestClient(app) as c:
        assert (
            c.get(
                "/music/api/v1/search/track?q=private-search",
                headers={"cookie": "private-cookie"},
            ).status_code
            == 503
        )
    rows = read_events(tmp_path)
    failure = next(r for r in rows if r["event"] == "identity.failed")
    assert failure["error"]["upstream"] == "official"
    assert failure["error"]["route"] == "/music/api/v1/user/me"
    assert "private-search" not in str(rows) and "private-cookie" not in str(rows)


def test_hls_range_retry_is_not_a_second_play_record(tmp_path):
    from starlette.responses import Response
    from fnmusic.logs import read_events

    oc = httpx.AsyncClient(
        base_url="http://official",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"code": 0, "data": {"guid": "a"}})
        ),
    )
    app = create_app(
        Config(data_dir=tmp_path),
        oc,
        httpx.AsyncClient(),
        httpx.AsyncClient(),
        worker=False,
    )
    with TestClient(app) as c:
        guid = app.state.store.save_track(RAW)["guid"]

        async def audio(*args):
            return Response(b"aac", media_type="audio/aac")

        app.state.transcode.audio = audio
        url = "/music/api/v1/track/hls/" + guid + "/audio.aac"
        assert c.get(url, headers={"cookie": "valid"}).status_code == 200
        assert (
            c.get(url, headers={"cookie": "valid", "range": "bytes=1-"}).status_code
            == 200
        )
    rows = [r for r in read_events(tmp_path) if r["component"] == "history"]
    assert [r["event"] for r in rows] == ["play.requested", "play.hls_started"]
