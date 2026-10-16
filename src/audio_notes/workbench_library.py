"""Read-only projections of recordings, generated artifacts and current activity."""

import re
import sqlite3
from functools import lru_cache
from pathlib import Path

from .control import lock_busy, read_json
from .files import sha256_file

ARTIFACTS = {
    "notes.md": "忠实阅读版",
    "transcript.simplified.md": "简体逐段稿",
    "transcript.md": "模型原始稿",
    "polished.md": "整理稿 Markdown",
    "polished.txt": "整理稿 TXT",
    "analysis.md": "分析稿",
    "transcript.simplified.txt": "完整转写 TXT",
    "transcript.txt": "原始转写 TXT",
    "transcript.simplified.srt": "简体字幕",
    "transcript.srt": "原始字幕",
    "raw.json": "原始识别数据",
}


def rows(config):
    path = config.state / "jobs.sqlite3"
    if not path.is_file():
        return []
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY created DESC")]
    finally:
        db.close()


def job(config, key):
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise ValueError("无效的录音标识")
    found = next((row for row in rows(config) if row["id"] == key), None)
    if not found:
        raise FileNotFoundError("录音不存在")
    directory = Path(found["output"]).resolve()
    if directory != (config.output / key).resolve() or directory.parent != config.output.resolve():
        raise ValueError("录音产物目录无效")
    return found


def artifact(config, key, name):
    if name not in ARTIFACTS:
        raise ValueError("不支持的文稿类型")
    directory = Path(job(config, key)["output"]).resolve()
    path = directory / name
    if path.is_symlink() or not path.is_file() or path.resolve().parent != directory:
        raise FileNotFoundError("这份文稿尚未生成")
    return path


@lru_cache(maxsize=128)
def audio_digest(path, signature):
    from .files import sha256_file

    return sha256_file(Path(path))


def audio_path(config, key):
    entry = job(config, key)
    candidates = [entry["source"]]
    db = sqlite3.connect((config.state / "jobs.sqlite3").as_uri() + "?mode=ro", uri=True)
    try:
        candidates.extend(
            row[0] for row in db.execute("SELECT path FROM sources WHERE job_id=?", (key,))
        )
    finally:
        db.close()
    for value in candidates:
        path = Path(value)
        if path.is_file() and not path.is_symlink():
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
            if audio_digest(str(path), signature) == entry["audio_hash"]:
                return path
    raise FileNotFoundError("原音频已移动或不可访问；转写文稿仍可阅读")


