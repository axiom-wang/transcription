import hashlib
import json
import logging
import math
import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from urllib.parse import quote

from .files import atomic_json, atomic_text, sha256_file
from .markdown import escape_inline, render
from .notifications import notify
from .store import Store
from .txt_exports import publish_txt

log = logging.getLogger("audio_notes")
ARTIFACTS = (
    "transcript.md",
    "notes.md",
    "transcript.srt",
    "transcript.txt",
    "transcript.simplified.md",
    "transcript.simplified.srt",
    "transcript.simplified.txt",
)


@dataclass
class Counts:
    completed: int = 0
    skipped: int = 0
    waiting: int = 0
    failed: int = 0


def load_raw(path, job_id, audio_hash, profile):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if (
            raw["schema_version"] != 1
            or raw["job_id"] != job_id
            or raw["audio_sha256"] != audio_hash
            or raw["profile"] != profile
        ):
            return None
        if not isinstance(raw["segments"], list) or not isinstance(raw["text"], str):
            return None
        if raw["text"] != "".join(s["text"] for s in raw["segments"]):
            return None
        if (
            not isinstance(raw["source"]["name"], str)
            or not Path(raw["source"]["path"]).is_absolute()
        ):
            return None
        if not isinstance(raw["info"]["language"], str) or not raw["settings"]["model"]:
            return None
        duration = raw["info"]["duration"]
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
            return None
        for s in raw["segments"]:
            if not all(
                isinstance(s[k], (float, int)) and math.isfinite(s[k]) for k in ("start", "end")
            ):
                return None
            if s["start"] < 0 or s["end"] < s["start"]:
                return None
        if not isinstance(raw["created_at"], str):
            return None
        return raw
    except (OSError, ValueError, KeyError, TypeError):
        return None


def export_fingerprint(options):
    return hashlib.sha256(
        json.dumps(
            {"renderer": 4, "options": options, "opencc": version("opencc")}, sort_keys=True
        ).encode()
    ).hexdigest()


