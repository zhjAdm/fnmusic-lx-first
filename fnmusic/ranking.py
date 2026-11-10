"""Rank real platform metrics within one platform; missing metrics stay unknown."""

import math
import re


def metric(value):
    if isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(万|亿|[kKmM])?", text)
    if not match:
        return None
    number = float(match[1]) * {None: 1, "万": 1e4, "亿": 1e8,
                              "k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6}[match[2]]
    return number if math.isfinite(number) else None


def quality_rank(row):
    def first(keys):
        return next((n for key in keys if (n := metric(row.get(key))) is not None), None)

    rating = first(("rating", "score", "grade"))
    likes = first(("likes", "like_count", "praise_count"))
    popularity = first(("play_count", "playCount", "listen_count"))
    return (rating is not None, rating or 0, likes is not None, likes or 0,
            popularity is not None, popularity or 0)
