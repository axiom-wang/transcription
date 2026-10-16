"""Read-only discovery of Apple's local Voice Memos cache, with atomic private copies.

Apple owns synchronization. This importer never writes to its directories/database,
never requests cloud credentials and never imports unrelated iCloud Drive files.
"""

import hashlib
import json
import logging
import os
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .files import atomic_json, sha256_file

log = logging.getLogger("audio_notes")
ACCESS_HELP = (
    "macOS 拒绝读取语音备忘录。请在「系统设置 → 隐私与安全性 → 完全磁盘访问权限」"
    "允许实际运行扫描的应用：从听记扫描时允许「听记」，从终端运行时允许该终端；"
    "仍被拒绝时，再添加 service status 显示的 Python 可执行文件，重启听记后重新扫描。"
)
IGNORED_DIRS = {
    "RecentlyDeleted",
    "Recently Deleted",
    "Trash",
    "Temporary",
    "tmp",
    "CaptureRecovery",
    "CloudRecordings_ckAssets",
}


@dataclass
class ImportCounts:
    copied: int = 0
    skipped: int = 0
    waiting: int = 0
    failed: int = 0
    found: int = 0
    roots: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def source_signature(path):
    s = path.stat()
    return [s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino]


def discover(root: Path, extensions, errors):
    """scandir reports permission errors; pathlib.rglob may silently hide them."""
    try:
        with os.scandir(root) as entries:
            entries = sorted(entries, key=lambda entry: entry.name)
    except FileNotFoundError:
        return
    except OSError as exc:
        errors.append(
            {
                "path": str(root),
                "error": str(exc),
                "permission_denied": isinstance(exc, PermissionError),
            }
        )
        return
    for entry in entries:
        if entry.name.startswith(".") or entry.name in IGNORED_DIRS or entry.is_symlink():
            continue
        path = Path(entry.path)
        try:
            if entry.is_dir(follow_symlinks=False):
                yield from discover(path, extensions, errors)
            elif entry.is_file(follow_symlinks=False) and path.suffix.lower() in extensions:
                yield path
        except OSError as exc:
            errors.append(
                {
                    "path": str(path),
                    "error": str(exc),
                    "permission_denied": isinstance(exc, PermissionError),
                }
            )


def inspect_sources(config):
    errors, files, roots = [], [], []
    extensions = {"." + value.lower().lstrip(".") for value in config.processing["extensions"]}
    seen = set()
    for value in config.voice_memos.get("directories", []):
        root = Path(value)
        found = list(discover(root, extensions, errors))
        roots.append({"path": str(root), "recordings": len(found)})
        for path in found:
            if str(path) not in seen:
                files.append(path)
                seen.add(str(path))
    return files, roots, errors


def recording_metadata(root):
    """Optional labels from the observed Apple schema, always read-only with WAL support."""
    path = root / "CloudRecordings.db"
    metadata = {}
    if not path.is_file():
        return metadata
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            db.execute("PRAGMA query_only=ON")
            db.row_factory = sqlite3.Row
            for table in ("ZCLOUDRECORDING", "ZRECORDING"):
                columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
                if not {"ZPATH", "ZCUSTOMLABEL"}.issubset(columns):
                    continue
                selected = [
                    name
                    for name in ("ZPATH", "ZCUSTOMLABEL", "ZDATE", "ZDURATION", "ZUNIQUEID")
                    if name in columns
                ]
                for row in db.execute(f'SELECT {",".join(selected)} FROM "{table}"'):
                    if not row["ZPATH"]:
                        continue
                    item = {"recording_title": row["ZCUSTOMLABEL"] or Path(row["ZPATH"]).stem}
                    if "ZDATE" in selected and isinstance(row["ZDATE"], (int, float)):
                        item["recorded_at"] = (
                            datetime(2001, 1, 1, tzinfo=timezone.utc)
                            + timedelta(seconds=row["ZDATE"])
                        ).isoformat()
                    if "ZDURATION" in selected:
                        item["recording_duration_seconds"] = row["ZDURATION"]
                    if "ZUNIQUEID" in selected:
                        item["recording_id"] = row["ZUNIQUEID"]
                    metadata.setdefault(Path(row["ZPATH"]).name, item)
    except (OSError, sqlite3.Error, ValueError, OverflowError) as exc:
        log.warning("无法读取录音名称，将使用原始文件名：%s；%s", path, exc)
    return metadata


def atomic_copy(source, destination, expected_signature):
    """Read the source once, hash while copying, and verify it didn't change."""
    destination.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".importing-", dir=destination)
    try:
        digest = hashlib.sha256()
        with source.open("rb") as incoming, os.fdopen(fd, "wb") as outgoing:
            fd = None
            for block in iter(lambda: incoming.read(1024 * 1024), b""):
                digest.update(block)
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if source_signature(source) != expected_signature:
            raise RuntimeError("录音在复制过程中改变，等待 iCloud 同步稳定后重试")
        return Path(name), digest.hexdigest()
    except BaseException:
        if fd is not None:
            os.close(fd)
        Path(name).unlink(missing_ok=True)
        raise


