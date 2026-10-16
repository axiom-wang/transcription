"""Offline pyannote CPU inference in its own process and dependency environment."""

import json
import os
import resource
import sys
import time
import traceback
from importlib.metadata import version
from pathlib import Path

from .control import activity
from .diarization import annotation_turns
from .files import atomic_json


def progress_hook(progress):
    def hook(step_name, step_artifact, file=None, total=None, completed=None):
        stages = {
            "segmentation": "检测声音与重叠发言",
            "embeddings": "比较说话人声音",
            "discrete_diarization": "对齐说话人时间段",
        }
        values = {}
        if total is not None and completed is not None:
            # pyannote may report numpy scalar counts, not native JSON integers.
            values = {
                "completed": min(int(completed), int(total)),
                "total": int(total),
                "unit": "批",
            }
        activity(progress, stages.get(step_name, "区分说话人"), **values)

    return hook


def process(request):
    os.environ.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        PYANNOTE_METRICS_ENABLED="0",
        OTEL_SDK_DISABLED="true",
        MPLBACKEND="Agg",
        PYTORCH_ENABLE_MPS_FALLBACK="1",
    )
    progress = request.get("progress_path")
    activity(progress, "加载说话人模型")
    import av
    import numpy as np
    import torch
    from pyannote.audio import Pipeline

    torch.set_num_threads(request["settings"]["cpu_threads"])
    torch.manual_seed(42)
    started = time.monotonic()
    pipeline = Pipeline.from_pretrained(request["model_dir"])
    if pipeline is None:
        raise ValueError("本地说话人模型加载失败")
    device = request["settings"].get("device", "cpu")
    if device == "auto":
        device = "mps" if torch.backends.mps.is_available() else "cpu"
    pipeline.to(torch.device(device))
    activity(progress, "解码音频 / 分析声音")
    # PyAV is bundled; supplying waveform avoids a system ffmpeg/torchcodec dependency.
    frames = []
    with av.open(request["source"]) as container:
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)
        for frame in container.decode(audio=0):
            frames.extend(part.to_ndarray().reshape(-1) for part in resampler.resample(frame))
        frames.extend(part.to_ndarray().reshape(-1) for part in resampler.resample(None))
    audio = np.concatenate(frames).astype(np.float32) if frames else np.zeros(0, dtype=np.float32)
    del frames
    if not len(audio):
        atomic_json(
            Path(request["result_path"]), {"turns": [], "exclusive_turns": [], "metrics": {}}
        )
        return
    waveform = torch.from_numpy(audio).unsqueeze(0)

    options = (
        {"num_speakers": request["settings"]["num_speakers"]}
        if request["settings"]["num_speakers"]
        else {}
    )
    with torch.inference_mode():
        output = pipeline(
            {"waveform": waveform, "sample_rate": 16000}, hook=progress_hook(progress), **options
        )
    turns, mapping = annotation_turns(output.speaker_diarization)
    exclusive, _ = annotation_turns(output.exclusive_speaker_diarization, mapping)
    atomic_json(
        Path(request["result_path"]),
        {
            "turns": turns,
            "exclusive_turns": exclusive,
            "metrics": {
                "elapsed_seconds": time.monotonic() - started,
            "device": device,
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "pyannote_audio": version("pyannote-audio"),
                "torch": version("torch"),
            },
        },
    )
    print(
        f"说话人区分完成，共 {len(mapping)} 人，用时 {time.monotonic() - started:.1f} 秒",
        flush=True,
    )


def main():
    try:
        process(json.loads(Path(sys.argv[1]).read_text()))
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
