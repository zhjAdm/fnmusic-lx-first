"""Small client for the pinned lxserver v2.1.2 HTTP contracts."""

import asyncio
import anyio
from pathlib import Path

from .models import quality_order


class LX:
    def __init__(self, client, password, platforms):
        self.client, self.platforms = client, platforms
        self.client.headers["x-frontend-auth"] = password

    async def call(self, method, path, **kwargs):
        response = await self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def search(self, keyword, page=1, limit=20):
        async def one(source):
            data = await self.call(
                "GET",
                "/api/music/search",
                params={
                    "name": keyword,
                    "source": source,
                    "page": page,
                    "pages": 1,
                    "limit": limit,
                },
            )
            if not isinstance(data, list):
                raise ValueError("LX search protocol mismatch")
            return [
                dict(row, source=row.get("source") or source)
                for row in data
                if isinstance(row, dict)
            ]

        groups = await asyncio.gather(
            *(one(p) for p in self.platforms), return_exceptions=True
        )
        successes = [g for g in groups if isinstance(g, list)]
        if not successes:
            raise RuntimeError("LX search unavailable")
        return [row for group in successes for row in group][:limit]

    async def resolve(self, raw, quality):
        data = await self.call(
            "POST",
            "/api/music/url",
            json={
                "songInfo": raw,
                "quality": quality,
                "enableAutoSwitchApiSource": False,
            },
        )
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("url"), str)
            or not data["url"]
        ):
            raise ValueError("LX did not return an audio URL")
        return data

    async def qualities(self, source):
        data = await self.call("GET", "/api/custom-source/list")
        scripts = data if isinstance(data, list) else data.get("sources", [])
        supported = set()
        for script in scripts:
            if not script.get("enabled"):
                continue
            platform = (script.get("sources") or {}).get(source, {})
            supported.update(platform.get("qualitys", []))
        return quality_order(supported)

    async def import_script(self, path):
        path = Path(path)
        result = await self.call(
            "POST",
            "/api/custom-source/upload",
            json={
                "filename": "private-source.js",
                "content": path.read_text(),
                "allowUnsafeVM": False,
            },
        )
        if not result.get("success") or not result.get("id"):
            raise ValueError("Script import rejected; inspect lxserver locally")
        identifier = result["id"]
        try:
            toggled = await self.call(
                "POST",
                "/api/custom-source/toggle",
                json={"id": identifier, "enabled": True},
            )
            if toggled.get("success") is not True or toggled.get("enabled") is not True:
                raise ValueError("Source activation rejected")
            for _ in range(40):
                catalog = await self.call("GET", "/api/custom-source/list")
                rows = (
                    catalog if isinstance(catalog, list) else catalog.get("sources", [])
                )
                loaded = next((row for row in rows if row.get("id") == identifier), {})
                if (
                    loaded.get("enabled")
                    and loaded.get("status") == "success"
                    and any(
                        (loaded.get("sources") or {}).get(p, {}).get("qualitys")
                        for p in self.platforms
                    )
                ):
                    return identifier
                if loaded.get("status") == "failed":
                    break
                await asyncio.sleep(0.1)
            raise ValueError("Source did not register usable platforms")
        except BaseException:
            with anyio.CancelScope(shield=True):
                try:
                    await self.call(
                        "POST", "/api/custom-source/delete", json={"id": identifier}
                    )
                except Exception:
                    pass
            raise

    async def lyric(self, raw):
        return await self.call("POST", "/api/music/lyric", json={"songInfo": raw})

    async def playlist(self, source, identifier, page=1):
        data = await self.call(
            "GET",
            "/api/music/songList/detail",
            params={"source": source, "id": identifier, "page": page},
        )
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise ValueError("LX playlist protocol mismatch")
        return data

    async def hot(self, source, board):
        if not board:
            data = await self.call(
                "GET", "/api/music/leaderboard/boards", params={"source": source}
            )
            boards = data if isinstance(data, list) else data.get("list", [])
            if not boards:
                return []
            board = boards[0].get("bangid") or boards[0].get("id")
        data = await self.call(
            "GET",
            "/api/music/leaderboard/list",
            params={"source": source, "bangid": board, "page": 1},
        )
        rows = data if isinstance(data, list) else data.get("list", [])
        return [dict(row, source=row.get("source") or source) for row in rows]
