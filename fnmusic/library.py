"""Read-only directory scans and an extension-owned audio metadata index."""

import asyncio
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

import mutagen

from .control import atomic_text
from .logs import event
from .models import same_recording

AUDIO_SUFFIXES = {
    ".mp3",
    ".flac",
    ".m4a",
    ".m4b",
    ".mp4",
    ".wav",
    ".ogg",
    ".opus",
    ".wv",
    ".ape",
    ".aif",
    ".aiff",
    ".wma",
    ".dsf",
    ".dff",
}


class IndexNotReady(RuntimeError):
    pass


class ScanCancelled(Exception):
    """User requested cancellation, distinct from shutting down the worker."""


def validate_roots(values, config):
    if not isinstance(values, list) or len(values) > 20:
        raise ValueError("最多配置20个本地目录")
    roots = []
    protected = [config.data_dir.resolve()]
    if config.install_root:
        protected.append(config.install_root.resolve())
    for value in values:
        if (
            not isinstance(value, str)
            or len(value) > 4096
            or not value.startswith("/")
            or ".." in Path(value).parts
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("请输入 NAS 上的绝对目录路径，每行一个")
        path = Path(value).resolve()
        if (
            len(path.parts) < 3
            or not path.parts[1].startswith("vol")
            or not path.parts[1][3:].isdigit()
        ):
            raise ValueError("扫描目录必须位于 /vol 开头的存储卷中")
        if not os.path.ismount(Path("/") / path.parts[1]) or not path.is_dir():
            raise ValueError("扫描目录不存在或存储卷未挂载")
        if any(path.is_relative_to(p) or p.is_relative_to(path) for p in protected):
            raise ValueError("请扫描应用目录之外的音乐文件夹")
        if not any(path.is_relative_to(p) for p in roots):
            roots = [p for p in roots if not p.is_relative_to(path)] + [path]
    return [str(p) for p in roots]


def metadata(path):
    if path.is_symlink():
        return None
    before = path.stat(follow_symlinks=False)
    audio = mutagen.File(path, easy=True)
    if audio is None:
        return None
    tags = audio.tags or {}

    def tag(name, frame):
        value = tags.get(name) or tags.get(frame) or []
        if hasattr(value, "text"):
            value = value.text
        if isinstance(value, (list, tuple)):
            return "、".join(str(v) for v in value)
        return str(value) if value else ""

    after = path.stat(follow_symlinks=False)
    if (
        path.is_symlink()
        or before.st_ino != after.st_ino
        or before.st_dev != after.st_dev
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        return None
    return {
        "path": str(path),
        "title": tag("title", "TIT2") or path.stem,
        "artist": tag("artist", "TPE1"),
        "album": tag("album", "TALB"),
        "genre": tag("genre", "TCON"),
        "duration": round(float(getattr(audio.info, "length", 0)) * 1000),
        "size": after.st_size,
        "mtime_ns": after.st_mtime_ns,
    }


def walk(roots):
    for root in roots:

        def failed(error):
            raise error

        for base, dirs, files in os.walk(root, followlinks=False, onerror=failed):
            dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
            for name in files:
                path = Path(base) / name
                if (
                    path.suffix.lower() in AUDIO_SUFFIXES
                    and not path.is_symlink()
                    and path.is_file()
                ):
                    yield root, path


def next_file(iterator):
    return next(iterator, None)


class Library:
    def __init__(self, config, validator=None):
        self.config = config
        self.path = config.data_dir / "library.json"
        self.report_path = config.data_dir / "library-scan.json"
        self.database = config.data_dir / "library-index.db"
        self.validator = validator or (lambda roots: validate_roots(roots, config))

    def read(self):
        if self.path.exists():
            data = json.loads(self.path.read_text())
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("directories"), list)
                or type(data.get("auto_scan")) is not bool
            ):
                raise ValueError("本地曲库配置无效，请重新保存")
            return {"directories": data["directories"], "auto_scan": data["auto_scan"]}
        # Reuse the already saved download directory if it is external to app data.
        download = str(self.config.download_dir)
        settings = self.config.data_dir / "settings.json"
        if settings.exists():
            download = json.loads(settings.read_text()).get("download_dir") or download
        path = Path(download).resolve()
        external = not path.is_relative_to(self.config.data_dir.resolve())
        try:
            roots = self.validator([str(path)]) if path.is_dir() and external else []
        except ValueError:
            roots = []
        return {"directories": roots, "auto_scan": True}

    def save(self, directories, auto_scan):
        if type(auto_scan) is not bool:
            raise ValueError("自动更新开关必须为布尔值")
        directories = self.validator(directories)
        atomic_text(
            self.path,
            json.dumps(
                {"directories": directories, "auto_scan": auto_scan}, ensure_ascii=False
            )
            + "\n",
        )
        event(self.config.data_dir, "library", "settings.saved")
        return self.state()

    def report(self):
        if not self.report_path.exists():
            return {
                "state": "idle",
                "visited": 0,
                "indexed": 0,
                "skipped": 0,
                "errors": 0,
            }
        return json.loads(self.report_path.read_text())

    def state(self):
        return {**self.read(), "scan": self.report()}

    def connect(self):
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.database, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        db = sqlite3.connect(self.database, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS audio(path TEXT PRIMARY KEY, root TEXT, title TEXT,artist TEXT,album TEXT,duration INTEGER,size INTEGER,mtime_ns INTEGER,seen TEXT)"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(audio)")}
            if "genre" not in columns:
                db.execute("ALTER TABLE audio ADD COLUMN genre TEXT DEFAULT NULL")
        except BaseException:
            db.close()
            raise
        return db

    def store_metadata(self, db, row, root, run):
        db.execute(
            "INSERT OR REPLACE INTO audio(path,root,title,artist,album,duration,size,mtime_ns,seen,genre) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                row["path"],
                root,
                row["title"],
                row["artist"],
                row["album"],
                row["duration"],
                row["size"],
                row["mtime_ns"],
                run,
                row.get("genre", ""),
            ),
        )

    async def scan(self):
        settings = self.read()
        if not settings["directories"]:
            raise ValueError("请先设置本地音乐目录")
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(
            self.config.data_dir / "library-scan.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        lock = os.fdopen(fd, "a")
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            run = uuid.uuid4().hex
            report = {
                "id": run,
                "state": "scanning",
                "directories": settings["directories"],
                "visited": 0,
                "indexed": 0,
                "skipped": 0,
                "errors": 0,
                "started_at": time.time(),
            }
            self.write_report(report)
            event(self.config.data_dir, "library", "scan.started")
            iterator = walk(settings["directories"])
            db = None
            try:
                db = self.connect()
                while item := await asyncio.to_thread(next_file, iterator):
                    if self.cancelled(run):
                        raise ScanCancelled()
                    root, path = item
                    report["visited"] += 1
                    try:
                        info = await asyncio.to_thread(path.stat)
                        previous = db.execute(
                            "SELECT * FROM audio WHERE path=?", (str(path),)
                        ).fetchone()
                        if (
                            previous
                            and previous["genre"] is not None
                            and previous["size"] == info.st_size
                            and previous["mtime_ns"] == info.st_mtime_ns
                        ):
                            row = dict(previous)
                        else:
                            row = await asyncio.to_thread(metadata, path)
                        if row:
                            self.store_metadata(db, row, root, run)
                            report["indexed"] += 1
                        else:
                            report["skipped"] += 1
                    except (OSError, ValueError, mutagen.MutagenError):
                        report["errors"] += 1
                    if report["visited"] % 25 == 0:
                        db.commit()
                        self.write_report(report)
                if self.cancelled(run):
                    raise ScanCancelled()
                if not report["errors"]:
                    for root in settings["directories"]:
                        db.execute(
                            "DELETE FROM audio WHERE root=? AND seen!=?", (root, run)
                        )
                db.commit()
                report.update(state="completed", finished_at=time.time())
                self.write_report(report)
                event(self.config.data_dir, "library", "scan.completed")
                return True
            except (ScanCancelled, asyncio.CancelledError) as exc:
                report.update(state="cancelled", finished_at=time.time())
                try:
                    if db:
                        db.commit()
                    self.write_report(report)
                except (OSError, sqlite3.Error):
                    pass
                event(
                    self.config.data_dir, "library", "scan.cancelled", level="WARNING"
                )
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return False
            except Exception as exc:
                if db:
                    db.rollback()
                report.update(state="failed", finished_at=time.time())
                self.write_report(report)
                event(
                    self.config.data_dir,
                    "library",
                    "scan.failed",
                    level="ERROR",
                    error=exc,
                )
                raise
            finally:
                if db:
                    db.close()
        finally:
            lock.close()

    def write_report(self, report):
        atomic_text(self.report_path, json.dumps(report, ensure_ascii=False) + "\n")

    def cancelled(self, run):
        flag = self.config.data_dir / "library-cancel.json"
        return flag.exists() and json.loads(flag.read_text()).get("id") == run

    def cancel(self):
        current = self.report()
        if current.get("state") == "scanning":
            atomic_text(
                self.config.data_dir / "library-cancel.json",
                json.dumps({"id": current["id"]}) + "\n",
            )
        return self.state()

    async def ensure_scanned(self):
        while True:
            settings = self.read()
            if not settings["directories"] or not settings["auto_scan"]:
                return
            report = self.report()
            same = report.get("directories") == settings["directories"]
            if (
                same
                and report.get("state") == "completed"
                and time.time() - report.get("finished_at", 0) < 60
            ):
                return
            if same and report.get("state") == "cancelled":
                raise IndexNotReady("本地扫描已取消，请重新扫描或关闭下载前更新索引")
            if await self.scan():
                if settings == self.read():
                    return
                continue
            report = self.report()
            if (
                report.get("state") == "cancelled"
                and report.get("directories") == settings["directories"]
            ):
                raise IndexNotReady("本地扫描已取消，请重新扫描或关闭下载前更新索引")
            await asyncio.sleep(0.25)

    def songs(self, q="", page=1, size=25):
        if (
            not isinstance(q, str) or len(q) > 200
            or type(page) is not int or not 1 <= page <= 1000000
            or type(size) is not int or not 1 <= size <= 100
        ):
            raise ValueError("歌曲列表查询参数无效")
        result = {"items": [], "total": 0, "page": page, "size": size}
        roots = self.read()["directories"]
        if not roots or not self.database.exists():
            return result
        where = "root IN (" + ",".join("?" for _ in roots) + ")"
        params = list(roots)
        keyword = q.strip().lower()
        if keyword:
            where += " AND (" + " OR ".join(
                f"instr(lower(COALESCE({column},'')), ?) > 0"
                for column in ("title", "artist", "album", "path")
            ) + ")"
            params.extend([keyword] * 4)
        db = self.connect()
        try:
            db.execute("BEGIN")
            result["total"] = db.execute(
                "SELECT count(*) FROM audio WHERE " + where, params
            ).fetchone()[0]
            result["items"] = [dict(row) for row in db.execute(
                "SELECT title,artist,album,duration,path,size FROM audio WHERE "
                + where + " ORDER BY title COLLATE NOCASE,path LIMIT ? OFFSET ?",
                [*params, size, (page - 1) * size],
            )]
            return result
        finally:
            db.close()

    def find_duplicate(self, track):
        if not self.database.exists():
            return None
        roots = self.read()["directories"]
        db = self.connect()
        try:
            for row in db.execute("SELECT * FROM audio"):
                row = dict(row)
                if row["root"] not in roots or not same_recording(track, row):
                    continue
                path = Path(row["path"])
                try:
                    stat = path.stat()
                    if (
                        not path.is_symlink()
                        and path.is_file()
                        and stat.st_size > 0
                        and stat.st_size == row["size"]
                        and stat.st_mtime_ns == row["mtime_ns"]
                    ):
                        return row
                except OSError:
                    continue
        finally:
            db.close()
        return None
