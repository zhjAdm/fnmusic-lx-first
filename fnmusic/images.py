from urllib.parse import urljoin

from fastapi import HTTPException
from starlette.responses import Response

from .media import public_url


async def image_response(client, url, head=False):
    if not url:
        return Response(status_code=404)
    for _ in range(6):
        await public_url(url)
        response = await client.send(
            client.build_request(
                "GET",
                url,
                headers={"accept": "image/*", "accept-encoding": "identity"},
                extensions={"fnmusic_upstream": "image"},
            ),
            stream=True,
            follow_redirects=False,
        )
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise ValueError("Invalid image redirect")
                url = urljoin(url, location)
                continue
            kind = response.headers.get("content-type", "").split(";")[0]
            if response.status_code != 200 or kind not in {
                "image/jpeg",
                "image/png",
                "image/webp",
                "image/gif",
                "image/avif",
            }:
                return Response(status_code=404)
            body = bytearray()
            async for part in response.aiter_bytes(65536):
                body.extend(part)
                if len(body) > 10 * 1024 * 1024:
                    raise ValueError("Image exceeds size limit")
            return Response(
                content=b"" if head else bytes(body),
                media_type=kind,
                headers={
                    "Cache-Control": "private, max-age=900",
                    "Content-Length": str(len(body)),
                },
            )
        finally:
            await response.aclose()
    raise HTTPException(502, "封面重定向次数过多")
