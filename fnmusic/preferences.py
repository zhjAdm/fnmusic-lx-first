"""Shared music style aliases and normalized song identities."""

import re

from .models import normalize

ALIASES = {
    "pop": "流行", "rock": "摇滚", "jazz": "爵士", "folk": "民谣",
    "classical": "古典", "electronic": "电子", "dance": "舞曲",
    "hip hop": "说唱", "hip-hop": "说唱", "hiphop": "说唱", "rap": "说唱",
    "r&b": "R&B", "rhythm and blues": "R&B", "blues": "蓝调",
    "country": "乡村", "metal": "金属", "punk": "朋克", "民歌": "民族",
    "轻音乐": "轻音乐", "soundtrack": "影视", "ost": "影视",
}


def style_names(value):
    if not isinstance(value, str):
        return []
    values = (normalize(v) for v in re.split(r"[,;；、|/]+", value))
    return list(dict.fromkeys(ALIASES.get(v, v) for v in values
                              if v and v not in {"unknown", "未知", "other", "其他"}))


def song_key(track):
    return normalize(track.get("title")), normalize(track.get("artist"))
