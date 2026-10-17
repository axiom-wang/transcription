"""Local speaker turns stored independently of immutable ASR data."""

import hashlib
import json
import logging
import math
import os
import re
import subprocess
import time
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from pathlib import Path

from .control import activity, read_json
from .files import atomic_json, model_lock, sha256_file

log = logging.getLogger("audio_notes")
REPO = "pyannote/speaker-diarization-community-1"
REVISION = "3533c8cf8e369892e6b79ff1bf80f7b0286a54ee"
MODEL_FILES = (
    "config.yaml",
    "embedding/pytorch_model.bin",
    "segmentation/pytorch_model.bin",
    "plda/plda.npz",
    "plda/xvec_transform.npz",
)
SCHEMA = 1
VIEW_SCHEMA = 3


def check_speaker_model(config, verify=False):
    directory = config.speaker_model_dir
    try:
        manifest = json.loads((directory / "local-model.json").read_text())
        if (
            manifest.get("repository") != REPO
            or manifest.get("revision") != REVISION
            or set(manifest.get("files", {})) != set(MODEL_FILES)
        ):
            raise ValueError("模型清单不匹配")
        for name, info in manifest["files"].items():
            path = directory / name
            if path.stat().st_size != info["size"] or (
                verify and sha256_file(path) != info["sha256"]
            ):
                raise ValueError(f"模型文件校验失败：{name}")
        if not config.speaker_python.is_file():
            raise ValueError("推理环境不存在")
        return manifest
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(
            "说话人模型未就绪，请运行 ./setup-speakers.sh；首次下载需登录并接受官方条款"
        ) from exc


def download_speaker_model(config, refresh=False, authorize=False):
    if not refresh:
        try:
            check_speaker_model(config, verify=True)
            print("本地说话人模型完整，无需下载。")
            return
        except ValueError:
            pass
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import GatedRepoError

    try:
        if authorize:
            environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            command = [
                str(config.speaker_python),
                "-m",
                "audio_notes.speaker_download",
                str(config.speaker_model_dir),
            ]
            if refresh:
                command.append("--refresh")
            subprocess.run(
                command,
                env=environment,
                check=True,
            )
        else:
            snapshot_download(
                REPO,
                revision=REVISION,
                local_dir=config.speaker_model_dir,
                allow_patterns=[*MODEL_FILES, "README.md", "*/README.md"],
                max_workers=2,
                force_download=refresh,
            )
    except GatedRepoError as exc:
        raise ValueError(
            f"官方模型访问未授权：请在 https://huggingface.co/{REPO} 接受条款，"
            "再运行 ./setup-speakers.sh --authorize 临时授权公开模型下载；无需把令牌写入配置或聊天"
        ) from exc
    manifest = {
        "repository": REPO,
        "revision": REVISION,
        "files": {
            name: {
                "sha256": sha256_file(config.speaker_model_dir / name),
                "size": (config.speaker_model_dir / name).stat().st_size,
            }
            for name in MODEL_FILES
        },
    }
    atomic_json(config.speaker_model_dir / "local-model.json", manifest)
    check_speaker_model(config, verify=True)
    print(f"说话人模型已准备好：{config.speaker_model_dir}")


SPEAKER_TEXT = re.compile(r"说话人\s?([1-9][0-9]{0,2})(?![0-9])")


def rename_speakers(text, names):
    """Apply the user's names to every automatic speaker label in a rendered text."""
    if not names:
        return text
    return SPEAKER_TEXT.sub(lambda m: names.get(f"speaker_{m.group(1)}", m.group(0)), text)


def speaker_label(segment):
    if "speaker" not in segment:
        return ""
    if segment.get("overlap"):
        return "重叠发言（归属待确认）"
    speaker = segment.get("speaker")
    if isinstance(speaker, str) and re.fullmatch(r"speaker_[1-9][0-9]*", speaker):
        return "说话人 " + speaker.split("_")[-1]
    return "说话人待确认"


