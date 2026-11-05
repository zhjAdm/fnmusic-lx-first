import httpx
import pytest
from fastapi import HTTPException
from test_core import RAW

from fnmusic.config import Config
from fnmusic.media import Media, public_url
from fnmusic.media import stream_response
from fnmusic.official import Official
from fnmusic.store import Store


class FakeLX:
    async def qualities(self, source):
        return ["flac", "320k", "128k"]

    async def resolve(self, raw, quality):
        return {"url": "https://93.184.216.34/audio.mp3"}


def official_response(req):
    if req.url.path.endswith("user/me"):
        if req.headers.get("cookie") != "music-token=ok":
            return httpx.Response(401)
        return httpx.Response(200, json={"code": 0, "data": {"guid": "a"}})
    return httpx.Response(200, json={"code": 0, "data": {"list": []}})


@pytest.mark.asyncio
async def test_auth_is_fail_closed():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(official_response), base_url="http://official"
    ) as c:
        official = Official(c, Config())
        for headers in ({}, {"cookie": "wrong"}):
            with pytest.raises(HTTPException):
                await official.user(headers)
        assert await official.user({"cookie": "music-token=ok"}) == "a"


@pytest.mark.asyncio
async def test_missing_user_identity_is_not_shared():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"code": 0, "data": {}})
        ),
        base_url="http://official",
    ) as c:
        with pytest.raises(HTTPException):
            await Official(c, Config()).user({"cookie": "x"})


@pytest.mark.asyncio
async def test_direct_play_does_not_write_audio(tmp_path):
    store = Store(tmp_path / "state.db")
    track = store.save_track(RAW)

    def upstream(req):
        assert "cookie" not in req.headers
        assert "authorization" not in req.headers
        return httpx.Response(
            206,
            headers={"content-type": "audio/mpeg", "content-range": "bytes 0-0/100"},
            content=b"x",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as c:
        media = Media(c, FakeLX(), None, store, Config(download_dir=tmp_path / "music"))
        response = await media.play(track, "a", {"cookie": "secret"}, None, False)
        assert response.status_code == 302
        assert not (tmp_path / "music").exists()
        assert store.jobs("a") == []
    store.close()


@pytest.mark.asyncio
async def test_proxy_range_is_same_resource(tmp_path):
    store = Store(tmp_path / "s.db")
    track = store.save_track(RAW)
    seen = []

    def handle(req):
        seen.append(req.headers.get("range"))
        return httpx.Response(
            206,
            content=b"audio",
            headers={"content-type": "audio/mpeg", "content-range": "bytes 5-9/10"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
        media = Media(c, FakeLX(), None, store, Config(play_mode="proxy"))
        with pytest.raises(HTTPException) as error:
            await media.play(track, "a", {}, "bytes=5-", False)
        assert error.value.status_code == 409
        media.remember("a", track["guid"], {"url": "https://93.184.216.34/a"})
        response = await media.play(track, "a", {}, "bytes=5-", False)
        assert response.status_code == 206
        assert b"".join([x async for x in response.body_iterator]) == b"audio"
        assert seen == ["bytes=5-"]
    store.close()


@pytest.mark.asyncio
async def test_online_failure_uses_local(tmp_path):
    store = Store(tmp_path / "s.db")
    track = store.save_track(RAW)

    class Broken(FakeLX):
        async def resolve(self, *args):
            raise ValueError("down")

    class Local:
        async def find_local(self, *args, **kwargs):
            return {"guid": "local"}

        async def stream(self, *args):
            from starlette.responses import Response

            return Response("local-stream")

    async with httpx.AsyncClient() as c:
        media = Media(c, Broken(), Local(), store, Config())
        response = await media.play(track, "a", {}, None, False)
        assert response.body == b"local-stream"
        assert response.fnmusic_mode == "local"
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a",
        "https://[::1]/",
        "https://169.254.169.254/",
        "file:///etc/passwd",
        "https://user:pass@93.184.216.34/a",
    ],
)
async def test_private_media_rejected(url):
    with pytest.raises(ValueError):
        await public_url(url)


@pytest.mark.asyncio
async def test_stream_closed_even_before_body_starts():
    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"a" * 65536
            yield b"b" * 65536

        async def aclose(self):
            self.closed = True

    upstream = Body()
    response = httpx.Response(
        200, stream=upstream, headers={"content-type": "audio/mpeg"}
    )
    result = await stream_response(response)
    await result.aclose()
    assert upstream.closed


@pytest.mark.asyncio
async def test_unavailable_play_quality_downgrades_to_declared_quality(tmp_path):
    class Limited(FakeLX):
        async def qualities(self, source):
            return ["128k"]

    s = Store(tmp_path / "s.db")
    t = s.save_track(RAW)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=b"x", headers={"content-type": "audio/mpeg"}
            )
        )
    ) as client:
        m = Media(client, Limited(), None, s, Config(play_quality="320k"))
        response = await m.play(t, "a", {}, None, False)
        assert response.status_code == 302
    s.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [204, 403, 404, 429, 500])
async def test_media_rejection_keeps_status_and_safe_request_diagnostics(status):
    from fnmusic.media import open_media
    from fnmusic.logs import error_summary
    import json

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(status, headers={'x-private': 'private-header'})
    )) as client:
        with pytest.raises(ValueError) as caught:
            await open_media(client, 'GET', 'https://93.184.216.34/private/audio?signature=private-key')
        detail = error_summary(caught.value)
        assert detail['http_status'] == status
        assert detail['reason'] == 'http_status'
        assert detail['upstream'] == 'media'
        assert detail['route'] == '[remote-media]'
        assert caught.value.response.is_closed
        assert 'private' not in json.dumps(detail)


@pytest.mark.asyncio
async def test_media_html_response_is_distinct_from_http_failure():
    from fnmusic.media import open_media
    from fnmusic.logs import error_summary

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, headers={'content-type': 'text/html'}, text='private body')
    )) as client:
        with pytest.raises(ValueError) as caught:
            await open_media(client, 'GET', 'https://93.184.216.34/audio')
        detail = error_summary(caught.value)
        assert detail['http_status'] == 200 and detail['reason'] == 'not_audio'
        assert caught.value.response.is_closed


@pytest.mark.asyncio
async def test_final_play_failure_retains_safe_last_media_cause(tmp_path):
    from fnmusic.logs import error_summary

    store = Store(tmp_path / 's.db')
    track = store.save_track(RAW)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(403)
    )) as client:
        media = Media(client, FakeLX(), None, store, Config(data_dir=tmp_path / 'data'))
        with pytest.raises(HTTPException) as caught:
            await media.play(track, 'a', {}, None, False, force_proxy=True)
        detail = error_summary(caught.value)
        assert detail['http_status'] == 502
        assert detail['cause']['http_status'] == 403
        assert detail['cause']['upstream'] == 'media'
        assert detail['cause']['reason'] == 'http_status'
    store.close()
