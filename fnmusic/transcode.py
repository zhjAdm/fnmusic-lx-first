"""AAC-in-ADTS HLS delivery using pipes only, with bounded concurrency."""

import asyncio
import anyio
import math

from fastapi import HTTPException
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse
from .streams import ClosingStream


class Transcode:
    def __init__(self, media):
        self.media = media
        self.slots = asyncio.Semaphore(2)
        self.cleanups = set()

    @staticmethod
    def playlist(track):
        duration = max(1, track["duration"] / 1000)
        return (
            "#EXTM3U\n#EXT-X-VERSION:3\n"
            + f"#EXT-X-TARGETDURATION:{math.ceil(duration)}\n"
            "#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-MEDIA-SEQUENCE:0\n"
            + f"#EXTINF:{duration:.3f},\naudio.aac\n#EXT-X-ENDLIST\n"
        )

    async def stop(self):
        for cleanup in list(self.cleanups):
            await cleanup()

    async def audio(self, track, user, headers):
        try:
            await asyncio.wait_for(self.slots.acquire(), 1)
        except TimeoutError:
            raise HTTPException(503, "转码繁忙，请稍后重试") from None
        process = None
        feed = None
        source = None
        cleanup_task = None

        async def finalize():
            try:
                if feed:
                    feed.cancel()
                    await asyncio.gather(feed, return_exceptions=True)
                try:
                    if process:
                        if process.returncode is None:
                            try:
                                process.kill()
                            except ProcessLookupError:
                                pass
                        await process.wait()
                finally:
                    if source:
                        if hasattr(source, "aclose"):
                            await source.aclose()
                        elif hasattr(source.body_iterator, "aclose"):
                            await source.body_iterator.aclose()
            finally:
                self.slots.release()
                self.cleanups.discard(cleanup)

        async def cleanup():
            nonlocal cleanup_task
            if cleanup_task is None:
                cleanup_task = asyncio.create_task(finalize())
            with anyio.CancelScope(shield=True):
                await asyncio.shield(cleanup_task)

        self.cleanups.add(cleanup)
        try:
            source = await self.media.play(
                track, user, headers, None, False, force_proxy=True
            )
            if not isinstance(source, StreamingResponse) or source.status_code not in (
                200,
                206,
            ):
                raise ValueError("No audio input for transcoding")
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-protocol_whitelist",
                "pipe",
                "-i",
                "pipe:0",
                "-vn",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-f",
                "adts",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )

            async def pump():
                try:
                    async for chunk in source.body_iterator:
                        process.stdin.write(chunk)
                        await process.stdin.drain()
                finally:
                    process.stdin.close()

            feed = asyncio.create_task(pump())
            first = await asyncio.wait_for(process.stdout.read(16384), 30)
            if not first:
                raise ValueError("AAC conversion failed")

            async def body():
                try:
                    yield first
                    while block := await asyncio.wait_for(
                        process.stdout.read(65536), 45
                    ):
                        yield block
                    await feed
                    if await process.wait():
                        raise ValueError("AAC conversion interrupted")
                finally:
                    await cleanup()

            return ClosingStream(
                body(),
                media_type="audio/aac",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
                background=BackgroundTask(cleanup),
                close=cleanup,
            )
        except BaseException:
            await cleanup()
            raise