def exports_valid(directory, options):
    try:
        receipt = json.loads((directory / "complete.json").read_text(encoding="utf-8"))
        speakers = directory / "speakers.json"
        return (
            receipt.get("speakers_sha256")
            == (sha256_file(speakers) if speakers.is_file() else None)
            and receipt["renderer"] == export_fingerprint(options)
        ) and all(
            receipt["sha256"][name] == sha256_file(directory / name)
            for name in ("raw.json", *ARTIFACTS)
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def write_exports(raw, directory, options):
    from .diarization import speaker_view

    render(speaker_view(raw, directory), directory, options)
    speakers = directory / "speakers.json"
    atomic_json(
        directory / "complete.json",
        {
            "renderer": export_fingerprint(options),
            "job_id": raw["job_id"],
            "speakers_sha256": sha256_file(speakers) if speakers.is_file() else None,
            "sha256": {name: sha256_file(directory / name) for name in ("raw.json", *ARTIFACTS)},
        },
    )


def stop_worker(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            process.wait()
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def run_worker(config, request, worker_log):
    request["progress_path"] = str(config.state / "activity" / (request["job_id"] + ".json"))
    from .control import activity

    activity(request["progress_path"], "等待模型资源")
    request_path = config.state / "requests" / (request["job_id"] + ".json")
    atomic_json(request_path, request)
    worker_log.parent.mkdir(parents=True, exist_ok=True)
    with worker_log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n--- 本次启动 {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        handle.flush()
        environment = dict(
            os.environ,
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            HF_HUB_DISABLE_TELEMETRY="1",
            PYTHONUNBUFFERED="1",
        )
        # Inherit the lock FD. If the parent is killed, a surviving worker still
        # prevents a second run from racing the same raw.json/model/database.
        process = subprocess.Popen(
            [sys.executable, "-m", "audio_notes.worker", str(request_path)],
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=environment,
            start_new_session=True,
            pass_fds=(config_lock_fd(),),
        )
        started = time.monotonic()
        try:
            while True:
                try:
                    limit = config.processing["timeout_seconds"]
                    remaining = limit - (time.monotonic() - started) if limit else 30
                    code = process.wait(timeout=max(0.001, min(30, remaining)))
                    break
                except subprocess.TimeoutExpired:
                    elapsed = time.monotonic() - started
                    limit = config.processing["timeout_seconds"]
                    if limit and elapsed >= limit:
                        stop_worker(process)
                        raise RuntimeError(f"单文件超时（{limit} 秒）；详见 {worker_log}")
                    log.info(
                        "仍在转写 %s（已用 %.0f 秒）；进度日志 %s",
                        Path(request["source"]).name,
                        elapsed,
                        worker_log,
                    )
        except BaseException:
            stop_worker(process)
            raise
    if code != 0:
        # Read a small tail instead of loading a potentially large progress log.
        with worker_log.open("rb") as handle:
            handle.seek(max(0, worker_log.stat().st_size - 2000))
            tail = handle.read().decode("utf-8", errors="replace").strip().splitlines()
        reason = tail[-1] if tail else "无详细错误"
        raise RuntimeError(f"转写进程退出 {code}: {reason}；完整日志 {worker_log}")


_lock_fd = None


def set_lock_fd(fd):
    global _lock_fd
    _lock_fd = fd


def config_lock_fd():
    if _lock_fd is None:
        raise RuntimeError("未持有运行锁")
    return _lock_fd


class Pipeline:
    def __init__(self, config, profile, worker=run_worker):
        self.config = config
        self.profile = profile
        self.worker = worker
        self.batch_failures = set()
        self.store = Store(config.state)
        recovered = self.store.recover()
        if recovered:
            log.info("发现 %d 个中断任务，将自动恢复", recovered)

    def close(self):
        self.store.close()

    def candidates(self):
        config = self.config
        extensions = {"." + ext.lower().lstrip(".") for ext in config.processing["extensions"]}
        paths = (
            config.inbox.rglob("*") if config.processing["recursive"] else config.inbox.glob("*")
        )
        # Skip symlinks and hidden directories, including macOS/iCloud temporary files.
        return sorted(
            [
                path
                for path in paths
                if not path.is_symlink()
                and path.is_file()
                and path.suffix.lower() in extensions
                and not any(part.startswith(".") for part in path.relative_to(config.inbox).parts)
                and path.resolve().is_relative_to(config.inbox)
            ],
            key=lambda path: (path.stat().st_size, str(path)),
        )

    def scan(self, watch=False, sources=None, followup_limit=True):
        from .control import paused, yield_to_workbench

        counts = Counts()
        seen = set()
        for source in self.candidates() if sources is None else sources:
            if paused(self.config) or (sources is None and yield_to_workbench(self.config)):
                counts.waiting = 1 if sources is not None else 0
                return counts
            job_id = None
            recording_name = source.stem
            try:
                if time.time() - source.stat().st_mtime < self.config.processing["settle_seconds"]:
                    counts.waiting += 1
                    log.info("等待文件同步完成：%s", source.name)
                    continue
                audio_hash = sha256_file(source)
                if audio_hash in self.store.deleted():
                    if sources is None:
                        counts.skipped += 1
                        continue
                    self.store.restore(audio_hash)
                job_id = hashlib.sha256(f"{audio_hash}:{self.profile}".encode()).hexdigest()
                if job_id in seen:
                    counts.skipped += 1
                    # Track all duplicate source paths for later audit.
                    self.store.register(
                        job_id, audio_hash, self.profile, source, self.config.output / job_id
                    )
                    continue
                seen.add(job_id)
                directory = self.config.output / job_id
                job = self.store.register(job_id, audio_hash, self.profile, source, directory)
                if not watch and job_id in self.batch_failures:
                    counts.skipped += 1
                    continue
                raw_path = directory / "raw.json"
                raw = load_raw(raw_path, job_id, audio_hash, self.profile)
                if raw:
                    recording_name = raw["source"]["name"]
                if raw and exports_valid(directory, self.config.markdown):
                    publish_txt(self.config, raw, directory)
                    if job["status"] != "completed":
                        self.store.mark(job_id, "completed")
                    counts.skipped += 1
                    log.info("跳过已完成：%s", source.name)
                    continue
                if (
                    watch
                    and job["status"] == "failed"
                    and time.time() - job["updated"] < self.config.processing["retry_seconds"]
                ):
                    counts.waiting += 1
                    continue
                self.store.mark(job_id, "running", attempt=raw is None)
                # Repairs of already completed exports should not repeat notifications.
                announce_completion = raw is None or job["status"] != "completed"
                if raw is None:
                    if raw_path.exists():
                        backup = directory / f"raw.invalid-{time.time_ns()}.json"
                        raw_path.rename(backup)
                        log.warning("原始缓存无效，已保留到 %s，将重新转写", backup)
                    from .knowledge import Knowledge
                    glossary = Knowledge(self.config.state).glossary()
                    request = {
                        "job_id": job_id,
                        "audio_hash": audio_hash,
                        "profile": self.profile,
                        "source": str(source),
                        "model_dir": str(self.config.model_dir),
                        "settings": {**self.config.whisper, 'initial_prompt':
                            (self.config.whisper.get('initial_prompt', '') + '\n' + glossary).strip()},
                        "raw_path": str(raw_path),
                    }
                    origin_path = source.with_suffix(source.suffix + ".origin.json")
                    if origin_path.is_file():
                        try:
                            origin = json.loads(origin_path.read_text(encoding="utf-8"))
                            if (
                                origin.get("audio_sha256") == audio_hash
                                and Path(origin["original_path"]).is_absolute()
                            ):
                                request["import_origin"] = origin
                                title = origin.get("recording_title")
                                if isinstance(title, str) and title.strip():
                                    recording_name = title
                        except (OSError, ValueError, KeyError, TypeError, AttributeError):
                            log.warning("导入来源记录无效，仍将转写音频：%s", origin_path)
                    log.info("开始本地转写：%s", source.name)
                    asr_started = time.monotonic()
                    self.worker(self.config, request, self.config.state / "logs" / f"{job_id}.log")
                    raw = load_raw(raw_path, job_id, audio_hash, self.profile)
                    if raw is None:
                        raise RuntimeError("转写进程没有生成有效原始结果")
                    from .estimates import record
                    record(self.config, job_id, 'asr', raw['info'].get('duration'), time.monotonic() - asr_started)
                else:
                    log.info("利用原始转写缓存重新生成文档：%s", source.name)
                write_exports(raw, directory, self.config.markdown)
                publish_txt(self.config, raw, directory)
                self.store.mark(job_id, "completed")
                self.write_index()
                counts.completed += 1
                log.info("完成：%s → %s", source.name, directory)
                from .knowledge import Knowledge
                if announce_completion and (Knowledge(self.config.state).preferences()['notification'] == 'stages' or not self.config.analysis.get('enabled')):
                    notify(
                        self.config,
                        "原文已可读",
                        f"{recording_name}\n可以先阅读原文，智能稿将在完成后通知。",
                    )
            except KeyboardInterrupt:
                if job_id:
                    self.store.mark(job_id, "interrupted", "用户停止；重新运行将恢复")
                raise
            except Exception as exc:
                counts.failed += 1
                if job_id:
                    self.store.mark(job_id, "failed", str(exc))
                    self.batch_failures.add(job_id)
                log.exception("文件处理失败：%s；%s", source, exc)
                notify(
                    self.config,
                    "转写失败",
                    f"{recording_name}\n请查看项目状态或 state/logs/workflow.log。",
                )
        self.write_index()
        if self.config.speakers.get("enabled"):
            from .diarization import diarize_completed

            jobs = self.store.completed()
            if sources is not None:
                selected = {str(p) for p in sources}
                ids = {
                    row["job_id"]
                    for row in self.store.db.execute("SELECT job_id,path FROM sources")
                    if row["path"] in selected
                }
                jobs = [job for job in jobs if job["id"] in ids]
            diarize_completed(
                self.config,
                jobs,
                limit=(
                    self.config.speakers["max_jobs_per_scan"]
                    if sources is None and followup_limit
                    else None
                ),
            )
            self.write_index()
        if self.config.analysis.get("enabled"):
            from .enrichment import enrich_completed

            jobs = self.store.completed()
            if sources is not None:
                selected = {str(p) for p in sources}
                ids = {
                    row["job_id"]
                    for row in self.store.db.execute("SELECT job_id,path FROM sources")
                    if row["path"] in selected
                }
                jobs = [job for job in jobs if job["id"] in ids]
            enrich_completed(
                self.config,
                jobs,
                limit=(
                    self.config.analysis["max_jobs_per_scan"]
                    if sources is None and followup_limit
                    else None
                ),
            )
            self.write_index()
        log.info(
            "本轮完成 %d，跳过 %d，等待 %d，失败 %d",
            counts.completed,
            counts.skipped,
            counts.waiting,
            counts.failed,
        )
        return counts

    def write_index(self):
        write_index(self.config, self.store.completed())


def write_index(config, completed):
    txt_link = quote(os.path.relpath(config.txt_output, config.output)) + "/"
    text = (
        "# 录音文档索引\n\n阅读版与简体完整稿保留全部识别内容，统一转换为简体；"
        "模型原始稿另行保留。优化交付稿与分析稿由本机文本模型生成，可通过引用核对原文。\n\n"
        f"[打开转写TXT文件夹]({txt_link})：简体纯文本按录音名称汇集。\n\n"
    )
    for job in completed:
        directory = Path(job["output"])
        if directory.parent != config.output or not (directory / "complete.json").is_file():
            continue
        label = escape_inline(Path(job["source"]).stem)
        origin = None
        origin_path = Path(job["source"]).with_suffix(Path(job["source"]).suffix + ".origin.json")
        if origin_path.is_file():
            try:
                origin = json.loads(origin_path.read_text(encoding="utf-8"))
                label = escape_inline(origin.get("recording_title") or Path(job["source"]).stem)
            except (OSError, ValueError, TypeError):
                origin = None
        from .titles import display_name
        from .workbench_store import read_metadata

        named = read_metadata(config.state, job["audio_hash"]).get("title") or display_name(
            directory, origin=origin
        )
        if named:
            label = escape_inline(named)
        full_name = (
            "transcript.simplified.md"
            if (directory / "transcript.simplified.md").is_file()
            else "transcript.md"
        )
        delivery = ""
        speaker_status = ""
        if config.speakers.get("enabled"):
            from .control import read_json

            stage = read_json(directory / "speakers.status.json").get("status", "pending")
            speaker_status = {
                "completed": "已区分说话人 · ",
                "running": "正在区分说话人 · ",
                "blocked": "说话人模型待准备 · ",
                "failed": "说话人区分待重试 · ",
            }.get(stage, "说话人区分待处理 · ")
        if (directory / "enrichment.complete.json").is_file():
            try:
                receipt = json.loads((directory / "enrichment.complete.json").read_text())
                report_label = (
                    "分析稿（待核对）"
                    if receipt.get("quality_status") == "needs_review"
                    else "分析稿"
                )
                speaker_path = directory / "speakers.json"
                if speaker_path.is_file() and receipt.get("speakers_sha256") != sha256_file(
                    speaker_path
                ):
                    report_label = "分析稿（说话人信息待更新）"
            except (OSError, ValueError):
                report_label = "分析稿"
            delivery = (
                f"[{report_label}]({directory.name}/analysis.md) · "
                f"[转写交付稿]({directory.name}/polished.md) · "
                f"[交付TXT]({directory.name}/polished.txt) · "
            )
        elif config.analysis.get("enabled"):
            try:
                stage = json.loads((directory / "enrichment.status.json").read_text())["status"]
            except (OSError, ValueError, KeyError):
                stage = "pending"
            delivery = {
                "running": "文稿优化与分析中 · ",
                "failed": "文稿优化与分析待重试（原稿可读） · ",
            }.get(stage, "文稿优化与分析待处理 · ")
        text += (
            f"- **{label}** · "
            + speaker_status
            + delivery
            + f"[忠实阅读版]({directory.name}/notes.md) · [完整稿]({directory.name}/{full_name}) · "
            f"[模型原始稿]({directory.name}/transcript.md) · "
            f"[原始结果]({directory.name}/raw.json)\n"
        )
    path = config.output / "index.md"
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        atomic_text(path, text)


def refresh_index(config):
    path = config.state / "jobs.sqlite3"
    if not path.is_file():
        return
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        jobs = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM jobs WHERE status='completed' ORDER BY created DESC"
            )
        ]
    write_index(config, jobs)
