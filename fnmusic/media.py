"""Bounded streaming only; this module never writes an audio file."""

import asyncio
import ipaddress
import socket
import time
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import HTTPException
from starlette.responses import RedirectResponse, Response
from .streams import ClosingStream
from .models import QUALITIES, quality_order
from .logs import event

MEDIA_HEADERS = {
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "etag",
    "last-modified",
}


async def public_url(url):
    value = urlsplit(url)
    if (
        value.scheme not in ("http", "https")
        or not value.hostname
        or value.username
        or value.password
    ):
        raise ValueError("Invalid public media URL")
    try:
        addresses = [ipaddress.ip_address(value.hostname)]
    except ValueError:
        resolved = await asyncio.get_running_loop().getaddrinfo(
            value.hostname,
            value.port or (443 if value.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        addresses = [ipaddress.ip_address(item[4][0]) for item in resolved]
    if not addresses or any(not ip.is_global for ip in addresses):
        raise ValueError("Private media target rejected")
    return url


async def open_media(client, method, url, headers=None):
    # Validate every redirect; no official credentials are accepted by this API.
    safe = {
        k: str(v)
        for k, v in (headers or {}).items()
        if k.lower() in {"range", "user-agent", "referer", "origin", "accept"}
    }
    safe["accept-encoding"] = "identity"
    for _ in range(6):
        await public_url(url)
        response = await client.send(
            client.build_request(
                method, url, headers=safe, extensions={"fnmusic_upstream": "media"}
            ),
            stream=True,
            follow_redirects=False,
        )
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise ValueError("Missing media redirect")
            url = urljoin(url, location)
            continue
        if response.status_code not in (200, 206):
            await response.aclose()
            raise ValueError("Audio upstream unavailable")
        content_type = response.headers.get("content-type", "").lower()
        if not (
            content_type.startswith("audio/")
            or "octet-stream" in content_type
            or "application/ogg" in content_type
        ):
            await response.aclose()
            raise ValueError("Upstream did not return audio")
        return response
    raise ValueError("Too many media redirects")


async def stream_response(response, head=False):
    headers = {k: v for k, v in response.headers.items() if k.lower() in MEDIA_HEADERS}
    headers.update({"X-Accel-Buffering": "no", "Cache-Control": "no-store"})
    if head:
        await response.aclose()
        return Response(status_code=response.status_code, headers=headers)
    iterator = response.aiter_bytes(65536)
    try:
        first = await anext(iterator)
    except BaseException:
        await response.aclose()
        raise

    async def body():
        try:
            yield first
            async for chunk in iterator:
                yield chunk
        finally:
            await response.aclose()

    return ClosingStream(
        body(), status_code=response.status_code, headers=headers, close=response.aclose
    )


class Media:
    def __init__(self, client, lx, official, store, config):
        self.client, self.lx, self.official, self.store, self.config = (
            client,
            lx,
            official,
            store,
            config,
        )
        self.bindings = {}

    def remember(self, user, guid, target):
        now = time.monotonic()
        self.bindings = {k: v for k, v in self.bindings.items() if v[0] > now}
        if len(self.bindings) >= 2048:
            self.bindings.pop(next(iter(self.bindings)))
        self.bindings[(user, guid)] = (now + 300, target)

    async def play(
        self, track, user, headers, range_header, head, *, force_proxy=False
    ):
        continuation = bool(range_header and not range_header.startswith("bytes=0-"))
        binding = self.bindings.get((user, track["guid"]))
        if continuation:
            if not binding or binding[0] <= time.monotonic():
                raise HTTPException(409, "播放资源已过期，请重新播放")
            target = binding[1]
            if "local" in target:
                return await self.official.stream(
                    target["local"], headers, range_header, head
                )
            return await self.deliver(
                target, range_header, head, force_proxy=force_proxy
            )
        try:
            qualities = await self.lx.qualities(track["source"])
            floor = QUALITIES.index(self.config.play_quality)
            qualities = [
                q for q in quality_order(qualities) if QUALITIES.index(q) >= floor
            ]
            for quality in qualities:
                try:
                    target = await self.lx.resolve(track["raw"], quality)
                    result = await self.deliver(
                        target, range_header, head, force_proxy=force_proxy
                    )
                    self.remember(user, track["guid"], target)
                    result.fnmusic_mode = (
                        "direct" if result.status_code == 302 else "proxy"
                    )
                    result.fnmusic_quality = quality
                    return result
                except (
                    httpx.HTTPError,
                    ValueError,
                    OSError,
                    StopAsyncIteration,
                ) as exc:
                    event(
                        self.config.data_dir,
                        "proxy",
                        "play.quality_failed",
                        level="WARNING",
                        error=exc,
                        task=track["guid"],
                        quality=quality,
                        source=track.get("source", ""),
                    )
                    continue
        except (httpx.HTTPError, ValueError, OSError, RuntimeError) as exc:
            event(
                self.config.data_dir,
                "proxy",
                "play.source_failed",
                level="WARNING",
                error=exc,
                task=track["guid"],
                source=track.get("source", ""),
            )
        if self.official:
            local = await self.official.find_local(track, headers)
            if local:
                self.remember(user, track["guid"], {"local": local["guid"]})
                result = await self.official.stream(
                    local["guid"], headers, range_header, head
                )
                result.fnmusic_mode = "local"
                return result
        raise HTTPException(502, "在线音源和本地曲库均无可用资源")

    async def deliver(self, target, range_header, head, *, force_proxy=False):
        url = target["url"]
        media_headers = dict(target.get("headers") or {})
        if (
            not force_proxy
            and self.config.play_mode == "direct"
            and url.startswith("https://")
            and not media_headers
        ):
            try:
                probe = await open_media(
                    self.client, "GET", url, {"range": "bytes=0-0"}
                )
                await probe.aclose()
                return RedirectResponse(
                    url,
                    status_code=302,
                    headers={
                        "Cache-Control": "no-store",
                        "Referrer-Policy": "no-referrer",
                    },
                )
            except (httpx.HTTPError, ValueError, OSError) as exc:
                event(self.config.data_dir, "proxy", "play.direct_fallback", error=exc)
        if range_header:
            media_headers["range"] = range_header
        response = await open_media(
            self.client, "HEAD" if head else "GET", url, media_headers
        )
        if (
            range_header
            and not range_header.startswith("bytes=0-")
            and response.status_code != 206
        ):
            await response.aclose()
            raise HTTPException(409, "音源不支持当前断点，请重新播放")
        return await stream_response(response, head)
