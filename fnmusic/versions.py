"""Conservative, read-only grouping of explicitly labelled local versions."""

from pathlib import Path
import re
import unicodedata

from .discovery import library_rows
from .models import normalize

LABELS = [
    ("Live", r"live|现场(?:版)?|演唱会(?:版)?"),
    ("伴奏", r"伴奏(?:版)?|instrumental|karaoke|off[ -]?vocal"),
    ("重制", r"(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?|(?:\d{4}年?)?重制(?:版)?"),
    ("Remix", r"remix|混音(?:版)?"),
    ("Acoustic", r"acoustic(?:\s+version)?|不插电(?:版)?"),
    ("Demo", r"demo|小样"),
    ("录音室", r"studio(?:\s+version)?|录音室(?:版)?"),
]
SUFFIX = re.compile(r"\s*(?:[（(\[【](?P<bracket>[^()（）\[\]【】]+)[）)\]】]|\s[-–—]\s(?P<dash>.+))\s*$")


def version_title(title):
    base, labels = " ".join(unicodedata.normalize("NFKC", str(title or "")).split()), []
    while match := SUFFIX.search(base):
        content = match.group("bracket") or match.group("dash")
        label = next((name for name, pattern in LABELS if re.fullmatch(pattern, content, re.I)), None)
        if not label:
            break
        labels.insert(0, label)
        base = base[:match.start()].strip()
    return base, " / ".join(labels) or "未标注"


def radar(library, q="", page=1, size=25):
    if (not isinstance(q, str) or len(q) > 200 or type(page) is not int
            or not 1 <= page <= 1000000 or type(size) is not int or not 1 <= size <= 100):
        raise ValueError("版本雷达查询参数无效")
    groups = {}
    for row in library_rows(library.config):
        title, label = version_title(row["title"])
        artist = normalize(row["artist"])
        if not title or not artist or artist in {"未知歌手", "unknown"}:
            continue
        groups.setdefault((normalize(title), artist), []).append(
            dict(row, version=label, format=Path(row["path"]).suffix[1:].upper()))
    items = []
    for (title, artist), versions in sorted(groups.items()):
        if len(versions) < 2:
            continue
        if q and not any(normalize(q) in normalize(" ".join(str(v) for v in r.values())) for r in versions):
            continue
        items.append({"title": version_title(versions[0]["title"])[0], "artist": versions[0]["artist"],
                      "status": "疑似关联", "versions": versions})
    return {"items": items[(page-1)*size:page*size], "total": len(items), "page": page, "size": size}
