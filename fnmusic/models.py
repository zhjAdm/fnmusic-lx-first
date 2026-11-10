import hashlib
import unicodedata

QUALITIES = (
    "master",
    "atmos_plus",
    "atmos",
    "hires",
    "flac24bit",
    "flac",
    "320k",
    "128k",
)


def quality_order(supported):
    return [q for q in QUALITIES if q in supported]


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def recording_key(track):
    return tuple(normalize(track.get(key)) for key in ("title", "artist", "album"))


def same_recording(left, right):
    key = recording_key(left)
    if (
        not all(key)
        or key[0] == normalize("未知歌曲")
        or key[1] == normalize("未知歌手")
        or key[2] == normalize("未知专辑")
        or key != recording_key(right)
    ):
        return False
    try:
        a, b = int(left.get("duration") or 0), int(right.get("duration") or 0)
        return a > 0 and b > 0 and abs(a - b) <= 3000
    except (TypeError, ValueError, OverflowError):
        return False


def track_from_raw(raw):
    source = raw.get("source")
    identifier = (
        raw.get("hash") if source == "kg" else raw.get("songmid", raw.get("id"))
    )
    if (
        source not in {"kw", "kg", "tx", "wy", "mg"}
        or identifier is None
        or identifier == ""
    ):
        raise ValueError("Missing LX track identity")
    interval = str(raw.get("interval") or "0")
    try:
        duration = sum(
            float(part) * 60**i for i, part in enumerate(reversed(interval.split(":")))
        )
    except ValueError:
        duration = 0
    guid = hashlib.md5(
        f"fnmusic-lx:{source}:{identifier}".encode(), usedforsecurity=False
    ).hexdigest()
    return {
        "guid": guid,
        "source": source,
        "identifier": str(identifier),
        "title": str(raw.get("name") or raw.get("title") or "未知歌曲"),
        "artist": str(raw.get("singer") or raw.get("artist") or "未知歌手"),
        "album": str(raw.get("albumName") or "未知专辑"),
        "duration": round(duration * 1000),
        "cover": str(raw.get("img") or raw.get("pic") or ""),
        "style": str(raw.get("recommendation_style") or raw.get("genre") or ""),
        "raw": raw,
    }


def to_official(track, liked=False):
    guid = track["guid"]
    artist_guid = hashlib.md5(
        (guid + ":artist").encode(), usedforsecurity=False
    ).hexdigest()
    album_guid = hashlib.md5(
        (guid + ":album").encode(), usedforsecurity=False
    ).hexdigest()
    return {
        "guid": guid,
        "id": guid,
        "title": track["title"],
        "name": track["title"],
        "artists": [{"guid": artist_guid, "name": track["artist"], "coverId": None}],
        "artist": track["artist"],
        "album": {"guid": album_guid, "name": track["album"], "coverId": guid},
        "duration": track["duration"],
        "coverId": guid,
        "isFavorite": liked,
        "hasLyric": True,
        "isCue": False,
        "accessStatus": 0,
        "genres": [],
        "audioSpec": {
            "path": f"online/lx/{guid}.mp3",
            "format": "mp3",
            "codec": "mp3",
            "container": "mp3",
            "duration": track["duration"],
            "size": 0,
        },
        "createdAt": 0,
        "updatedAt": 0,
        "year": None,
        "discNo": None,
        "trackNo": None,
    }
