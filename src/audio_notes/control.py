"""Small shared controls and structured activity; never imports inference libraries."""

import fcntl
import json
import os
import time
from pathlib import Path

from .files import atomic_json

MAX_AUTOMATIC_ATTEMPTS = 5


def read_json(path, default=None):
    try:
        result = json.loads(Path(path).read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else ({} if default is None else default)
    except (OSError, ValueError):
        return {} if default is None else default


def paused(config):
    return bool(read_json(config.state / "workbench-control.json").get("paused"))


def set_paused(config, value):
    atomic_json(config.state / "workbench-control.json", {"paused": value, "updated": time.time()})


def lock_busy(path):
    """Inspect an existing flock without creating a file or changing ownership."""
    try:
        with Path(path).open("r") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    except FileNotFoundError:
        pass
    return False


def activity(path, stage, **fields):
    if path:
        previous = read_json(path)
        started = previous.get('started', time.time()) if previous.get('pid') == os.getpid() else time.time()
        atomic_json(
            Path(path), {"stage": stage, "updated": time.time(), "started": started, "pid": os.getpid(), **fields}
        )


def smart_is_automatic(config):
    return bool(config.analysis.get("enabled") and config.analysis.get("automatic"))


def awaiting_retry(status, key=None):
    return status.get("status") == "failed" and status.get("fingerprint") == key


def failed_attempts(status, key=None):
    """Consecutive failures on this exact input; older markers without a count mean one."""
    if status.get("fingerprint") != key:
        return 0
    return status.get("attempts", 1 if status.get("status") == "failed" else 0)


def retry_due(status, retry_seconds, key=None):
    """Repeated failures wait 1x, 2x, 4x... the interval, then only a manual retry runs them."""
    if not awaiting_retry(status, key):
        return True
    attempts = failed_attempts(status, key)
    return (
        attempts < MAX_AUTOMATIC_ATTEMPTS
        and time.time() - status.get("updated", 0) >= retry_seconds * 2 ** (attempts - 1)
    )


def failed_status(previous, exc, key=None):
    attempts = failed_attempts(previous, key) + 1
    error = str(exc)
    if attempts >= MAX_AUTOMATIC_ATTEMPTS:
        error += f"（已连续失败 {attempts} 次，不再自动重试，可手动重试）"
    return {
        "status": "failed",
        "fingerprint": key,
        "attempts": attempts,
        "error": error,
        "updated": time.time(),
    }


def yield_to_workbench(config):
    """Automatic scans yield between files to explicitly queued user work."""
    if paused(config):
        return True
    if os.environ.get("AUDIO_NOTES_WORKBENCH_TASK"):
        return False
    return queued_work(config)


def queued_work(config):
    path = config.state / "workbench.sqlite3"
    if not path.is_file() or not lock_busy(config.state / "workbench-server" / "run.lock"):
        return False
    import sqlite3

    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            return (
                db.execute("SELECT 1 FROM requests WHERE status='queued' LIMIT 1").fetchone()
                is not None
            )
        finally:
            db.close()
    except sqlite3.Error:
        return False
