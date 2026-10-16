"""Offline real-model check with two macOS system voices; no private recordings."""

import json
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import av
import numpy as np

from audio_notes.config import load_config
from audio_notes.diarization import check_speaker_model, diarize_completed, speaker_view
from audio_notes.files import atomic_json, atomic_text, run_lock, sha256_file
from audio_notes.pipeline import set_lock_fd, write_exports

root = Path(__file__).resolve().parents[1]
base = root / "verification" / "speakers"
base.mkdir(parents=True, exist_ok=True)
texts = [
    (
        "Tingting",
        "我们今天讨论项目安排。我建议先完成录音整理，再检查说话人的区分结果。上线时间还没有确定，需要明天再讨论。",
    ),
    (
        "Daniel",
        "I agree. Let us verify the transcript carefully before making a decision. We should keep the original recording and review the action items tomorrow.",
    ),
]
for i, (voice, text) in enumerate(texts, 1):
    path = base / f"voice-{i}.aiff"
    if not path.is_file():
        subprocess.run(["/usr/bin/say", "-v", voice, "-o", str(path), text], check=True)


def decode(path):
    pieces = []
    with av.open(str(path)) as c:
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)
        for frame in c.decode(audio=0):
            pieces.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(frame))
        pieces.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(None))
    return np.concatenate(pieces)


voices = [decode(base / f"voice-{i}.aiff") for i in (1, 2)]
samples, segments, expected = [], [], []
offset = 0
for i, which in enumerate((0, 1, 0, 1)):
    wave = voices[which]
    samples.append(wave)
    duration = len(wave) / 16000
    segments.append({"start": offset, "end": offset + duration, "text": texts[which][1]})
    expected.append({"start": offset + 0.5, "end": offset + duration - 0.5, "voice": which})
    offset += duration
    samples.append(np.zeros(16000, dtype=np.float32))
    offset += 1
samples = np.concatenate(samples)
work = base / f"offline-{time.time_ns()}"
config = load_config(root / "config.toml")
config = replace(
    config,
    inbox=work / "inbox",
    output=work / "output",
    state=work / "state",
    analysis={**config.analysis, "enabled": False},
    speakers={**config.speakers, "enabled": True},
    notifications={"enabled": False},
)
config.ensure_dirs()
source = config.inbox / "双人交替.wav"
with av.open(str(source), "w") as c:
    stream = c.add_stream("pcm_s16le", rate=16000)
    stream.layout = "mono"
    audio = av.AudioFrame.from_ndarray(
        (samples * 32767).astype(np.int16).reshape(1, -1), format="s16", layout="mono"
    )
    audio.sample_rate = 16000
    for packet in stream.encode(audio):
        c.mux(packet)
    for packet in stream.encode():
        c.mux(packet)
key = "c" * 64
directory = config.output / key
raw = {
    "schema_version": 1,
    "job_id": key,
    "profile": "synthetic",
    "created_at": "2026-10-16T00:00:00Z",
    "source": {"name": "双人交替离线验证", "path": str(source)},
    "audio_sha256": sha256_file(source),
    "settings": {"model": "synthetic-ground-truth"},
    "info": {"language": "mixed", "duration": offset},
    "segments": segments,
    "text": "".join(s["text"] for s in segments),
}
atomic_json(directory / "raw.json", raw)
write_exports(raw, directory, config.markdown)
check_speaker_model(config, verify=True)
original = sha256_file(directory / "raw.json")
job = {"id": key, "source": str(source), "output": str(directory)}
with run_lock(config.state) as lock:
    set_lock_fd(lock.fileno())
    assert diarize_completed(config, [job], retry_now=True) == 1
    assert diarize_completed(config, [job]) == 0
view = speaker_view(raw, directory)
data = json.loads((directory / "speakers.json").read_text())
labels = []
for span in expected:
    coverage = {}
    for t in data["exclusive_turns"]:
        overlap = max(0, min(t["end"], span["end"]) - max(t["start"], span["start"]))
        coverage[t["speaker"]] = coverage.get(t["speaker"], 0) + overlap
    labels.append(max(coverage, key=coverage.get))
assert labels[0] == labels[2] and labels[1] == labels[3] and labels[0] != labels[1], labels
assert sha256_file(directory / "raw.json") == original
assert "".join(s["text"] for s in view["segments"]) == raw["text"]
report = {
    "passed": True,
    "audio_seconds": offset,
    "speaker_sequence": labels,
    "raw_unchanged": True,
    "text_preserved": True,
    "cache_reused": True,
    "metrics": data["metrics"],
    "output": str(directory),
}
atomic_json(work / "report.json", report)
atomic_text(base / "latest.txt", str(work / "report.json") + "\n")
print(json.dumps(report, ensure_ascii=False, indent=2))
