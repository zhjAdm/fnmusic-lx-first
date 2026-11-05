import wave
from fnmusic.audio import tag_audio


def song(folder, name="song.wav", title="歌曲", artist="歌手", album="专辑"):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"\0\0" * 8000)
    tag_audio(path, {"title": title, "artist": artist, "album": album})
    return path
