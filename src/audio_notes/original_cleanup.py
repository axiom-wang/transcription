"""Delete the Apple Voice Memos original once its private copy is fully transcribed.

The Apple database is only ever opened read-only. The deletion itself goes through the
Voice Memos "Delete Recording" App Intent via a user-created Shortcut, so Voice Memos
and iCloud handle it like a deletion made in the app (Recently Deleted, every device).
Files are never removed from Apple's directories directly: Voice Memos would treat a
missing file as an evicted iCloud copy and download it again.

Before a recording is deleted, all of the following must hold:
  * the original is unchanged since it was copied;
  * the private copy still exists and its SHA-256 matches the imported audio;
  * the transcription job for that audio completed with a valid raw.json and exports;
  * the decoded audio length matches the duration recorded by Voice Memos (full decode);
  * the transcript text is not empty;
  * the recording name is unambiguous among the other recordings.
After the Shortcut runs, the Apple database must show that the target changed and that
no other recording disappeared; otherwise automatic deletion halts until reviewed.
"""

import json
import logging
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .files import atomic_json, sha256_file

log = logging.getLogger("audio_notes")
SHORTCUTS = "/usr/bin/shortcuts"
HALT_FILE = "original-deletion-halted.json"
MAX_ATTEMPTS = 3
VERIFY_SECONDS = 15


@dataclass
class CleanupCounts:
    deleted: int = 0
    pending: int = 0
    blocked: int = 0
    failed: int = 0
    details: list = field(default_factory=list)


def duration_matches(decoded, expected):
    if not all(isinstance(v, (int, float)) for v in (decoded, expected)):
        return False
    if decoded <= 0 or expected <= 0:
        return False
    return abs(decoded - expected) <= max(2.0, 0.02 * expected)


def apple_recordings(root):
    """Read-only snapshot {uid: row} of the Voice Memos library at root."""
    path = root / "CloudRecordings.db"
    if not path.is_file():
        return None
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
        db.execute("PRAGMA query_only=ON")
        db.row_factory = sqlite3.Row
        columns = {row[1] for row in db.execute('PRAGMA table_info("ZCLOUDRECORDING")')}
        if not {"ZPATH", "ZUNIQUEID", "ZDURATION", "Z_OPT"}.issubset(columns):
            return None
        wanted = [
            name
            for name in (
                "ZUNIQUEID",
                "ZPATH",
                "ZDURATION",
                "Z_OPT",
                "ZFLAGS",
                "ZFOLDER",
                "ZENCRYPTEDTITLE",
                "ZCUSTOMLABEL",
            )
            if name in columns
        ]
        rows = {}
        for row in db.execute(f'SELECT {",".join(wanted)} FROM "ZCLOUDRECORDING"'):
            item = dict(row)
            if not item.get("ZUNIQUEID"):
                continue
            # Voice Memos displays ZENCRYPTEDTITLE (plain text despite the name).
            item["title"] = (item.get("ZENCRYPTEDTITLE") or item.get("ZCUSTOMLABEL") or "").strip()
            item["state"] = (item.get("Z_OPT"), item.get("ZFLAGS"), item.get("ZFOLDER"))
            rows[item["ZUNIQUEID"]] = item
        return rows


def find_target(rows, source):
    matches = [r for r in rows.values() if r.get("ZPATH") and Path(r["ZPATH"]).name == source.name]
    return matches[0] if len(matches) == 1 else None


def ambiguous(rows, target):
    """The App Intent resolves recordings by name; refuse names another title contains."""
    title = target["title"].casefold()
    if not title:
        return True
    return any(
        uid != target["ZUNIQUEID"] and title in row["title"].casefold() for uid, row in rows.items()
    )


