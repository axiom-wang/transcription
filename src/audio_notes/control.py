"""Small shared controls and structured activity; never imports inference libraries."""

import fcntl
import json
import os
import time
from pathlib import Path

from .files import atomic_json


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


def yield_to_workbench(config):
    """Automatic scans yield between files to explicitly queued user work."""
    if paused(config):
        return True
    if os.environ.get("AUDIO_NOTES_WORKBENCH_TASK"):
        return False
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
