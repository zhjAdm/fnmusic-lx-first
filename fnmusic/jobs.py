import asyncio
import time
from collections import defaultdict

from fastapi import HTTPException

from .official import session_headers


class Jobs:
    def __init__(self, store, lx, audio, official, config):
        self.store, self.lx, self.audio, self.official, self.config = (
            store,
            lx,
            audio,
            official,
            config,
        )
        self.sessions = {}
        self.user_locks = defaultdict(asyncio.Lock)
        self.file_locks = defaultdict(asyncio.Lock)
        self.task = None

    def touch(self, user, headers):
        now = time.monotonic()
        self.sessions = {k: v for k, v in self.sessions.items() if v[0] > now}
        if len(self.sessions) >= 100 and user not in self.sessions:
            self.sessions.pop(next(iter(self.sessions)))
        self.sessions[user] = (now + self.config.session_ttl, session_headers(headers))

    def start(self):
        self.task = asyncio.create_task(self.loop())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.sessions.clear()

    async def loop(self):
        while True:
            for user, (expiry, headers) in list(self.sessions.items()):
                if expiry <= time.monotonic():
                    self.sessions.pop(user, None)
                    continue
                await self.run_once(user, headers)
            await asyncio.sleep(3)

    async def run_once(self, user, headers):
        async with self.user_locks[user]:
            for job in self.store.jobs(user):
                if (
                    job["state"] in {"done", "cancelled", "failed"}
                    or job["next_at"] > time.time()
                ):
                    continue
                guid = job["guid"]
                try:
                    if await self.official.user(headers) != user:
                        raise HTTPException(401, "User changed")
                    await self.process(user, guid, headers)
                except asyncio.CancelledError:
                    raise
                except HTTPException as exc:
                    if not self.store.is_liked(user, guid):
                        continue
                    if exc.status_code == 401:
                        self.sessions.pop(user, None)
                        self.store.update_job(
                            user,
                            guid,
                            state="waiting_auth",
                            error="登录已失效",
                            next_at=0,
                        )
                        break
                    self.retry(user, job)
                except Exception:
                    if self.store.is_liked(user, guid):
                        self.retry(user, job)

    def retry(self, user, job):
        attempts = job["attempts"] + 1
        self.store.update_job(
            user,
            job["guid"],
            state="failed" if attempts >= 8 else "retry",
            attempts=attempts,
            next_at=time.time() + min(3600, 15 * 2**attempts),
            error="处理失败；请检查音源、曲库扫描和歌单配置后重试",
        )

    async def process(self, user, guid, headers):
        if not self.store.is_liked(user, guid):
            return
        track = self.store.get_track(guid)
        previous = next(j for j in self.store.jobs(user) if j["guid"] == guid)
        async with self.file_locks[guid]:
            existing = self.store.file(guid)
            if existing:
                path = existing["path"]
            else:
                self.store.update_job(user, guid, state="downloading")
                supported = await self.lx.qualities(track["source"])
                for quality in supported:
                    try:
                        path = await self.audio.download(track, quality)
                        self.store.save_file(guid, path, quality)
                        break
                    except (ValueError, OSError, TimeoutError):
                        continue
                    except Exception as exc:
                        import httpx

                        if not isinstance(exc, httpx.HTTPError):
                            raise
                else:
                    raise ValueError("All qualities unavailable")
        if not self.store.is_liked(user, guid):
            return
        self.store.update_job(
            user, guid, state="waiting_scan", path=str(path), next_at=time.time() + 15
        )
        if not previous["scan_at"] or time.time() - previous["scan_at"] >= 300:
            await self.official.scan(headers)
            self.store.update_job(user, guid, scan_at=time.time())
        local = await self.official.find_local(track, headers, path=path)
        if not local:
            return
        if not self.store.is_liked(user, guid):
            return
        # Serialize writes per user; the current implementation has one worker process.
        playlist = await self.official.default_playlist(headers)
        if not self.store.is_liked(user, guid):
            return
        self.store.update_job(user, guid, state="binding", official_guid=local["guid"])
        await self.official.api(
            "POST",
            "/music/api/v1/favorite-track/create",
            headers,
            json={"trackGUID": local["guid"]},
        )
        if not self.store.is_liked(user, guid):
            await self.official.api(
                "POST",
                "/music/api/v1/favorite-track/delete",
                headers,
                json={"trackGUID": local["guid"]},
            )
            return
        await self.official.api(
            "POST",
            "/music/api/v1/playlist/add-track",
            headers,
            json={"guid": playlist, "trackGUIDs": [local["guid"]]},
        )
        if not self.store.is_liked(user, guid):
            await self.official.api(
                "POST",
                "/music/api/v1/favorite-track/delete",
                headers,
                json={"trackGUID": local["guid"]},
            )
            return
        self.store.update_job(user, guid, state="done", next_at=0, error="")
