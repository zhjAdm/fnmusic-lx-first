"""The only audio writer. Called exclusively by the favorite job worker."""

import asyncio
import json
import os
import uuid

import mutagen

from .media import open_media

LOSSLESS = {"flac", "alac", "pcm_s16le", "pcm_s24le", "pcm_s32le", "wavpack", "ape"}
EXTENSIONS = {
    "flac": ".flac",
    "alac": ".m4a",
    "aac": ".m4a",
    "mp3": ".mp3",
    "opus": ".ogg",
    "vorbis": ".ogg",
    "pcm_s16le": ".wav",
    "pcm_s24le": ".wav",
    "pcm_s32le": ".wav",
    "wavpack": ".wv",
    "ape": ".ape",
}


async def probe_audio(path, expected_ms=0, quality=""):
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 30)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode or len(stdout) > 1024 * 1024:
        raise ValueError("Invalid audio file")
    result = json.loads(stdout)
    tracks = [s for s in result.get("streams", []) if s.get("codec_type") == "audio"]
    if not tracks:
        raise ValueError("No audio stream")
    stream = tracks[0]
    codec = stream.get("codec_name")
    if codec not in EXTENSIONS:
        raise ValueError("Unsupported audio container")
    if quality in {"flac", "flac24bit", "hires"} and codec not in LOSSLESS:
        raise ValueError("Lossless request returned lossy audio")
    duration = (
        float(
            (result.get("format") or {}).get("duration") or stream.get("duration") or 0
        )
        * 1000
    )
    if duration <= 0:
        raise ValueError("Unknown audio duration")
    if expected_ms and abs(duration - expected_ms) > max(5000, expected_ms * 0.03):
        raise ValueError("Audio duration mismatch or preview")
    return EXTENSIONS[codec]


def tag_audio(path, track):
    audio = mutagen.File(path, easy=True)
    if audio is None:
        raise ValueError("Cannot tag audio")
    if audio.tags is None:
        audio.add_tags()
    if audio.__class__.__name__ == "WAVE":
        from mutagen.id3 import TALB, TIT2, TPE1

        audio.tags.add(TIT2(encoding=3, text=track["title"]))
        audio.tags.add(TPE1(encoding=3, text=track["artist"]))
        audio.tags.add(TALB(encoding=3, text=track["album"]))
    else:
        audio["title"] = [track["title"]]
        audio["artist"] = [track["artist"]]
        audio["album"] = [track["album"]]
    audio.save()


class Audio:
    def __init__(self, client, lx, config):
        self.client, self.lx, self.config = client, lx, config

    async def download(self, track, quality):
        target = await self.lx.resolve(track["raw"], quality)
        missing = []
        directory = self.config.download_dir
        while not directory.exists():
            missing.append(directory)
            directory = directory.parent
        for directory in reversed(missing):
            directory.mkdir(exist_ok=True)
            directory.chmod(0o755)
        temporary = self.config.download_dir / (
            "." + track["guid"] + "-" + uuid.uuid4().hex + ".part"
        )
        response = await open_media(
            self.client, "GET", target["url"], target.get("headers")
        )
        try:
            if response.status_code != 200:
                raise ValueError("Incomplete download response")
            expected = int(response.headers.get("content-length") or 0)
            if expected > self.config.max_download_bytes:
                raise ValueError("Audio exceeds download size limit")
            count = 0
            with temporary.open("xb") as output:
                os.chmod(temporary, 0o600)
                async for block in response.aiter_bytes(65536):
                    count += len(block)
                    if count > self.config.max_download_bytes:
                        raise ValueError("Audio exceeds download size limit")
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if not count or (expected and expected != count):
                raise ValueError("Incomplete audio body")
            extension = await probe_audio(temporary, track.get("duration", 0), quality)
            await asyncio.to_thread(tag_audio, temporary, track)
            os.chmod(
                temporary, 0o644
            )  # Official scanner must be able to read the final audio.
            final = self.config.download_dir / (track["guid"] + extension)
            temporary.replace(final)
            return final.resolve()
        finally:
            await response.aclose()
            temporary.unlink(missing_ok=True)
