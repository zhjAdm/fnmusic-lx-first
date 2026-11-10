"""Persistent, attributed playback activity; never a claim of completed listening."""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import re
import sqlite3
import time

from .models import normalize

PLAYED = {"play.redirect", "play.proxy_started", "play.hls_started",
          "play.local_forwarded", "play.playlist_forwarded"}


def user_key(user):
    return "user_" + hashlib.sha256(str(user).encode()).hexdigest()[:12]


class History:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.path = data_dir / "playback.db"

    @contextmanager
    def connect(self):
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=2)
        self.path.chmod(0o600)
        db.row_factory = sqlite3.Row
        try:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS playback(
                    user TEXT, song TEXT, title TEXT, artist TEXT, style TEXT, guid TEXT,
                    first_at REAL, last_at REAL, count INTEGER,
                    PRIMARY KEY(user,song));
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY);
            """)
            with db:
                # Claim migration under a write lock, including for multiple services.
                if db.execute("INSERT OR IGNORE INTO meta VALUES('logs-imported')").rowcount:
                    from .logs import read_events

                    for row in read_events(self.data_dir, limit=500, component="history"):
                        if (row.get("event") not in PLAYED
                                or not re.fullmatch(r"user_[0-9a-f]{12}", row.get("user", ""))):
                            continue
                        try:
                            at = datetime.fromisoformat(row["time"]).timestamp()
                        except (KeyError, ValueError, TypeError):
                            continue
                        self.upsert(db, row["user"], dict(row, guid=row.get("task", "")), at)
            yield db
        finally:
            db.close()

    @staticmethod
    def upsert(db, user, track, at):
        title, artist = str(track.get("title") or "")[:500], str(track.get("artist") or "")[:500]
        guid = str(track.get("guid") or "")[:200]
        if title and artist:
            song = repr((normalize(title), normalize(artist)))
        elif guid:
            song = "guid:" + guid
        else:
            return
        db.execute("""
            INSERT INTO playback VALUES(?,?,?,?,?,?,?,?,1)
            ON CONFLICT(user,song) DO UPDATE SET
                first_at=min(first_at,excluded.first_at),
                last_at=max(last_at,excluded.last_at), count=count+1,
                style=CASE WHEN excluded.style!='' THEN excluded.style ELSE style END,
                guid=CASE WHEN excluded.guid!='' THEN excluded.guid ELSE guid END
        """, (user, song, title, artist, str(track.get("style") or "")[:500], guid, at, at))

    def record(self, user, track, at=None):
        with self.connect() as db, db:
            self.upsert(db, user_key(user), track, time.time() if at is None else at)

    def songs(self, user):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT title,artist,style,guid,first_at,last_at,count FROM playback "
                "WHERE user=? ORDER BY last_at DESC", (user_key(user),),
            )]
