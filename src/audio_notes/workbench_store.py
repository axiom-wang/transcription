"""Durable UI metadata and requests, independent of generated output."""

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager


class Conflict(ValueError):
    pass


def decode(row):
    item = dict(row)
    try:
        item["speakers"] = json.loads(item.get("speakers") or "{}")
    except ValueError:
        item["speakers"] = {}
    return item


def read_metadata(state, audio_hash):
    """Read-only lookup for the pipeline; never creates the workbench database."""
    path = state / "workbench.sqlite3"
    if not path.is_file():
        return {}
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            row = db.execute("SELECT * FROM metadata WHERE audio_hash=?", (audio_hash,)).fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return {}
    return decode(row) if row else {}


class WorkbenchStore:
    def __init__(self, state):
        self.path = state / "workbench.sqlite3"
        state.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata (
                    audio_hash TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
                    favorite INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 0,
                    updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS note_history (
                    audio_hash TEXT NOT NULL, revision INTEGER NOT NULL,
                    notes TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(audio_hash, revision)
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    title TEXT NOT NULL, status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL, error TEXT,
                    pid INTEGER, log TEXT
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(metadata)")}
            if "speakers" not in columns:
                db.execute("ALTER TABLE metadata ADD COLUMN speakers TEXT NOT NULL DEFAULT '{}'")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(requests)")}
            if "summary" not in columns:
                db.execute("ALTER TABLE requests ADD COLUMN summary TEXT")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def metadata(self):
        with self.connect() as db:
            return {row["audio_hash"]: decode(row) for row in db.execute("SELECT * FROM metadata")}

    def rename_speaker(self, audio_hash, speaker, name):
        """One name per speaker id for the whole recording; empty restores the default."""
        if not isinstance(speaker, str) or not re.fullmatch(r"speaker_[1-9][0-9]{0,2}", speaker):
            raise ValueError("说话人标识无效")
        if not isinstance(name, str):
            raise ValueError("说话人名称必须为文字")
        name = re.sub(r"\s+", " ", name).strip()
        if len(name) > 40 or re.search(r"[\x00-\x1f\x7f*\[\]<>`#|]", name):
            raise ValueError("说话人名称最多 40 个字，且不能包含换行或 * [ ] < > ` # | 等符号")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO metadata (audio_hash,updated) VALUES (?,?)",
                (audio_hash, time.time()),
            )
            row = db.execute(
                "SELECT speakers FROM metadata WHERE audio_hash=?", (audio_hash,)
            ).fetchone()
            names = json.loads(row["speakers"] or "{}")
            if name:
                names[speaker] = name
            else:
                names.pop(speaker, None)
            db.execute(
                "UPDATE metadata SET speakers=?,updated=? WHERE audio_hash=?",
                (json.dumps(names, ensure_ascii=False, sort_keys=True), time.time(), audio_hash),
            )
            return names

    def forget(self, audio_hash):
        with self.connect() as db:
            db.execute("DELETE FROM metadata WHERE audio_hash=?", (audio_hash,))
            db.execute("DELETE FROM note_history WHERE audio_hash=?", (audio_hash,))

    def update(self, audio_hash, changes):
        if set(changes) - {"title", "favorite", "archived", "notes", "revision"}:
            raise ValueError("不支持的资料字段")
        for field in ("title", "notes"):
            if field in changes and not isinstance(changes[field], str):
                raise ValueError("标题与笔记必须为文字")
        if len(changes.get("title", "")) > 200 or len(changes.get("notes", "")) > 500000:
            raise ValueError("标题或笔记过长")
        for field in ("favorite", "archived"):
            if field in changes and not isinstance(changes[field], bool):
                raise ValueError("收藏与归档必须为布尔值")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO metadata (audio_hash,updated) VALUES (?,?)",
                (audio_hash, time.time()),
            )
            row = dict(
                db.execute("SELECT * FROM metadata WHERE audio_hash=?", (audio_hash,)).fetchone()
            )
            if "notes" in changes:
                if changes.get("revision") != row["revision"]:
                    raise Conflict(
                        "笔记已在其他窗口更新。当前输入仍保留，请重新打开资料比较后保存。"
                    )
                db.execute(
                    "INSERT INTO note_history VALUES (?,?,?,?)",
                    (audio_hash, row["revision"], row["notes"], time.time()),
                )
                row["revision"] += 1
            row.update({k: v for k, v in changes.items() if k != "revision"})
            db.execute(
                """UPDATE metadata SET title=?,favorite=?,archived=?,notes=?,revision=?,updated=?
                WHERE audio_hash=?""",
                (
                    row["title"],
                    row["favorite"],
                    row["archived"],
                    row["notes"],
                    row["revision"],
                    time.time(),
                    audio_hash,
                ),
            )
            return dict(
                db.execute("SELECT * FROM metadata WHERE audio_hash=?", (audio_hash,)).fetchone()
            )

    def enqueue(self, kind, payload, title):
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM requests WHERE kind=? AND payload=? AND status IN ('queued','running')",
                (kind, encoded),
            ).fetchone()
            if existing:
                return dict(existing)
            key = uuid.uuid4().hex
            db.execute(
                """INSERT INTO requests (id,kind,payload,title,status,created,updated)
                VALUES (?,?,?,?,'queued',?,?)""",
                (key, kind, encoded, title, now, now),
            )
            return dict(db.execute("SELECT * FROM requests WHERE id=?", (key,)).fetchone())

    def requests(self):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    """SELECT * FROM requests ORDER BY
                CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                priority DESC, created DESC LIMIT 100"""
                )
            ]

    def pending(self):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM requests WHERE status='queued' ORDER BY priority DESC,created LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def claim(self, key):
        with self.connect() as db:
            return (
                db.execute(
                    "UPDATE requests SET status='running',updated=? WHERE id=? AND status='queued'",
                    (time.time(), key),
                ).rowcount
                == 1
            )

    def finish(self, key, status, error=None, summary=None):
        with self.connect() as db:
            db.execute(
                "UPDATE requests SET status=?,error=?,summary=?,updated=? WHERE id=?",
                (status, error, summary, time.time(), key),
            )

    def request_action(self, key, action):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM requests WHERE id=?", (key,)).fetchone()
            if not row:
                raise ValueError("请求不存在")
            if action in ("cancel", "prioritize") and row["status"] != "queued":
                raise Conflict("任务已开始或已结束；只能调整尚未开始的请求")
            if action == "cancel":
                db.execute(
                    "UPDATE requests SET status='cancelled',updated=? WHERE id=?",
                    (time.time(), key),
                )
            elif action == "prioritize":
                db.execute(
                    "UPDATE requests SET priority=(SELECT COALESCE(MAX(priority),0)+1 FROM requests),updated=? WHERE id=?",
                    (time.time(), key),
                )
            elif action == "retry" and row["status"] in ("failed", "interrupted"):
                db.execute(
                    "UPDATE requests SET status='queued',error=NULL,summary=NULL,updated=? WHERE id=?",
                    (time.time(), key),
                )
            else:
                raise ValueError("当前请求不能执行此操作")

    def set_process(self, key, pid, log):
        with self.connect() as db:
            db.execute("UPDATE requests SET pid=?,log=? WHERE id=?", (pid, str(log), key))

    def recover(self):
        with self.connect() as db:
            db.execute(
                """UPDATE requests SET status='interrupted', updated=?,
                error='工作台上次运行中断。已完成结果保留，请检查后重试。'
                WHERE status='running'""",
                (time.time(),),
            )
