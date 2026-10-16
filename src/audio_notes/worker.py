"""One independent process per recording; importing this module never loads ASR."""

import json
import math
import os
import sys
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from .control import activity
from .files import atomic_json, model_lock, sha256_file


def json_safe(value):
    """Keep internal infinity/NaN settings losslessly in strict JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"$float": repr(value)}
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_safe(item) for item in value]
    return value


def transcribe_whisper(request):
    from faster_whisper import WhisperModel

    settings = request["settings"]
    source = Path(request["source"])
    activity(request.get("progress_path"), "加载语音模型")
    print(f"加载本地模型：{request['model_dir']}", flush=True)
    model = WhisperModel(
        request["model_dir"],
        device="cpu",
        compute_type=settings["compute_type"],
        cpu_threads=settings["cpu_threads"],
        local_files_only=True,
    )
    print(f"解码音频：{source}", flush=True)
    segments, info = model.transcribe(
        str(source),
        task="transcribe",
        language=None if settings["language"] == "auto" else settings["language"],
        beam_size=settings["beam_size"],
        word_timestamps=settings["word_timestamps"],
        vad_filter=settings["vad_filter"],
        condition_on_previous_text=settings["condition_on_previous_text"],
        initial_prompt=settings["initial_prompt"] or None,
    )
    saved = []
    for segment in segments:
        saved.append(asdict(segment))
        activity(request.get("progress_path"), "识别音频", completed=segment.end, total=info.duration, unit="秒")
        print(f"识别进度：{segment.end:.1f} / {info.duration:.1f} 秒", flush=True)
    engine = {
        "name": "faster-whisper",
        "version": version("faster-whisper"),
        "ctranslate2_version": version("ctranslate2"),
        "av_version": version("av"),
        "device": "cpu",
    }
    return saved, asdict(info), engine, None


def transcribe(request):
    with model_lock(Path(request["model_dir"]).parent):
        return transcribe_locked(request)


def transcribe_locked(request):
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    settings = request["settings"]
    source = Path(request["source"])
    if settings["model"] == "qwen3-asr-1.7b":
        from .qwen import transcribe_local

        print(f"加载本地 Qwen3-ASR：{request['model_dir']}", flush=True)
        activity(request.get("progress_path"), "加载语音模型 / 解码音频")
        saved, info, engine, native = transcribe_local(
            source, request["model_dir"], settings,
            on_progress=lambda done, total: activity(
                request.get("progress_path"), "识别音频", completed=done, total=total, unit="秒"
            ),
        )
    else:
        saved, info, engine, native = transcribe_whisper(request)
    if sha256_file(source) != request["audio_hash"]:
        raise RuntimeError("音频在转写期间改变；此次结果未保存，等同步完成后重试")
    engine["model_manifest"] = json.loads(
        (Path(request["model_dir"]) / "local-model.json").read_text(encoding="utf-8")
    )
    raw = {
        "schema_version": 1,
        "job_id": request["job_id"],
        "audio_sha256": request["audio_hash"],
        "profile": request["profile"],
        "source": {"path": str(source), "name": source.stem},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "settings": settings,
        "engine": engine,
        "info": info,
        "segments": saved,
        "text": "".join(s["text"] for s in saved),
    }
    if native is not None:
        raw["native_transcription"] = native
    if request.get("import_origin"):
        origin = request["import_origin"]
        raw["import_origin"] = origin
        if isinstance(origin.get("recording_title"), str) and origin["recording_title"].strip():
            raw["source"]["name"] = origin["recording_title"]
    activity(request.get("progress_path"), "保存转写原文")
    atomic_json(Path(request["raw_path"]), json_safe(raw))
    print(f"原始转写已保存：{request['raw_path']}", flush=True)


def main():
    try:
        transcribe(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
