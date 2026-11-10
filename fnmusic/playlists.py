import asyncio
import hashlib
import time
from datetime import datetime, timedelta, timezone
from itertools import zip_longest

from .logs import event
from .models import normalize
from .discovery import Discovery, day_now
from .library import Library
from .official import session_key
from .images import playlist_cover_id

BEIJING = timezone(timedelta(hours=8))


class Playlists:
    def __init__(self, store, lx, config):
        self.store, self.lx, self.config = store, lx, config
        self.cache = {}
        self.discovery = Discovery(config, store, lx)
        self.explore_modes = {}
        self.native_cache = {}
        self.hot_lock = asyncio.Lock()
        self.hot_retry_at = 0
        self.task = None

    @property
    def hot_key(self):
        return "hot:" + hashlib.sha256(
            repr((self.config.hot_source, self.config.hot_board)).encode()
        ).hexdigest()

    def start(self):
        self.task = asyncio.create_task(self.loop())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def loop(self):
        while True:
            try:
                await self.refresh_hot()
            except Exception:
                # refresh_hot records the failure and applies a retry cooldown.
                pass
            await asyncio.sleep(60)

    async def refresh_hot(self, *, force=False):
        """Daily shared metadata snapshot; never enqueues favorites or downloads."""
        async with self.hot_lock:
            now = time.time()
            day = datetime.fromtimestamp(now, BEIJING).date().isoformat()
            snapshot = self.store.playlist_snapshot(self.hot_key)
            if not force and snapshot and snapshot["day"] == day:
                return snapshot["tracks"]
            if not force and time.monotonic() < self.hot_retry_at:
                if snapshot:
                    return snapshot["tracks"]
                raise ValueError("热门推荐暂不可用，请稍后重试")
            try:
                groups = await asyncio.gather(
                    *(self.lx.hot(source, self.config.hot_board)
                      for source in self.config.hot_sources),
                    return_exceptions=True,
                )
                available = []
                for source, group in zip(self.config.hot_sources, groups):
                    if isinstance(group, Exception):
                        event(
                            self.config.data_dir, "proxy", "recommendation.source_failed",
                            level="WARNING", source=source, error=group,
                        )
                    elif isinstance(group, list):
                        available.append(group)
                raws = (raw for row in zip_longest(*available) for raw in row)
                tracks, seen, songs = [], set(), set()
                for raw in raws:
                    if not isinstance(raw, dict):
                        continue
                    try:
                        track = self.store.save_track(raw)
                    except (ValueError, TypeError, OverflowError):
                        continue
                    # Ignore album differences between platforms, but keep version labels.
                    song = (normalize(track["title"]), normalize(track["artist"]))
                    known = song[0] != normalize("未知歌曲") and song[1] != normalize("未知歌手")
                    if track["guid"] not in seen and (not known or song not in songs):
                        seen.add(track["guid"])
                        if known:
                            songs.add(song)
                        tracks.append(track)
                    if len(tracks) >= 30:
                        break
                if not tracks:
                    raise ValueError("热门推荐没有有效歌曲")
                self.store.save_playlist_snapshot(self.hot_key, day, now, tracks)
                self.hot_retry_at = 0
                event(
                    self.config.data_dir, "proxy", "recommendation.updated",
                    total=len(tracks), source=self.config.hot_source,
                )
                return tracks
            except Exception as exc:
                self.hot_retry_at = time.monotonic() + 300
                event(
                    self.config.data_dir, "proxy", "recommendation.failed",
                    level="WARNING", error=exc,
                )
                if snapshot and not force:
                    return snapshot["tracks"]
                raise ValueError("热门推荐暂不可用，请稍后重试") from exc

    @staticmethod
    def guid(user, kind):
        return hashlib.md5(
            f"fnmusic-playlist:{user}:{kind}".encode(), usedforsecurity=False
        ).hexdigest()

    def definitions(self):
        return [("hot", "热门推荐"),
                ("explore", "音乐探险"), ("archaeology", "曲库考古")] + [
            (f"public:{i}", row.get("name") or "在线歌单")
            for i, row in enumerate(self.config.public_playlists)
        ]

    def cards(self, user):
        hot = self.store.playlist_snapshot(self.hot_key)
        return [
            {
                "guid": self.guid(user, key),
                "name": name,
                "coverId": playlist_cover_id(key),
                "trackCount": (
                    len(hot["tracks"]) if key == "hot" and hot
                    else len(self.cache.get((user, key), (0, []))[1])
                ),
                "createdAt": 0,
                "updatedAt": hot["updated_at"] if key == "hot" and hot else 0,
                "description": (
                    "每日更新热门推荐，仅刷新歌曲信息，不自动下载" if key == "hot"
                    else "熟悉风格与新风格混合，探险程度可在扩展管理页设置" if key == "explore"
                    else "每天重访本地收藏；无记录仅指扩展保留的播放活动" if key == "archaeology"
                    else "在线歌单，点击喜欢才下载"
                ),
            }
            for key, name in self.definitions()
        ]

    def key(self, user, guid):
        for key, _ in self.definitions():
            if self.guid(user, key) == guid:
                return key
        raise KeyError("Unknown playlist for this user")

    def official_covers(self, data):
        """Decorate the user's real default playlist, preserving official metadata."""
        if isinstance(data, list):
            return [self.official_covers(row) for row in data]
        if not isinstance(data, dict):
            return data
        if isinstance(data.get("list"), list):
            return dict(data, list=self.official_covers(data["list"]))
        if isinstance(data.get("playlist"), dict):
            return dict(data, playlist=self.official_covers(data["playlist"]))
        if not data.get("guid"):
            return data
        matches = (
            data["guid"] == self.config.default_playlist_guid
            if self.config.default_playlist_guid
            else data.get("name") == self.config.playlist_name
        )
        return dict(data, coverId=playlist_cover_id("default")) if matches else data

    async def tracks(self, user, guid, headers=None, official=None):
        key = self.key(user, guid)
        if key == "hot":
            return await self.refresh_hot()
        if key == "archaeology":
            if headers is None or official is None:
                raise ValueError("曲库考古需要当前用户的飞牛音乐会话")
            library = Library(self.config)
            revision = repr((library.read(), library.report().get("finished_at")))
            cache_key = (user, session_key(headers), day_now(), revision)
            cached = self.native_cache.get(cache_key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            rows = await self.discovery.archaeology(user, headers, official)
            if len(self.native_cache) >= 100:
                self.native_cache.pop(next(iter(self.native_cache)))
            self.native_cache[cache_key] = (time.monotonic() + 300, rows)
            return rows
        if key == "explore":
            mode = self.discovery.settings()["exploration_mode"]
            if self.explore_modes.get(user) != mode:
                self.cache.pop((user, key), None)
            if user not in self.explore_modes and len(self.explore_modes) >= 500:
                self.explore_modes.pop(next(iter(self.explore_modes)))
            self.explore_modes[user] = mode
        cached = self.cache.get((user, key))
        if cached and cached[0] > time.monotonic():
            return cached[1]
        if key == "explore":
            tracks = await self.discovery.explore(user, self.refresh_hot)
            self.cache_tracks(user, key, tracks)
            return tracks
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
            try:
                track = self.store.save_track(raw)
            except (ValueError, TypeError, OverflowError):
                continue
            if track["guid"] in seen:
                continue
            tracks.append(track)
            seen.add(track["guid"])
        self.cache_tracks(user, key, tracks)
        return tracks

    def cache_tracks(self, user, key, tracks):
        if len(self.cache) > 500:
            self.cache = {
                k: v for k, v in self.cache.items() if v[0] > time.monotonic()
            }
            if len(self.cache) > 500:
                self.cache.pop(next(iter(self.cache)))
        self.cache[(user, key)] = (time.monotonic() + 900, tracks)

    def invalidate(self, user):
        self.cache.pop((user, "explore"), None)
        self.native_cache = {k: v for k, v in self.native_cache.items() if k[0] != user}
