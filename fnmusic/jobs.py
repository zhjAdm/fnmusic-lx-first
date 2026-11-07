import asyncio
import time
from collections import defaultdict
from pathlib import Path

from fastapi import HTTPException

from .official import session_headers
from .tools import MissingTool
from .logs import event
from .models import recording_key
from .library import Library, IndexNotReady


class Jobs:
    def __init__(self, store, lx, audio, official, config):
        self.store, self.lx, self.audio, self.official, self.config = (
            store,
            lx,
            audio,
            official,
            config,
        )
        self.library = Library(config)
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
                    await self.process(user, guid, headers, job=job)
                except asyncio.CancelledError:
                    raise
                except (MissingTool, IndexNotReady) as exc:
                    if self.store.is_liked(user, guid):
                        if job["state"] not in ("waiting_tools", "waiting_index"):
                            event(
                                self.config.data_dir,
                                "jobs",
                                "index.waiting"
                                if isinstance(exc, IndexNotReady)
                                else "tools.waiting",
                                level="WARNING",
                                task=guid,
                            )
                        self.store.update_job(
                            user,
                            guid,
                            state="waiting_index"
                            if isinstance(exc, IndexNotReady)
                            else "waiting_tools",
                            error="请先完成本地曲库扫描"
                            if isinstance(exc, IndexNotReady)
                            else "缺少 FFmpeg 音频组件，请在扩展管理页面下载",
                            next_at=time.time() + 30,
                        )
                except HTTPException as exc:
                    event(
                        self.config.data_dir,
                        "jobs",
                        "official.failed",
                        level="WARNING",
                        task=guid,
                        error=exc,
                        status=exc.status_code,
                    )
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
                except Exception as exc:
                    if self.store.is_liked(user, guid):
                        event(
                            self.config.data_dir,
                            "jobs",
                            "processing.failed",
                            level="ERROR",
                            task=guid,
                            error=exc,
                        )
                        self.retry(user, job)

    def retry(self, user, job):
        attempts = job["attempts"] + 1
        event(
            self.config.data_dir,
            "jobs",
            "retry.scheduled" if attempts < 8 else "retry.exhausted",
            level="WARNING" if attempts < 8 else "ERROR",
            task=job["guid"],
            attempts=attempts,
        )
        self.store.update_job(
            user,
            job["guid"],
            state="failed" if attempts >= 8 else "retry",
            attempts=attempts,
            next_at=time.time() + min(3600, 15 * 2**attempts),
            error="处理失败；请检查音源、曲库扫描和歌单配置后重试",
        )

    async def process(self, user, guid, headers, *, job):
        waiting = job["state"] == "waiting_scan"
        indexed = False

        def same_waiting_file(path):
            # Indexed paths stay private: an empty persisted path identifies
            # an indexed-file wait, without exposing library directories in jobs.
            return waiting and (
                job["path"] == str(path) or (indexed and not job["path"])
            )

        def wait_for_organization(path):
            message = "等待整理至正式曲库；请确认 MoviePilot 正在运行且曲库自动索引已开启"
            self.store.update_job(
                user, guid, state="waiting_scan", path=str(path),
                error=message, next_at=time.time() + 30,
            )
            if job["error"] != message:
                event(
                    self.config.data_dir, "jobs", "playlist.waiting",
                    task=guid, state="waiting_organization",
                )

        if not self.store.is_liked(user, guid):
            return
        track = self.store.get_track(guid)
        local = None
        async with self.file_locks[recording_key(track)]:
            if not self.store.is_liked(user, guid):
                return
            await self.library.ensure_scanned()
            local = await asyncio.to_thread(self.library.find_duplicate, track)
            if not self.store.is_liked(user, guid):
                return
            roots = [Path(root).resolve() for root in self.library.read()["directories"]]
            organized_only = bool(roots) and not any(
                self.config.download_dir.resolve().is_relative_to(root) for root in roots
            )
            existing = self.store.file(guid) or self.store.recording_file(track)
            if local:
                indexed = True
                path = local["path"]
                if not same_waiting_file(path):
                    organized = organized_only and bool(job["path"]) and job["path"] != str(path)
                    event(
                        self.config.data_dir,
                        "jobs",
                        "playlist.organized" if organized else "download.skipped",
                        task=guid,
                        state="organized_file" if organized else "local_index",
                    )
            elif organized_only and (
                existing or waiting or job["path"]
                or self.store.recording_file(track, require_exists=False)
            ):
                # A missing source can have been moved by an external organizer.
                # Keep the durable download record; never redownload while waiting.
                wait_for_organization(existing["path"] if existing else job["path"])
                return
            elif existing:
                path = existing["path"]
                self.store.save_file(guid, path, existing["quality"])
                if not same_waiting_file(path):
                    event(
                        self.config.data_dir,
                        "jobs",
                        "download.skipped",
                        task=guid,
                        state="existing_extension_file",
                    )
            else:
                if waiting and job["path"]:
                    previous = Path(job["path"])
                    try:
                        reason = (
                            "cache_miss" if previous.is_file() and previous.stat().st_size
                            else "missing_or_empty"
                        )
                    except OSError:
                        reason = "unreadable"
                    event(
                        self.config.data_dir, "jobs", "download.file_unavailable",
                        level="WARNING", task=guid, state=reason,
                    )
                self.store.update_job(user, guid, state="downloading")
                supported = await self.lx.qualities(track["source"])
                for quality in supported:
                    if not self.store.is_liked(user, guid):
                        return
                    try:
                        path = await self.audio.download(track, quality)
                        self.store.save_file(guid, path, quality)
                        self.store.update_job(user, guid, scan_at=0)
                        event(
                            self.config.data_dir,
                            "jobs",
                            "audio.downloaded",
                            task=guid,
                            quality=quality,
                        )
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
        if organized_only and not indexed:
            wait_for_organization(path)
            return
        self.store.update_job(
            user,
            guid,
            state="waiting_scan",
            path="" if indexed else str(path),
            next_at=time.time() + 15,
            error="",
        )
        # Local indexing never calls the official scan API. Official lookup is
        # only needed to associate this exact file with an App playlist GUID.
        local = await self.official.find_local(track, headers, path=path)
        if not local:
            if not same_waiting_file(path):
                event(
                    self.config.data_dir, "jobs", "playlist.waiting",
                    task=guid, state="waiting_scan",
                )
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
        event(self.config.data_dir, "jobs", "playlist.completed", task=guid)
