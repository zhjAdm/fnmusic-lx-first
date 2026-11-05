import asyncio
import hashlib
import time


class Playlists:
    def __init__(self, store, lx, config):
        self.store, self.lx, self.config = store, lx, config
        self.cache = {}

    @staticmethod
    def guid(user, kind):
        return hashlib.md5(
            f"fnmusic-playlist:{user}:{kind}".encode(), usedforsecurity=False
        ).hexdigest()

    def definitions(self):
        return [("hot", "热门推荐"), ("liked", "猜你喜欢")] + [
            (f"public:{i}", row.get("name") or "在线歌单")
            for i, row in enumerate(self.config.public_playlists)
        ]

    def cards(self, user):
        return [
            {
                "guid": self.guid(user, key),
                "name": name,
                "coverId": None,
                "trackCount": len(self.cache.get((user, key), (0, []))[1]),
                "createdAt": 0,
                "updatedAt": 0,
                "description": "在线歌单，点击喜欢才下载",
            }
            for key, name in self.definitions()
        ]

    def key(self, user, guid):
        for key, _ in self.definitions():
            if self.guid(user, key) == guid:
                return key
        raise KeyError("Unknown playlist for this user")

    async def tracks(self, user, guid):
        key = self.key(user, guid)
        cached = self.cache.get((user, key))
        if cached and cached[0] > time.monotonic():
            return cached[1]
        if key == "hot":
            raws = await self.lx.hot(self.config.hot_source, self.config.hot_board)
        elif key == "liked":
            favorites = self.store.favorites(user)
            artists = list(dict.fromkeys(t["artist"] for t in favorites))[:3]
            groups = await asyncio.gather(
                *(self.lx.search(artist, 1, 20) for artist in artists),
                return_exceptions=True,
            )
            raws = [x for group in groups if isinstance(group, list) for x in group]
        else:
            entry = self.config.public_playlists[int(key.split(":")[1])]
            raws = []
            # Bounded pagination; fail rather than claim an incomplete imported list.
            for page in range(1, 101):
                data = await self.lx.playlist(entry["source"], str(entry["id"]), page)
                rows = data["list"]
                old = len(raws)
                for raw in rows:
                    raw = dict(raw, source=raw.get("source") or entry["source"])
                    if raw not in raws:
                        raws.append(raw)
                total = data.get("total", data.get("totalNum"))
                if not rows or (isinstance(total, int) and len(raws) >= total):
                    break
                if len(raws) == old:
                    raise ValueError("Playlist pagination did not advance")
            else:
                raise ValueError("Playlist exceeds 100 pages")
        tracks = []
        seen = set()
        for raw in raws:
            track = self.store.save_track(raw)
            if track["guid"] in seen:
                continue
            if key == "liked" and self.store.is_liked(user, track["guid"]):
                continue
            tracks.append(track)
            seen.add(track["guid"])
            if key in {"hot", "liked"} and len(tracks) >= 30:
                break
        if len(self.cache) > 500:
            self.cache = {
                k: v for k, v in self.cache.items() if v[0] > time.monotonic()
            }
            if len(self.cache) > 500:
                self.cache.pop(next(iter(self.cache)))
        self.cache[(user, key)] = (time.monotonic() + 900, tracks)
        return tracks

    def invalidate(self, user):
        self.cache.pop((user, "liked"), None)
