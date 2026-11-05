import httpx
from fastapi import HTTPException
from starlette.responses import Response

from .models import normalize
from .streams import ClosingStream

AUTH_HEADERS = {"cookie", "authorization", "x-auth-token", "x-access-token"}
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
}


def session_headers(headers):
    return {k: v for k, v in headers.items() if k.lower() in AUTH_HEADERS}


def forward_headers(headers):
    excluded = HOP_HEADERS | {
        x.strip().lower() for x in headers.get("connection", "").split(",")
    }
    return {k: v for k, v in headers.items() if k.lower() not in excluded}


class Official:
    def __init__(self, client, config):
        self.client, self.config = client, config

    async def api(self, method, path, headers, **kwargs):
        response = await self.client.request(
            method, path, headers=session_headers(headers), **kwargs
        )
        if response.status_code in (401, 403):
            raise HTTPException(401, "飞牛登录已失效")
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get("code") != 0:
            raise ValueError("Official API rejected operation")
        return data.get("data")

    async def user(self, headers):
        if not session_headers(headers):
            raise HTTPException(401, "请先登录飞牛音乐")
        try:
            data = await self.api("GET", "/music/api/v1/user/me", headers)
        except HTTPException:
            raise
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, "无法确认飞牛登录状态") from None
        if not isinstance(data, dict) or not data.get("guid"):
            raise HTTPException(401, "无法确认用户身份")
        return str(data["guid"])

    async def search(self, title, headers, page=1, size=100):
        data = await self.api(
            "GET",
            "/music/api/v1/search/track",
            headers,
            params={"keyword": title, "page": page, "size": size},
        )
        return data if isinstance(data, dict) else {"list": [], "total": 0}

    async def list_all(self, path, headers, **params):
        rows = []
        seen = set()
        first = {}
        for page in range(1, 101):
            data = await self.api(
                "GET", path, headers, params=dict(params, page=page, size=100)
            )
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise ValueError("Official list protocol mismatch")
            if page == 1:
                first = data
            batch = data["list"]
            before = len(rows)
            for row in batch:
                guid = row.get("guid")
                if guid and guid not in seen:
                    rows.append(row)
                    seen.add(guid)
            total = data.get("total")
            if not batch or (isinstance(total, int) and len(rows) >= total):
                return dict(first, list=rows, total=len(rows))
            if len(rows) == before:
                raise ValueError("Official pagination did not advance")
        raise ValueError("Official list exceeds supported size")

    async def find_local(self, track, headers, path=None):
        data = await self.search(track["title"], headers)
        for row in data.get("list", []):
            if normalize(row.get("title")) != normalize(track["title"]):
                continue
            artists = "、".join(a.get("name", "") for a in row.get("artists", []))
            if normalize(artists) != normalize(track["artist"]):
                continue
            if path is not None:
                if row.get("audioSpec", {}).get("path") != str(path):
                    continue
            else:
                album = (row.get("album") or {}).get("name")
                if track["album"] != "未知专辑" and normalize(album) != normalize(
                    track["album"]
                ):
                    continue
                duration = int(row.get("duration") or 0)
                if track["duration"] and (
                    not duration or abs(duration - track["duration"]) > 3000
                ):
                    continue
            return row
        return None

    async def passthrough(self, method, path, headers, content=b""):
        request = self.client.build_request(
            method, path, headers=forward_headers(headers), content=content
        )
        response = await self.client.send(request, stream=True)
        clean = forward_headers(response.headers)
        # httpx decodes content only when asked; raw streaming preserves encoding/length.
        if method == "HEAD":
            await response.aclose()
            result = Response(status_code=response.status_code)
        else:

            async def body():
                try:
                    async for chunk in response.aiter_raw():
                        yield chunk
                finally:
                    await response.aclose()

            if response.is_stream_consumed:
                result = Response(
                    content=response.content, status_code=response.status_code
                )
                await response.aclose()
                clean = {
                    k: v
                    for k, v in clean.items()
                    if k.lower() not in {"content-encoding", "content-length"}
                }
            else:
                result = ClosingStream(
                    body(), status_code=response.status_code, close=response.aclose
                )
        result.raw_headers = [
            (k, v)
            for k, v in response.headers.raw
            if k.decode().lower() in {x.lower() for x in clean}
        ]
        return result

    async def stream(self, guid, headers, range_header=None, head=False):
        safe = session_headers(headers)
        if range_header:
            safe["range"] = range_header
        path = str(httpx.URL("/music/api/v1/track/stream", params={"guid": guid}))
        return await self.passthrough("HEAD" if head else "GET", path, safe)

    async def scan(self, headers):
        if self.config.scan_path:
            await self.api(
                "POST", self.config.scan_path, headers, json=self.config.scan_body
            )

    async def default_playlist(self, headers):
        data = await self.api(
            "GET",
            "/music/api/v1/playlist/list",
            headers,
            params={"page": 1, "size": 1000},
        )
        rows = (data or {}).get("list", [])
        if self.config.default_playlist_guid:
            matches = [
                x for x in rows if x.get("guid") == self.config.default_playlist_guid
            ]
            if len(matches) != 1:
                raise ValueError("Configured playlist is not accessible")
            return matches[0]["guid"]
        matches = [x for x in rows if x.get("name") == self.config.playlist_name]
        if len(matches) > 1:
            raise ValueError(
                "Multiple default playlists; configure DEFAULT_PLAYLIST_GUID"
            )
        if matches:
            return matches[0]["guid"]
        data = await self.api(
            "POST",
            "/music/api/v1/playlist/create",
            headers,
            json={"name": self.config.playlist_name},
        )
        if isinstance(data, dict) and data.get("guid"):
            return data["guid"]
        # Some versions return no payload from writes. Re-read; never invent a GUID.
        data = await self.api(
            "GET",
            "/music/api/v1/playlist/list",
            headers,
            params={"page": 1, "size": 1000},
        )
        matches = [
            x
            for x in (data or {}).get("list", [])
            if x.get("name") == self.config.playlist_name
        ]
        if len(matches) != 1:
            raise ValueError("Default playlist creation not confirmed")
        return matches[0]["guid"]
