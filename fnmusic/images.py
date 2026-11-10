from functools import lru_cache
import hashlib
from pathlib import Path
from urllib.parse import urljoin

from fastapi import HTTPException
from starlette.responses import Response

from .media import public_url


@lru_cache(maxsize=1)
def _playlist_covers():
    """Trusted bundled assets only; content IDs change when artwork is replaced."""
    static = Path(__file__).parent / "static"
    covers = {}
    for kind in ("hot", "explore", "archaeology", "default"):
        path = static / f"playlist-{kind}-v1.png"
        guid = hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
        covers[kind] = (guid, path)
    return covers


def playlist_cover_id(kind):
    entry = _playlist_covers().get(kind)
    return entry[0] if entry else None


def playlist_cover_path(guid):
    return next((path for cover, path in _playlist_covers().values() if cover == guid), None)


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