def summarize(config, entry, metadata, busy):
    directory = Path(entry["output"])
    if directory.resolve().parent != config.output.resolve():
        return None
    raw = read_json(directory / "raw.json")
    if not isinstance(raw, dict):
        raw = {}
    origin = raw.get("import_origin") or read_json(
        Path(entry["source"]).with_suffix(Path(entry["source"]).suffix + ".origin.json")
    )
    meta = metadata.get(entry["audio_hash"], {})
    from .titles import display_name

    generated = display_name(directory, raw, origin)
    title = (
        meta.get("title")
        or generated
        or raw.get("source", {}).get("name")
        or origin.get("recording_title")
        or Path(entry["source"]).stem
    )
    available = [
        name
        for name in ARTIFACTS
        if (directory / name).is_file() and not (directory / name).is_symlink()
    ]
    text = read_json(directory / "enrichment.status.json")
    receipt = read_json(directory / "enrichment.complete.json")
    speaker = read_json(directory / "speakers.status.json")
    speaker_status = speaker.get(
        "status", "pending" if config.speakers.get("enabled") else "disabled"
    )
    if speaker_status == "running" and not busy["asr"]:
        speaker_status = "interrupted"
    asr_status = entry["status"]
    if asr_status == "running" and not busy["asr"]:
        asr_status = "interrupted"
    text_status = text.get("status", "pending" if config.analysis.get("enabled") else "disabled")
    if text_status == "running" and not busy["text"]:
        text_status = "interrupted"
    complete = bool(receipt) and all(
        name in available for name in ("polished.md", "polished.txt", "analysis.md")
    )
    speaker_file = directory / "speakers.json"
    text_speaker_stale = (
        bool(receipt)
        and speaker_file.is_file()
        and receipt.get("speakers_sha256") != sha256_file(speaker_file)
    )
    if text_status not in ("running", "failed", "interrupted") and complete:
        text_status = "completed"
    if text_status == "completed" and not complete:
        text_status = "pending"
    if (
        asr_status == "completed"
        and "transcript.simplified.md" not in available
        and "transcript.md" not in available
    ):
        asr_status = "missing"
    active = text_status == "running" or asr_status == "running" or speaker_status == "running"
    activity_name = (
        f"text-{entry['id']}"
        if text_status == "running"
        else f"speakers-{entry['id']}"
        if speaker_status == "running"
        else entry["id"]
    )
    progress = read_json(config.state / "activity" / f"{activity_name}.json") if active else {}
    # Older in-flight workers do not publish structured activity. Report that honestly.
    if active and not progress:
        log_path = config.state / "logs" / f"{activity_name}.log"
        progress = {
            "stage": "正在处理（当前进程未提供细分进度）",
            "updated": log_path.stat().st_mtime if log_path.exists() else entry["updated"],
        }
    return {
        "id": entry["id"],
        "audio_hash": entry["audio_hash"],
        "title": title,
        "title_source": "custom" if meta.get("title") else "generated" if generated else "original",
        "created": entry["created"],
        "updated": entry["updated"],
        "recorded_at": origin.get("recorded_at"),
        "duration": raw.get("info", {}).get("duration"),
        "source_kind": "语音备忘录" if origin else "手动导入",
        "asr_status": asr_status,
        "text_status": text_status,
        "speaker_status": speaker_status,
        "speaker_error": speaker.get("error"),
        "asr_error": entry.get("error"),
        "text_error": text.get("error"),
        "available": available,
        "text_available": complete,
        "text_speaker_stale": text_speaker_stale,
        "favorite": bool(meta.get("favorite")),
        "archived": bool(meta.get("archived")),
        "needs_review": receipt.get("quality_status") == "needs_review",
        "progress": progress,
        "attempts": entry["attempts"],
        "preview": re.sub(r"\s+", " ", raw.get("text", ""))[:170],
    }


def library(config, store):
    metadata = store.metadata()
    busy = {
        "asr": lock_busy(config.state / "run.lock"),
        "text": lock_busy(config.state / "text-processing" / "run.lock"),
    }
    groups = {}
    for entry in rows(config):
        item = summarize(config, entry, metadata, busy)
        if item is None:
            continue
        key = item["audio_hash"]
        version = {"id": item["id"], "created": item["created"], "asr_status": item["asr_status"]}
        if key not in groups:
            groups[key] = {**item, "versions": [version]}
        else:
            groups[key]["versions"].append(version)
    return sorted(groups.values(), key=lambda item: item["created"], reverse=True)


