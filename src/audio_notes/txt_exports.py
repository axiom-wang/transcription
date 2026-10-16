"""Publish readable TXT copies beside the detailed output directory."""

import re

from .diarization import rename_speakers
from .files import atomic_text
from .markdown import simplified_text


def recording_name(config, raw, directory):
    """Workbench rename first, then the generated one-sentence name with time, then the source."""
    from .titles import display_name
    from .workbench_store import read_metadata

    meta = read_metadata(config.state, raw.get("audio_sha256", ""))
    return meta.get("title") or display_name(directory, raw) or raw["source"]["name"], meta


def txt_filename(raw, name=None):
    title = simplified_text(name or raw["source"]["name"]).replace(":", "：")
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", title)
    title = re.sub(r"\s+", " ", title).strip(" .")
    # Leave room for the identifier and extension on filesystems with byte limits.
    title = title.encode("utf-8")[:180].decode("utf-8", errors="ignore").strip(" .") or "录音"
    job_id = raw["job_id"]
    if not re.fullmatch(r"[0-9a-f]{64}", job_id):
        raise ValueError("TXT 导出的任务标识无效")
    return f"{title}（{job_id[:12]}）.txt"


def publish_txt(config, raw, directory):
    name, meta = recording_name(config, raw, directory)
    text = rename_speakers(
        (directory / "transcript.simplified.txt").read_text(encoding="utf-8"),
        meta.get("speakers"),
    )
    destination = config.txt_output / txt_filename(raw, name)
    if destination.is_symlink():
        raise ValueError(f"TXT 目标不能是符号链接：{destination}")
    if not destination.is_file() or destination.read_text(encoding="utf-8") != text:
        atomic_text(destination, text)
    # A renamed recording keeps exactly one TXT: remove copies published under older names.
    for stale in config.txt_output.glob(f"*（{raw['job_id'][:12]}）.txt"):
        if stale != destination and stale.is_file() and not stale.is_symlink():
            stale.unlink()
    return destination