def validate_turns(turns, duration, exclusive=False):
    if not isinstance(turns, list):
        raise ValueError("说话人时间段必须是列表")
    last_start = last_end = -1
    for t in turns:
        if not all(type(t[k]) in (int, float) and math.isfinite(t[k]) for k in ("start", "end")):
            raise ValueError("说话人时间戳无效")
        if not (0 <= t["start"] < t["end"] <= duration + 0.1) or t["start"] < last_start:
            raise ValueError("说话人时间段越界或顺序错误")
        if exclusive and t["start"] < last_end - 0.001:
            raise ValueError("单一归属时间段存在重叠")
        if not isinstance(t["speaker"], str) or not re.fullmatch(
            r"speaker_[1-9][0-9]*", t["speaker"]
        ):
            raise ValueError("说话人编号无效")
        last_start, last_end = t["start"], t["end"]
    return turns


def annotation_turns(annotation, mapping=None):
    mapping = {} if mapping is None else mapping
    turns = []
    for turn, _, name in annotation.itertracks(yield_label=True):
        mapping.setdefault(name, f"speaker_{len(mapping) + 1}")
        turns.append({"start": float(turn.start), "end": float(turn.end), "speaker": mapping[name]})
    return sorted(turns, key=lambda t: (t["start"], t["end"], t["speaker"])), mapping


def _intersection(start, end, turn):
    return max(0, min(end, turn["end"]) - max(start, turn["start"]))


class TurnIndex:
    """Find only turns near a word, including nested/overlapping speech intervals."""

    def __init__(self, turns):
        self.turns = turns
        self.starts = [turn["start"] for turn in turns]
        self.ends = []
        end = -1
        for turn in turns:
            end = max(end, turn["end"])
            self.ends.append(end)

    def overlapping(self, start, end):
        left = bisect_right(self.ends, start)
        right = bisect_left(self.starts, end)
        return self.turns[left:right]


def _assignment(start, end, exclusive, regular):
    """Temporal coverage is an alignment heuristic, never a model confidence score."""
    duration = end - start
    if duration < 0:
        return None, False
    point = duration == 0
    if point:
        # Forced alignment occasionally gives a real word a zero-length timestamp.
        # Keep that text, and only assign it when its immediate neighborhood is unambiguous.
        start, end = max(0, start - 0.04), end + 0.04
        duration = end - start
    overlap = False
    relevant = [t for t in regular.overlapping(start, end) if _intersection(start, end, t) > 0]
    for i, a in enumerate(relevant):
        for b in relevant[i + 1 :]:
            if a["speaker"] != b["speaker"]:
                simultaneous = max(
                    0, min(end, a["end"], b["end"]) - max(start, a["start"], b["start"])
                )
                if simultaneous >= min(0.05, duration * 0.2):
                    overlap = True
    scores = {}
    for t in exclusive.overlapping(start, end):
        weight = _intersection(start, end, t)
        if weight:
            scores[t["speaker"]] = scores.get(t["speaker"], 0) + weight
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if point and len(ranked) != 1:
        return None, overlap
    if not ranked or ranked[0][1] / duration < 0.6:
        return None, overlap
    # Exclusive turns already pick the dominant voice inside overlapping speech, so a
    # brief back-channel under the main speaker keeps that speaker. Only overlap the
    # exclusive turns cannot settle stays unattributed.
    return ranked[0][0], False


def assign_segments(segments, exclusive, regular):
    """Split on aligned words only; preserve every source character exactly once."""
    result = []
    exclusive, regular = TurnIndex(exclusive), TurnIndex(regular)
    for sid, segment in enumerate(segments, 1):
        text = segment["text"]
        words = segment.get("words") or []
        boundaries, offset = [], 0
        for word in words:
            token = word.get("word", "").strip()
            at = text.find(token, offset) if token else -1
            # Do not hide unaligned words in the gap between aligned tokens.
            if at < 0 or any(c.isalnum() for c in text[offset:at]):
                boundaries = []
                break
            if not (segment["start"] <= word["start"] <= word["end"] <= segment["end"]):
                boundaries = []
                break
            boundaries.append((offset if not boundaries else at, word))
            offset = at + len(token)
        if any(c.isalnum() for c in text[offset:]):
            boundaries = []
        if not boundaries:
            speaker, overlap = _assignment(segment["start"], segment["end"], exclusive, regular)
            # A segment crossing several people must not be attributed to its majority speaker.
            people = {
                t["speaker"]
                for t in exclusive.overlapping(segment["start"], segment["end"])
                if _intersection(segment["start"], segment["end"], t) > 0.02
            }
            if len(people) > 1:
                speaker = None
            result.append(
                {**segment, "original_segment": sid, "speaker": speaker, "overlap": overlap}
            )
            continue
        pieces = []
        for i, (at, word) in enumerate(boundaries):
            stop = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
            speaker, overlap = _assignment(word["start"], word["end"], exclusive, regular)
            part = {
                **segment,
                "text": text[at:stop],
                "start": word["start"],
                "end": word["end"],
                "words": [word],
                "original_segment": sid,
                "speaker": speaker,
                "overlap": overlap,
            }
            if pieces and (pieces[-1]["speaker"], pieces[-1]["overlap"]) == (speaker, overlap):
                pieces[-1]["text"] += part["text"]
                pieces[-1]["end"] = part["end"]
                pieces[-1]["words"].append(word)
            else:
                pieces.append(part)
        if "".join(p["text"] for p in pieces) != text:
            raise ValueError("说话人对齐改变了原始文字，拒绝发布")
        result.extend(pieces)
    for i, segment in enumerate(result):
        segment["id"] = i
    if "".join(s["text"] for s in result) != "".join(s["text"] for s in segments):
        raise ValueError("说话人对齐未保留全部原文")
    return result


