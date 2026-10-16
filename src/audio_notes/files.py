import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def model_lock(models):
    """Share GPU memory across ASR, text processing and verification configurations."""
    models.mkdir(parents=True, exist_ok=True)
    with (models / ".inference.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        # Closing releases our descriptor; inherited descriptors retain the lock
        # if an orphaned native worker is still using the GPU.
        yield handle


def signature(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def sha256_file(path: Path) -> str:
    before = signature(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    if signature(path) != before:
        raise RuntimeError("文件在读取时发生变化，等同步完成后重试")
    return digest.hexdigest()


def atomic_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_json(path, data):
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


@contextmanager
def run_lock(state, blocking=False):
    state.mkdir(parents=True, exist_ok=True)
    with (state / "run.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise AlreadyRunning(
                "另一个 run/watch/download-model 正在运行，请等待或停止它"
            ) from None
        try:
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