def detail(config, store, key):
    entry = job(config, key)
    meta = store.metadata()
    item = summarize(
        config,
        entry,
        meta,
        {
            "asr": lock_busy(config.state / "run.lock"),
            "text": lock_busy(config.state / "text-processing" / "run.lock"),
        },
    )
    raw = read_json(Path(entry["output"]) / "raw.json")
    from .diarization import rename_speakers, speaker_label, speaker_view

    raw = speaker_view(raw, Path(entry["output"]))
    names = meta.get(entry["audio_hash"], {}).get("speakers", {})
    from .markdown import simplified_text

    item["segments"] = [
        {
            "id": i,
            "start": s["start"],
            "end": s["end"],
            "text": simplified_text(s["text"]),
            "original_segment": s.get("original_segment", i),
            "speaker": s.get("speaker"),
            "speaker_label": rename_speakers(speaker_label(s), names),
            "overlap": s.get("overlap", False),
        }
        for i, s in enumerate(raw.get("segments", []), 1)
    ]
    counts = {}
    for s in raw.get("segments", []):
        if re.fullmatch(r"speaker_[1-9][0-9]{0,2}", str(s.get("speaker"))) and not s.get("overlap"):
            counts[s["speaker"]] = counts.get(s["speaker"], 0) + 1
    item["speakers"] = [
        {
            "id": key,
            "default": "说话人 " + key.split("_")[-1],
            "name": names.get(key, ""),
            "segments": counts[key],
        }
        for key in sorted(counts, key=lambda k: int(k.split("_")[-1]))
    ]
    item["source"] = entry["source"]
    item["output"] = entry["output"]
    item["model"] = raw.get("settings", {}).get("model")
    item["notes"] = meta.get(entry["audio_hash"], {}).get("notes", "")
    item["revision"] = meta.get(entry["audio_hash"], {}).get("revision", 0)
    try:
        item["audio_name"] = audio_path(config, key).name
    except FileNotFoundError:
        item["audio_name"] = None
    item["versions"] = [
        {"id": r["id"], "created": r["created"], "asr_status": r["status"]}
        for r in rows(config)
        if r["audio_hash"] == entry["audio_hash"]
    ]
    return item


def historical_artifact(config, key, name, version):
    if name not in ("polished.md", "polished.txt", "analysis.md") or not re.fullmatch(
        r"[0-9]{16,24}", version
    ):
        raise ValueError("历史文稿标识无效")
    directory = Path(job(config, key)["output"]).resolve()
    path = directory / "history" / version / name
    if not path.resolve().is_relative_to(directory) or path.is_symlink() or not path.is_file():
        raise FileNotFoundError("历史文稿不存在")
    return path


def compatible_audio(config, key):
    """Decode locally with the installed PyAV; cache a mono WAV for browser playback."""
    from .files import run_lock

    source = audio_path(config, key)
    directory = config.state / "workbench-preview" / job(config, key)["audio_hash"]
    with run_lock(directory, blocking=True):
        target = directory / "audio.wav"
        if target.is_file():
            return target
        import os
        import wave

        import av

        temporary = directory / ".audio.wav"
        try:
            with av.open(str(source)) as container, wave.open(str(temporary), "wb") as output:
                if not container.streams.audio:
                    raise ValueError("源文件不含可播放音轨")
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
                for frame in container.decode(container.streams.audio[0]):
                    for converted in resampler.resample(frame):
                        output.writeframes(converted.to_ndarray().tobytes())
                for converted in resampler.resample(None):
                    output.writeframes(converted.to_ndarray().tobytes())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target


def backlog(config):
    """Unregistered local inbox files, including files still settling; no content reads."""
    import time

    known = set()
    path = config.state / "jobs.sqlite3"
    if path.is_file():
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            known = {row[0] for row in db.execute("SELECT path FROM sources")}
        finally:
            db.close()
        from .store import deleted_records

        # Recordings deleted in the workbench keep their audio but are never transcribed again.
        known.update(p for paths in deleted_records(config.state).values() for p in paths)
    result = []
    candidates = (
        config.inbox.rglob("*") if config.processing["recursive"] else config.inbox.glob("*")
    )
    for file in candidates:
        if (
            file.is_symlink()
            or not file.is_file()
            or str(file) in known
            or file.suffix.lower().lstrip(".") not in config.processing["extensions"]
            or any(part.startswith(".") for part in file.relative_to(config.inbox).parts)
        ):
            continue
        try:
            stat = file.stat()
            origin = read_json(file.with_suffix(file.suffix + ".origin.json"))
            result.append(
                {
                    "name": origin.get("recording_title") or file.stem,
                    "size": stat.st_size,
                    "settling": stat.st_size == 0
                    or time.time() - stat.st_mtime < config.processing["settle_seconds"],
                }
            )
        except OSError:
            continue
    return sorted(result, key=lambda item: item["size"])