class VoiceMemosImporter:
    def __init__(self, config):
        self.config = config
        self.db = sqlite3.connect(config.state / "imports.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        schema = self.db.execute("PRAGMA user_version").fetchone()[0]
        if schema not in (0, 1):
            raise ValueError("导入数据库版本高于当前程序支持")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS imports (
                source TEXT PRIMARY KEY, signature TEXT NOT NULL, audio_hash TEXT NOT NULL,
                destination TEXT NOT NULL, destination_signature TEXT NOT NULL,
                imported_at TEXT NOT NULL
            );
            PRAGMA user_version=1;
        """)

    def close(self):
        self.db.close()

    def scan(self):
        counts = ImportCounts()
        if not self.config.voice_memos.get("enabled"):
            return counts
        files, counts.roots, errors = inspect_sources(self.config)
        counts.found = len(files)
        counts.errors.extend(errors)
        counts.failed += len(errors)
        metadata = {}
        for value in self.config.voice_memos.get("directories", []):
            root = Path(value)
            if any(path.is_relative_to(root) for path in files):
                for name, info in recording_metadata(root).items():
                    metadata.setdefault(name, info)
        for error in errors:
            log.error(
                "语音备忘录目录读取失败：%s；%s%s",
                error["path"],
                error["error"],
                "；" + ACCESS_HELP if error["permission_denied"] else "",
            )
        from .store import deleted_records

        deleted = deleted_records(self.config.state)
        for source in files:
            temporary = None
            try:
                before = source_signature(source)
                s = source.stat()
                # SF_DATALESS is Apple's on-disk-only placeholder. Do not read it and
                # cause accidental mass downloads. Sync/download belongs to Voice Memos.
                if (
                    getattr(s, "st_flags", 0) & getattr(stat, "SF_DATALESS", 0x40000000)
                    or not s.st_size
                ):
                    counts.waiting += 1
                    log.info("等待录音实际下载到 Mac：%s", source.name)
                    continue
                if (
                    time.time() - max(s.st_mtime, s.st_ctime)
                    < self.config.processing["settle_seconds"]
                ):
                    counts.waiting += 1
                    continue
                previous = self.db.execute(
                    "SELECT * FROM imports WHERE source=?", (str(source),)
                ).fetchone()
                if (
                    previous
                    and json.loads(previous["signature"]) == before
                    and previous["audio_hash"] in deleted
                ):
                    # Deleted in the workbench; an edited recording gets a new hash and returns.
                    counts.skipped += 1
                    continue
                if previous and json.loads(previous["signature"]) == before:
                    destination = Path(previous["destination"])
                    try:
                        saved_origin = json.loads(
                            destination.with_suffix(destination.suffix + ".origin.json").read_text(
                                encoding="utf-8"
                            )
                        )
                        if (
                            source_signature(destination)
                            == json.loads(previous["destination_signature"])
                            and saved_origin.get("audio_sha256") == previous["audio_hash"]
                            and Path(saved_origin["original_path"]).is_absolute()
                        ):
                            counts.skipped += 1
                            continue
                    except (FileNotFoundError, ValueError, KeyError, TypeError, AttributeError):
                        pass
                # A private staging folder keeps partial copies away from transcription.
                staging = self.config.inbox / "voice-memos" / ".staging"
                temporary, digest = atomic_copy(source, staging, before)
                # Exact content copies share one private snapshot. Edited recordings get
                # a new hash directory; old snapshots and outputs are retained.
                directory = self.config.inbox / "voice-memos" / digest
                directory.mkdir(parents=True, exist_ok=True)
                destination = directory / source.name
                if destination.is_file() and sha256_file(destination) == digest:
                    temporary.unlink()
                else:
                    os.replace(temporary, destination)
                    # Copy mtime without touching source metadata.
                    os.utime(destination, ns=(s.st_mtime_ns, s.st_mtime_ns))
                temporary = None
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                imported_at = datetime.now(timezone.utc).isoformat()
                origin = {
                    "kind": "apple-voice-memos",
                    "original_path": str(source),
                    "audio_sha256": digest,
                    "original_mtime_ns": s.st_mtime_ns,
                    "imported_at": imported_at,
                    **metadata.get(source.name, {}),
                }
                atomic_json(destination.with_suffix(destination.suffix + ".origin.json"), origin)
                with self.db:
                    self.db.execute(
                        "INSERT OR REPLACE INTO imports VALUES (?,?,?,?,?,?)",
                        (
                            str(source),
                            json.dumps(before),
                            digest,
                            str(destination),
                            json.dumps(source_signature(destination)),
                            imported_at,
                        ),
                    )
                counts.copied += 1
                log.info("自动复制语音备忘录：%s → %s", source.name, destination)
            except Exception as exc:
                counts.failed += 1
                counts.errors.append(
                    {
                        "path": str(source),
                        "error": str(exc),
                        "permission_denied": isinstance(exc, PermissionError),
                    }
                )
                log.exception("语音备忘录导入失败：%s；%s", source, exc)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        report = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "enabled": True,
            "found": counts.found,
            "copied": counts.copied,
            "skipped": counts.skipped,
            "waiting": counts.waiting,
            "failed": counts.failed,
            "roots": counts.roots,
            "errors": counts.errors,
        }
        atomic_json(self.config.state / "voice-memos-status.json", report)
        log.info(
            "语音备忘录发现 %d，新增复制 %d，已导入 %d，待同步 %d，失败 %d",
            counts.found,
            counts.copied,
            counts.skipped,
            counts.waiting,
            counts.failed,
        )
        return counts
