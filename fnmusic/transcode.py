"""AAC-in-ADTS HLS delivery using pipes only, with bounded concurrency."""

import asyncio
import anyio
import math

from fastapi import HTTPException
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse
from .streams import ClosingStream
from .tools import MediaTools, MissingTool
from .config import Config
from .logs import event


class Transcode:
    def __init__(self, media, config=None):
        self.media = media
        self.config = config or Config()
        self.tools = MediaTools(self.config)
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

    async def audio(self, track, user, headers, *, request_id=""):
        try:
            executable = await self.tools.require("ffmpeg")
        except MissingTool as exc:
            raise HTTPException(503, str(exc)) from None
        try:
            await asyncio.wait_for(self.slots.acquire(), 1)
        except TimeoutError:
            raise HTTPException(503, "转码繁忙，请稍后重试") from None
        process = None
        feed = None
        diagnostics = None
        source = None
        cleanup_task = None

        async def finalize():
            try:
                if feed:
                    feed.cancel()
                    await asyncio.gather(feed, return_exceptions=True)
                try:
                    if process:
                        forced = process.returncode is None
                        if forced:
                            try:
                                process.kill()
                            except ProcessLookupError:
                                forced = False
                        exit_code = await process.wait()
                        if diagnostics:
                            reason = await diagnostics
                            if not forced and exit_code:
                                event(
                                    self.config.data_dir, "proxy", "play.ffmpeg_failed",
                                    level="ERROR", mode="hls", task=track["guid"],
                                    exit_code=exit_code, code=reason, request_id=request_id,
                                )
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
                executable,
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
                stderr=asyncio.subprocess.PIPE,
            )

            async def read_diagnostics():
                tail = b""
                while block := await process.stderr.read(4096):
                    tail = (tail + block)[-8192:]
                # Classify known messages without retaining FFmpeg's raw text.
                tail = tail.lower()
                for marker, code in (
                    (b"invalid data", "invalid_input"),
                    (b"error while decoding", "decode_failed"),
                    (b"error opening input", "input_open_failed"),
                    (b"error while opening encoder", "encoder_failed"),
                    (b"broken pipe", "broken_pipe"),
                ):
                    if marker in tail:
                        return code
                return "other" if tail else "no_stderr"

            async def pump():
                try:
                    async for chunk in source.body_iterator:
                        process.stdin.write(chunk)
                        await process.stdin.drain()
                finally:
                    process.stdin.close()

            diagnostics = asyncio.create_task(read_diagnostics())
            feed = asyncio.create_task(pump())
            first = await asyncio.wait_for(process.stdout.read(16384), 30)
            if not first:
                await asyncio.wait_for(process.wait(), 5)
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