def speaker_view(raw, directory):
    """Read a verified sidecar. Bad or unrelated sidecars never affect ASR."""
    data = read_json(Path(directory) / "speakers.json")
    if not data:
        return raw
    try:
        if (
            data["schema"] != SCHEMA
            or data["audio_sha256"] != raw["audio_sha256"]
            or data["raw_sha256"] != sha256_file(Path(directory) / "raw.json")
            or data["repository"] != REPO
            or data["revision"] != REVISION
        ):
            return raw
        validate_turns(data["turns"], raw["info"]["duration"])
        validate_turns(data["exclusive_turns"], raw["info"]["duration"], exclusive=True)
        return {
            **raw,
            "segments": assign_segments(raw["segments"], data["exclusive_turns"], data["turns"]),
            "diarization": {"repository": REPO, "revision": REVISION},
        }
    except (OSError, ValueError, KeyError, TypeError):
        return raw


def speaker_fingerprint(config, raw_path, manifest):
    return hashlib.sha256(
        json.dumps(
            {
                "schema": SCHEMA,
                "raw": sha256_file(raw_path),
                "model": manifest,
                "num_speakers": config.speakers["num_speakers"],
                "device": config.speakers.get("device", "cpu"),
                "runtime": (
                    config.speaker_python.parents[2] / "requirements-speakers.lock"
                ).read_text(),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def run_speaker_worker(config, request, worker_log):
    from .pipeline import config_lock_fd, stop_worker

    request_path = config.state / "requests" / f"speakers-{request['job_id']}.json"
    request["progress_path"] = str(config.state / "activity" / f"speakers-{request['job_id']}.json")
    activity(request["progress_path"], "等待模型资源")
    worker_log.parent.mkdir(parents=True, exist_ok=True)
    with model_lock(config.models) as resource, worker_log.open("a") as log_handle:
        atomic_json(request_path, request)
        environment = dict(
            os.environ,
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            HF_HUB_DISABLE_TELEMETRY="1",
            PYANNOTE_METRICS_ENABLED="0",
            OTEL_SDK_DISABLED="true",
            MPLBACKEND="Agg",
            PYTHONUNBUFFERED="1",
            PYTHONPATH=str(Path(__file__).resolve().parents[1]),
        )
        process = subprocess.Popen(
            [str(config.speaker_python), "-m", "audio_notes.speaker_worker", str(request_path)],
            env=environment,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            pass_fds=(config_lock_fd(), resource.fileno()),
        )
        started = time.monotonic()
        try:
            while True:
                remaining = config.speakers["timeout_seconds"] - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("说话人区分超时，原始转写仍可使用")
                try:
                    code = process.wait(timeout=min(30, remaining))
                    break
                except subprocess.TimeoutExpired:
                    log.info("仍在本机区分说话人（已用 %.0f 秒）", time.monotonic() - started)
        except BaseException:
            stop_worker(process)
            raise
    if code:
        raise RuntimeError(f"说话人识别失败；详见 {worker_log}")


def diarize_one(config, job, manifest, force=False, worker=run_speaker_worker):
    directory = Path(job["output"])
    raw_path = directory / "raw.json"
    raw = json.loads(raw_path.read_text())
    key = speaker_fingerprint(config, raw_path, manifest)
    cached = read_json(directory / "speakers.json")
    if (
        not force
        and cached.get("fingerprint") == key
        and "diarization" in speaker_view(raw, directory)
    ):
        return False
    source = Path(job["source"])
    if not source.is_file() or sha256_file(source) != raw["audio_sha256"]:
        raise ValueError("原音频不可访问或已改变；未修改原始转写")
    work = directory / "speaker-processing" / f"{key}-{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    request = {
        "job_id": raw["job_id"],
        "source": str(source),
        "model_dir": str(config.speaker_model_dir),
        "settings": config.speakers,
        "result_path": str(work / "turns.json"),
    }
    worker(config, request, config.state / "logs" / f"speakers-{raw['job_id']}.log")
    data = json.loads((work / "turns.json").read_text())
    validate_turns(data["turns"], raw["info"]["duration"])
    validate_turns(data["exclusive_turns"], raw["info"]["duration"], exclusive=True)
    if (
        speaker_fingerprint(config, raw_path, manifest) != key
        or sha256_file(source) != raw["audio_sha256"]
    ):
        raise ValueError("处理期间原文或音频改变，未发布说话人结果")
    result = {
        **data,
        "embeddings": data.get("embeddings", {}),
        "schema": SCHEMA,
        "repository": REPO,
        "revision": REVISION,
        "fingerprint": key,
        "audio_sha256": raw["audio_sha256"],
        "raw_sha256": sha256_file(raw_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    old = directory / "speakers.json"
    if old.is_file():
        atomic_json(work / "previous-speakers.json", read_json(old))
    # Validate exact text preservation before publishing even the sidecar.
    assign_segments(raw["segments"], data["exclusive_turns"], data["turns"])
    atomic_json(old, result)
    from .estimates import record
    from .knowledge_api import match_voices
    match_voices(config, raw['job_id'], raw['audio_sha256'], result.get('embeddings', {}))
    record(config, raw['job_id'], 'speaker', raw['info'].get('duration'), result.get('metrics', {}).get('elapsed_seconds', 0))
    return True


def diarize_completed(
    config, jobs, limit=None, force=False, retry_now=False, worker=run_speaker_worker
):
    """Caller holds the ASR run lock; failures remain independent of ASR success."""
    from .control import paused, yield_to_workbench
    from .pipeline import write_exports
    from .txt_exports import publish_txt

    if not config.speakers.get("enabled"):
        return 0
    try:
        manifest = check_speaker_model(config)
    except ValueError as exc:
        for job in jobs:
            directory = Path(job["output"])
            if directory.parent != config.output:
                continue
            raw = read_json(directory / "raw.json")
            if "diarization" in speaker_view(raw, directory):
                continue
            atomic_json(
                Path(job["output"]) / "speakers.status.json",
                {"status": "blocked", "error": str(exc), "updated": time.time()},
            )
        log.warning("%s；原始转写可继续使用", exc)
        return 0
    completed = attempted = 0
    for job in jobs:
        if paused(config) or (limit is not None and yield_to_workbench(config)):
            break
        directory = Path(job["output"])
        if directory.parent != config.output or not (directory / "complete.json").is_file():
            continue
        status_path = directory / "speakers.status.json"
        status = read_json(status_path)
        key = speaker_fingerprint(config, directory / "raw.json", manifest)
        raw = json.loads((directory / "raw.json").read_text())
        valid = read_json(directory / "speakers.json").get(
            "fingerprint"
        ) == key and "diarization" in speaker_view(raw, directory)
        if not force and valid:
            continue
        if (
            not force
            and not retry_now
            and status.get("status") == "failed"
            and status.get("fingerprint") == key
            and time.time() - status.get("updated", 0) < config.speakers["retry_seconds"]
        ):
            continue
        if limit is not None and attempted >= limit:
            break
        attempted += 1
        atomic_json(status_path, {"status": "running", "fingerprint": key, "updated": time.time()})
        try:
            changed = diarize_one(config, job, manifest, force=force, worker=worker)
            write_exports(raw, directory, config.markdown)
            publish_txt(config, raw, directory)
            completed += int(changed)
            atomic_json(
                status_path, {"status": "completed", "fingerprint": key, "updated": time.time()}
            )
        except Exception as exc:
            atomic_json(
                status_path,
                {"status": "failed", "fingerprint": key, "error": str(exc), "updated": time.time()},
            )
            log.exception("说话人区分失败，完整转写仍保留：%s", directory.name)
    return completed
