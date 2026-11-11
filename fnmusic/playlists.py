import asyncio
import hashlib
import time
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from weakref import WeakValueDictionary

from .logs import event
from .models import normalize
from .discovery import Discovery, day_now
from .images import playlist_cover_id

BEIJING = timezone(timedelta(hours=8))
DISCOVERY_KINDS = ("explore", "archaeology")


class Playlists:
    def __init__(self, store, lx, config):
        self.store, self.lx, self.config = store, lx, config
        self.cache = {}
        self.discovery = Discovery(config, store, lx)
        self.discovery_locks = WeakValueDictionary()
        self.discovery_retry = {}
        self.hot_lock = asyncio.Lock()
        self.hot_retry_at = 0
        self.task = None

    @property
    def hot_key(self):
        return "hot:" + hashlib.sha256(
            repr((self.config.hot_source, self.config.hot_board)).encode()
        ).hexdigest()

    def start(self, jobs=None, official=None):
        self.task = asyncio.create_task(self.loop(jobs, official))

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def loop(self, jobs=None, official=None):
        while True:
            try:
                await self.refresh_hot()
            except Exception:
                # refresh_hot records the failure and applies a retry cooldown.
                pass
            if jobs is not None and official is not None:
                try:
                    await self.refresh_active_discovery(jobs.sessions, official)
                except Exception as exc:
                    event(self.config.data_dir, "proxy", "playlist.refresh_failed",
                          level="WARNING", error=exc)
            await asyncio.sleep(60)

    def discovery_key(self, user, kind):
        return "discovery:" + self.guid(user, kind)

    def discovery_due(self, key, day, snapshot):
        return (snapshot is None or snapshot["day"] != day) and (
            self.discovery_retry.get((key, day), 0) <= time.monotonic()
        )

    async def refresh_active_discovery(self, sessions, official):
        for user, (expiry, headers) in list(sessions.items()):
            if expiry <= time.monotonic():
                continue
            day = day_now()
            due = []
            for kind in DISCOVERY_KINDS:
                key = self.discovery_key(user, kind)
                if self.discovery_due(key, day, self.store.playlist_snapshot(key)):
                    due.append(kind)
            if not due:
                continue
            try:
                if await official.user(headers) != user:
                    continue
            except Exception:
                # Official.user handles identity failures. Wait for a valid session.
                continue
            await asyncio.gather(
                *(self.refresh_discovery(user, kind, headers, official) for kind in due),
                return_exceptions=True,
            )

    async def refresh_discovery(self, user, kind, headers, official):
        key = self.discovery_key(user, kind)
        async with self.discovery_locks.setdefault(key, asyncio.Lock()):
            day = day_now()
            snapshot = self.store.playlist_snapshot(key)
            if not self.discovery_due(key, day, snapshot):
                if snapshot is not None:
                    return snapshot["tracks"]
                raise ValueError("发现歌单暂不可用，请稍后重试")
            try:
                if kind == "explore":
                    generate = self.discovery.explore(user, self.refresh_hot)
                else:
                    generate = self.discovery.archaeology(user, headers, official)
                rows = await asyncio.wait_for(generate, timeout=20)
                self.store.save_playlist_snapshot(key, day, time.time(), rows)
                return rows
            except Exception as exc:
                now = time.monotonic()
                self.discovery_retry = {k: until for k, until in self.discovery_retry.items()
                                        if until > now and k[1] == day}
                self.discovery_retry[key, day] = now + 300
                event(self.config.data_dir, "proxy", "playlist.refresh_failed",
                      level="WARNING", task=key, error=exc)
                if snapshot is not None:
                    return snapshot["tracks"]
                raise ValueError("发现歌单暂不可用，请稍后重试") from exc

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
        snapshots = {kind: self.store.playlist_snapshot(self.discovery_key(user, kind))
                     for kind in DISCOVERY_KINDS}
        snapshots["hot"] = self.store.playlist_snapshot(self.hot_key)
        return [
            {
                "guid": self.guid(user, key),
                "name": name,
                "coverId": playlist_cover_id(key),
                "trackCount": (
                    len(snapshots[key]["tracks"]) if snapshots.get(key) is not None
                    else len(self.cache.get((user, key), (0, []))[1])
                ),
                "createdAt": 0,
                "updatedAt": snapshots[key]["updated_at"] if snapshots.get(key) is not None else 0,
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
        if key in DISCOVERY_KINDS:
            if key == "archaeology" and (headers is None or official is None):
                raise ValueError("曲库考古需要当前用户的飞牛音乐会话")
            return await self.refresh_discovery(user, key, headers, official)
        cached = self.cache.get((user, key))
        if cached and cached[0] > time.monotonic():
            return cached[1]
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
