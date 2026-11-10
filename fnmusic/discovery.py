"""Discovery playlists and their small, shared administrator setting."""

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
from itertools import zip_longest
import json
import time

import httpx

from .control import atomic_text
from .history import History
from .library import Library
from .models import normalize
from .preferences import song_key, style_names

BEIJING = timezone(timedelta(hours=8))
MODES = {"comfort": 0.1, "balanced": 0.35, "bold": 0.65}
# Editorial rules, not inferred artist genres or audio analysis.
NEIGHBORS = {
    "流行": ["民谣", "R&B", "电子"], "摇滚": ["朋克", "金属", "蓝调"],
    "爵士": ["蓝调", "R&B", "轻音乐"], "民谣": ["乡村", "流行", "蓝调"],
    "古典": ["轻音乐", "影视", "爵士"], "电子": ["舞曲", "说唱", "流行"],
    "舞曲": ["电子", "流行"], "说唱": ["R&B", "电子", "爵士"],
    "R&B": ["爵士", "说唱", "蓝调"], "蓝调": ["爵士", "摇滚", "乡村"],
    "乡村": ["民谣", "蓝调"], "金属": ["摇滚", "朋克"], "朋克": ["摇滚", "金属"],
    "轻音乐": ["古典", "爵士", "影视"], "影视": ["古典", "轻音乐", "流行"],
}


def day_now():
    return datetime.now(BEIJING).date().isoformat()


def library_rows(config):
    library = Library(config)
    roots = library.read()["directories"]
    if not roots or not library.database.exists():
        return []
    db = library.connect()
    try:
        return [dict(row) for row in db.execute(
            "SELECT title,artist,album,duration,path,size,genre FROM audio WHERE root IN ("
            + ",".join("?" for _ in roots) + ") ORDER BY path", roots,
        )]
    finally:
        db.close()


def archaeology_candidates(library, history, user, day, now):
    last = {song_key(row): row["last_at"] for row in history}
    def rank(row):
        return hashlib.sha256(f"{user}:{day}:{row['path']}".encode()).digest()

    library = sorted(library, key=rank)
    unheard = [dict(r, reason="尚无播放记录") for r in library if song_key(r) not in last]
    forgotten = [dict(r, reason="超过 30 天未播放") for r in library
                 if song_key(r) in last and now - last[song_key(r)] >= 30 * 86400]
    random = [dict(r, reason="每日翻箱底") for r in library]
    result, seen = [], set()
    queues = [iter(unheard), iter(forgotten), iter(random)]
    while queues:
        remaining = []
        for queue in queues:
            row = next((r for r in queue if r["path"] not in seen), None)
            if row is not None:
                result.append(row)
                seen.add(row["path"])
                remaining.append(queue)
        queues = remaining
    return result


class Discovery:
    def __init__(self, config, store, lx):
        self.config, self.store, self.lx = config, store, lx
        self.history = History(config.data_dir)
        self.path = config.data_dir / "discovery.json"

    def settings(self):
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        mode = data.get("exploration_mode", "balanced")
        if mode not in MODES:
            raise ValueError("音乐探险设置无效，请重新保存")
        return {"exploration_mode": mode}

    def save_settings(self, mode):
        if not isinstance(mode, str) or mode not in MODES:
            raise ValueError("探险程度请选择舒适、均衡或大胆")
        atomic_text(self.path, json.dumps({"exploration_mode": mode}) + "\n")
        return self.settings()

    async def explore(self, user, hot):
        mode = self.settings()["exploration_mode"]
        history, library = await asyncio.gather(
            asyncio.to_thread(self.history.songs, user),
            asyncio.to_thread(library_rows, self.config),
        )
        scores, artists = Counter(), set()
        for row in library:
            scores.update(style_names(row.get("genre")))
            artists.add(normalize(row["artist"]))
        for row in history:
            scores.update({s: 3 for s in style_names(row["style"])})
            artists.add(normalize(row["artist"]))
        familiar = [s for s, _ in scores.most_common(3)]
        adjacent = list(dict.fromkeys(s for f in familiar for s in NEIGHBORS.get(f, [])
                                      if s not in familiar))[:3]
        raws = await self.lx.styles(familiar + adjacent, self.config.hot_sources) if familiar else []
        if not raws and callable(hot):
            hot = await hot()
        excluded = {song_key(r) for r in history + library}
        known, new, seen = [], [], set()
        for raw in raws or [r["raw"] for r in hot]:
            try:
                track = self.store.save_track(raw)
            except (ValueError, TypeError, OverflowError):
                continue
            key = song_key(track)
            if key in excluded or key in seen:
                continue
            seen.add(key)
            styles = style_names(track["style"])
            is_known = bool(set(styles) & set(familiar))
            track["reason"] = ("熟悉风格：" if is_known else "探索风格：") + (
                "、".join(styles) if styles else "热门发现（暂无风格信息）")
            (known if is_known else new).append(track)
        # For familiar genres, favor unfamiliar artists as exploration increases.
        if mode != "comfort":
            known.sort(key=lambda t: normalize(t["artist"]) in artists)
        target = round(30 * MODES[mode])
        selected = known[:30-target] + new[:target]
        selected += known[30-target:] + new[target:]
        # Distribute the selected new styles instead of appending them as a block.
        selected = selected[:30]
        a = [r for r in selected if r in known]
        b = [r for r in selected if r not in known]
        return [r for batch in zip_longest(a, b) for r in batch if r is not None]

    async def archaeology(self, user, headers, official):
        library, history = await asyncio.gather(
            asyncio.to_thread(library_rows, self.config),
            asyncio.to_thread(self.history.songs, user),
        )
        candidates = archaeology_candidates(library, history, user, day_now(), time.time())[:90]
        last_by_guid = {}
        for row in history:
            if row.get("guid"):
                last_by_guid[row["guid"]] = max(last_by_guid.get(row["guid"], 0), row["last_at"])
        semaphore = asyncio.Semaphore(4)
        results = {}

        async def resolve(i, row):
            async with semaphore:
                try:
                    native = await official.find_local(row, headers, path=row["path"])
                    if native and native.get("guid"):
                        reason = row["reason"]
                        # Native playback can be observed before any metadata lookup.
                        # Its GUID-only history must not be labelled as unheard.
                        last = last_by_guid.get(native["guid"])
                        if last is not None and reason != "每日翻箱底":
                            reason = ("超过 30 天未播放" if time.time() - last >= 30 * 86400
                                      else "每日翻箱底")
                        results[i] = dict(native, discoveryReason=reason)
                except (httpx.HTTPError, ValueError):
                    # One bad match must not hide otherwise accessible tracks.
                    pass

        tasks = [asyncio.create_task(resolve(i, row)) for i, row in enumerate(candidates)]
        try:
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
        except TimeoutError:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        seen, rows = set(), []
        for i in sorted(results):
            row = results[i]
            if row["guid"] not in seen:
                seen.add(row["guid"])
                rows.append(row)
        return rows[:30]
