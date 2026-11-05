import httpx
import pytest

from fnmusic.images import image_response


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
