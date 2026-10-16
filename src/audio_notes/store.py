import json
import sqlite3
import time
from pathlib import Path


def deleted_records(state):
    """Tombstones written by the workbench: {audio_hash: [known paths]}; read-only."""
    path = state / "jobs.sqlite3"
    if not path.is_file():
        return {}
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        return {row[0]: json.loads(row[1]) for row in db.execute("SELECT * FROM deleted")}
    except sqlite3.OperationalError:
        return {}
    finally:
        db.close()


class Store:
    def __init__(self, state: Path):
        self.db = sqlite3.connect(state / "jobs.sqlite3")
        self.db.row_factory = sqlite3.Row
        schema = self.db.execute("PRAGMA user_version").fetchone()[0]
        if schema not in (0, 1):
            self.db.close()
            raise ValueError(f"任务库版本 {schema} 高于当前程序支持；请使用匹配版本")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, audio_hash TEXT NOT NULL, profile TEXT NOT NULL,
                source TEXT NOT NULL, output TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT, created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sources (
                job_id TEXT NOT NULL REFERENCES jobs(id), path TEXT NOT NULL,
                seen REAL NOT NULL, PRIMARY KEY(job_id, path)
            );
            CREATE TABLE IF NOT EXISTS deleted (
                audio_hash TEXT PRIMARY KEY, paths TEXT NOT NULL, deleted REAL NOT NULL
            );
            PRAGMA user_version=1;
        """)

    def register(self, job_id, audio_hash, profile, source, output):
        now = time.time()
        with self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO jobs
                (id,audio_hash,profile,source,output,created,updated) VALUES (?,?,?,?,?,?,?)""",
                (job_id, audio_hash, profile, str(source), str(output), now, now),
            )
            self.db.execute("UPDATE jobs SET output=? WHERE id=?", (str(output), job_id))
            self.db.execute(
                "INSERT OR REPLACE INTO sources VALUES (?,?,?)", (job_id, str(source), now)
            )
        return self.get(job_id)

    def get(self, job_id):
        return dict(self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def completed(self):
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM jobs WHERE status='completed' ORDER BY created DESC"
            )
        ]

    def mark(self, job_id, status, error=None, attempt=False):
        with self.db:
            self.db.execute(
                """UPDATE jobs SET status=?, error=?, updated=?,
                attempts=attempts+? WHERE id=?""",
                (status, error, time.time(), int(attempt), job_id),
            )

    def recover(self):
        with self.db:
            return self.db.execute("""UPDATE jobs SET status='interrupted',
                error='上一次进程中断；下次扫描自动恢复' WHERE status='running'""").rowcount

    def deleted(self):
        return {row[0] for row in self.db.execute("SELECT audio_hash FROM deleted")}

    def restore(self, audio_hash):
        with self.db:
            self.db.execute("DELETE FROM deleted WHERE audio_hash=?", (audio_hash,))

    def close(self):
        self.db.close()
