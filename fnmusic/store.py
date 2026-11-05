"""Extension state only. Never opens the official music database."""

import json
import os
import sqlite3
from pathlib import Path

from .models import track_from_raw


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS track(guid TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS favorite(user TEXT, guid TEXT, PRIMARY KEY(user,guid));
            CREATE TABLE IF NOT EXISTS job(user TEXT, guid TEXT, state TEXT NOT NULL DEFAULT 'pending',
              attempts INTEGER DEFAULT 0, next_at REAL DEFAULT 0, error TEXT DEFAULT '',
              path TEXT DEFAULT '', official_guid TEXT DEFAULT '', PRIMARY KEY(user,guid));
            CREATE TABLE IF NOT EXISTS file(guid TEXT PRIMARY KEY, path TEXT, quality TEXT);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(job)")}
        if "scan_at" not in columns:
            with self.db:
                self.db.execute(
                    "ALTER TABLE job ADD COLUMN scan_at REAL NOT NULL DEFAULT 0"
                )

    def save_track(self, raw):
        track = track_from_raw(raw)
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO track VALUES(?,?)",
                (track["guid"], json.dumps(track, ensure_ascii=False)),
            )
        return track

    def get_track(self, guid):
        row = self.db.execute(
            "SELECT payload FROM track WHERE guid=?", (guid,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def like(self, user, guid):
        if not self.get_track(guid):
            raise ValueError("Unknown online track")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO favorite VALUES(?,?)", (user, guid))
            self.db.execute(
                "INSERT OR IGNORE INTO job(user,guid) VALUES(?,?)", (user, guid)
            )
            self.db.execute(
                "UPDATE job SET state='pending',next_at=0,error='',attempts=0 WHERE user=? AND guid=? AND state IN ('cancelled','failed')",
                (user, guid),
            )

    def unlike(self, user, guid):
        with self.db:
            self.db.execute(
                "DELETE FROM favorite WHERE user=? AND guid=?", (user, guid)
            )
            self.db.execute(
                "UPDATE job SET state='cancelled' WHERE user=? AND guid=?", (user, guid)
            )

    def is_liked(self, user, guid):
        return (
            self.db.execute(
                "SELECT 1 FROM favorite WHERE user=? AND guid=?", (user, guid)
            ).fetchone()
            is not None
        )

    def favorites(self, user):
        rows = self.db.execute(
            "SELECT t.payload FROM track t JOIN favorite f USING(guid) WHERE f.user=? ORDER BY f.rowid DESC",
            (user,),
        )
        return [json.loads(r[0]) for r in rows]

    def jobs(self, user):
        return [
            dict(row)
            for row in self.db.execute("SELECT * FROM job WHERE user=?", (user,))
        ]

    def update_job(self, user, guid, **values):
        if not values or set(values) - {
            "state",
            "attempts",
            "next_at",
            "error",
            "path",
            "official_guid",
            "scan_at",
        }:
            raise ValueError("Invalid job update")
        with self.db:
            self.db.execute(
                "UPDATE job SET "
                + ",".join(k + "=?" for k in values)
                + " WHERE user=? AND guid=?",
                (*values.values(), user, guid),
            )

    def file(self, guid):
        row = self.db.execute(
            "SELECT path,quality FROM file WHERE guid=?", (guid,)
        ).fetchone()
        if row and Path(row[0]).is_file() and Path(row[0]).stat().st_size:
            return dict(row)
        return None

    def save_file(self, guid, path, quality):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO file VALUES(?,?,?)", (guid, str(path), quality)
            )

    def close(self):
        self.db.close()
