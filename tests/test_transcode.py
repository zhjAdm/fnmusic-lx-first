import wave
import asyncio
import anyio

import pytest
from starlette.responses import StreamingResponse

from fnmusic.transcode import Transcode
from fnmusic.streams import ClosingStream


@pytest.mark.asyncio
async def test_aac_stream_uses_memory_pipes_not_files(tmp_path):
    sample = tmp_path / "sample.wav"
    with wave.open(str(sample), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(8000)
        f.writeframes(b"\0\0" * 8000)
    data = sample.read_bytes()

    class Media:
        async def play(self, *args, **kwargs):
            assert kwargs["force_proxy"] is True

            async def body():
                yield data

            return StreamingResponse(body())

    transcode = Transcode(Media())
    response = await transcode.audio({"guid": "a" * 32}, "user", {})
    result = b"".join([x async for x in response.body_iterator])
    assert result[0] == 0xFF and (result[1] & 0xF0) == 0xF0
    assert list(tmp_path.iterdir()) == [sample]
    playlist = transcode.playlist({"guid": "a" * 32, "duration": 1000})
    assert "audio.aac" in playlist and "#EXT-X-ENDLIST" in playlist


@pytest.mark.asyncio
async def test_cancelled_close_still_releases_process_and_slot(monkeypatch):
    class Process:
        returncode = None
        killed = False

        class Input:
            def write(self, chunk):
                pass

            async def drain(self):
                await asyncio.Future()

            def close(self):
                pass

        class Output:
            async def read(self, size):
                return b"aac-frame"

        stdin = Input()
        stdout = Output()

        def kill(self):
            self.killed = True
            self.returncode = 0

        async def wait(self):
            return 0

    process = Process()

    async def spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    closed = []

    async def source_close():
        closed.append(True)

    class Media:
        async def play(self, *args, **kwargs):
            async def body():
                yield b"input"

            return ClosingStream(body(), close=source_close)

    transcode = Transcode(Media())

    # This test replaces subprocesses to exercise cancellation. Tool discovery
    # is independently covered with real audio smoke checks and candidate tests.
    async def executable(name):
        return name

    monkeypatch.setattr(transcode.tools, "require", executable)
    response = await transcode.audio({"guid": "a" * 32}, "user", {})
    await asyncio.sleep(0)
    with anyio.CancelScope() as scope:
        scope.cancel()
        await response.aclose()
    await transcode.stop()
    assert process.killed and closed
    assert transcode.slots._value == 2
    assert not transcode.cleanups