def shortcut_installed(name):
    try:
        result = subprocess.run([SHORTCUTS, "list"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and name in result.stdout.splitlines()


def run_shortcut(name, title):
    with tempfile.TemporaryDirectory() as directory:
        payload = Path(directory) / "recording.txt"
        payload.write_text(title, encoding="utf-8")
        result = subprocess.run(
            [SHORTCUTS, "run", name, "--input-path", str(payload)],
            capture_output=True,
            text=True,
            timeout=180,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"快捷指令「{name}」运行失败（{result.returncode}）："
            + (result.stderr or result.stdout).strip()[-300:]
        )


def verify_deleted(root, before, target, wait=VERIFY_SECONDS):
    """True when the target changed/vanished; raises when other recordings vanished."""
    deadline = time.monotonic() + wait
    while True:
        after = apple_recordings(root) or {}
        vanished = [uid for uid in before if uid not in after and uid != target]
        if vanished:
            raise PermissionError(
                "删除后发现其他录音也从语音备忘录消失："
                + "、".join(before[uid]["title"] or uid for uid in vanished)
            )
        if target not in after or after[target]["state"] != before[target]["state"]:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


class OriginalCleaner:
    def __init__(
        self, config, import_db, store, profile, runner=run_shortcut, installed=shortcut_installed
    ):
        self.config, self.db, self.store, self.profile = config, import_db, store, profile
        self.runner, self.installed = runner, installed
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS original_deletions (
                recording TEXT PRIMARY KEY, source TEXT NOT NULL, audio_hash TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT, updated TEXT NOT NULL
            );
        """)

    @property
    def enabled(self):
        return bool(
            self.config.voice_memos.get("enabled")
            and self.config.voice_memos.get("delete_original_after_transcription")
        )

    def record(self, uid, source, audio_hash, status, error=None, attempt=False):
        with self.db:
            self.db.execute(
                """INSERT INTO original_deletions VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(recording) DO UPDATE SET source=excluded.source,
                audio_hash=excluded.audio_hash, status=excluded.status,
                attempts=attempts+?, error=excluded.error, updated=excluded.updated""",
                (
                    uid,
                    str(source),
                    audio_hash,
                    status,
                    int(attempt),
                    error,
                    datetime.now(timezone.utc).isoformat(),
                    int(attempt),
                ),
            )

    def previous(self, uid):
        return self.db.execute(
            "SELECT * FROM original_deletions WHERE recording=?", (uid,)
        ).fetchone()

    def transcribed(self, audio_hash):
        """Valid raw transcript for this audio, or a reason why not."""
        from .pipeline import exports_valid, load_raw

        profile = self.profile
        if audio_hash in self.store.deleted():
            return None, "听记中已删除该录音的文稿"
        row = self.store.db.execute(
            "SELECT * FROM jobs WHERE audio_hash=? AND profile=?", (audio_hash, profile)
        ).fetchone()
        if not row or row["status"] != "completed":
            return None, "转写尚未完成"
        directory = Path(row["output"])
        raw = load_raw(directory / "raw.json", row["id"], audio_hash, profile)
        if raw is None or not exports_valid(directory, self.config.markdown):
            return None, "转写结果未通过校验"
        if not raw["segments"] or not raw["text"].strip():
            return None, "转写文本为空"
        return raw, None

    def check(self, imported, rows):
        """Return (target_row, reason). A reason means: keep the original for now."""
        from .voice_memos import source_signature

        source = Path(imported["source"])
        if not source.is_file():
            return None, "原录音不在本机"
        if source_signature(source) != json.loads(imported["signature"]):
            return None, "原录音复制后发生变化，等待重新导入"
        destination = Path(imported["destination"])
        if not destination.is_file() or sha256_file(destination) != imported["audio_hash"]:
            return None, "本地副本缺失或与原录音不一致"
        target = find_target(rows, source)
        if target is None:
            return None, "语音备忘录数据库中找不到唯一对应的录音"
        try:
            origin = json.loads(
                destination.with_suffix(destination.suffix + ".origin.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError):
            return None, "本地副本来源记录缺失"
        if (
            origin.get("audio_sha256") != imported["audio_hash"]
            or origin.get("recording_id", target["ZUNIQUEID"]) != target["ZUNIQUEID"]
        ):
            return None, "本地副本来源记录与录音不一致"
        raw, reason = self.transcribed(imported["audio_hash"])
        if reason:
            return None, reason
        if not duration_matches(raw["info"].get("duration"), target.get("ZDURATION")):
            return None, (
                f"解码时长 {raw['info'].get('duration')} 秒与语音备忘录记录的 "
                f"{target.get('ZDURATION')} 秒不符，未确认完整解码"
            )
        return target, None

    def run(self):
        counts = CleanupCounts()
        if not self.enabled:
            return counts
        halt = self.config.state / HALT_FILE
        if halt.exists():
            log.warning("自动删除原录音已暂停，请检查 %s 后删除该文件以恢复", halt)
            return counts
        name = self.config.voice_memos.get("delete_shortcut", "")
        imports = self.db.execute("SELECT * FROM imports ORDER BY imported_at").fetchall()
        shortcut_ready = None
        snapshots = {}
        for imported in imports:
            target = None
            source = Path(imported["source"])
            root = next(
                (
                    Path(d)
                    for d in self.config.voice_memos.get("directories", [])
                    if source.is_relative_to(Path(d))
                ),
                None,
            )
            if root is None:
                continue
            try:
                if root not in snapshots:
                    snapshots[root] = apple_recordings(root)
                rows = snapshots[root]
                if rows is None:
                    continue
                target, reason = self.check(imported, rows)
                if target is None:
                    if reason not in ("原录音不在本机", "转写尚未完成"):
                        counts.pending += 1
                        counts.details.append({"source": str(source), "reason": reason})
                    continue
                uid = target["ZUNIQUEID"]
                previous = self.previous(uid)
                if previous and previous["status"] == "deleted":
                    # Restored from Recently Deleted by the user: never delete it again.
                    continue
                if previous and previous["attempts"] >= MAX_ATTEMPTS:
                    counts.failed += 1
                    continue
                if ambiguous(rows, target):
                    counts.blocked += 1
                    reason = f"录音名称「{target['title']}」与其他录音重名或被包含，请在语音备忘录中手动删除"
                    if not previous or previous["status"] != "blocked":
                        log.warning("未自动删除：%s", reason)
                    self.record(uid, source, imported["audio_hash"], "blocked", reason)
                    counts.details.append({"source": str(source), "reason": reason})
                    continue
                if shortcut_ready is None:
                    shortcut_ready = bool(name) and self.installed(name)
                    if not shortcut_ready:
                        log.warning("未找到快捷指令「%s」，原录音保留；创建方法见 README", name)
                if not shortcut_ready:
                    counts.pending += 1
                    counts.details.append(
                        {"source": str(source), "reason": f"未找到快捷指令「{name}」"}
                    )
                    continue
                log.info("转写已确认，删除语音备忘录原录音：%s（%s）", target["title"], source.name)
                self.runner(name, target["title"])
                if not verify_deleted(root, rows, uid):
                    raise RuntimeError("快捷指令已运行，但语音备忘录中录音未变化")
                self.record(uid, source, imported["audio_hash"], "deleted")
                snapshots.pop(root, None)
                counts.deleted += 1
                log.info(
                    "已删除语音备忘录原录音：%s；本地副本 %s",
                    target["title"],
                    imported["destination"],
                )
            except PermissionError as exc:
                # Something other than the target vanished: stop all automatic deletions.
                atomic_json(halt, {"at": datetime.now(timezone.utc).isoformat(), "error": str(exc)})
                log.error("%s；已暂停自动删除原录音，请到语音备忘录「最近删除」检查并恢复", exc)
                from .notifications import notify

                notify(
                    self.config,
                    "已暂停删除原录音",
                    "发现意外变化，请到语音备忘录「最近删除」检查。",
                )
                counts.failed += 1
                break
            except Exception as exc:
                counts.failed += 1
                if target is not None:
                    self.record(
                        target["ZUNIQUEID"],
                        source,
                        imported["audio_hash"],
                        "failed",
                        str(exc),
                        attempt=True,
                    )
                log.exception("删除语音备忘录原录音失败，原录音保留：%s；%s", source, exc)
                snapshots.pop(root, None)
        atomic_json(
            self.config.state / "original-deletions-status.json",
            {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "deleted": counts.deleted,
                "pending": counts.pending,
                "blocked": counts.blocked,
                "failed": counts.failed,
                "details": counts.details,
            },
        )
        if counts.deleted or counts.blocked or counts.failed:
            log.info(
                "原录音删除：删除 %d，需手动 %d，失败 %d，暂缓 %d",
                counts.deleted,
                counts.blocked,
                counts.failed,
                counts.pending,
            )
        return counts
