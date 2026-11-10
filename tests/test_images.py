import httpx
import pytest

from fnmusic.images import image_response


@pytest.mark.parametrize('kind', ['hot', 'explore', 'archaeology', 'default'])
def test_playlist_cover_ids_are_content_based_and_resolve_only_bundled_art(kind):
    import hashlib
    import struct
    from fnmusic.images import playlist_cover_id, playlist_cover_path

    guid = playlist_cover_id(kind)
    path = playlist_cover_path(guid)
    data = path.read_bytes()
    assert guid == hashlib.md5(data, usedforsecurity=False).hexdigest()
    assert data.startswith(b'\x89PNG\r\n\x1a\n')
    width, height = struct.unpack('>II', data[16:24])
    assert width == height and width >= 512
    assert playlist_cover_id('public:0') is None
    assert playlist_cover_path('../../private') is None
    assert playlist_cover_path('unknown') is None


@pytest.mark.asyncio
async def test_image_proxy_never_forwards_credentials():
    def handler(req):
        assert "cookie" not in req.headers
        return httpx.Response(
            200, content=b"png", headers={"content-type": "image/png"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        response = await image_response(c, "https://93.184.216.34/a.png", False)
        assert response.media_type == "image/png" and response.body == b"png"
