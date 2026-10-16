"""Verify genuine Qwen inference with network access denied and isolated test media."""

import json
import shutil
import sqlite3
import subprocess
import time
import wave
from pathlib import Path

from audio_notes.config import load_config
from audio_notes.files import atomic_json, sha256_file
from audio_notes.pipeline import exports_valid

ROOT = Path(__file__).resolve().parents[1]
POLICY = "(version 1)(allow default)(deny network*)"


def main():
    folder = ROOT / "verification" / f"qwen-offline-{time.time_ns()}"
    inbox = folder / "inbox"
    inbox.mkdir(parents=True)
    # These are generated fixtures only; no private recordings are used.
    subprocess.run(
        [
            "/usr/bin/say",
            "-v",
            "Tingting",
            "-r",
            "170",
            "-o",
            str(folder / "sample.aiff"),
            "今天测试本地音频转写。这个项目使用 Python 和 Markdown。最后一句用于检查有没有漏掉结尾。",
        ],
        check=True,
    )
    import av

    for extension in ("wav", "m4a", "mp3", "mov", "mp4"):
        codec, fmt = (
            ("pcm_s16le", "s16")
            if extension == "wav"
            else (("libmp3lame", "fltp") if extension == "mp3" else ("aac", "fltp"))
        )
        with (
            av.open(str(folder / "sample.aiff")) as incoming,
            av.open(str(inbox / f"中文与English.{extension}"), "w") as outgoing,
        ):
            stream = outgoing.add_stream(codec, rate=16000)
            stream.layout = "mono"
            resampler = av.AudioResampler(format=fmt, layout="mono", rate=16000)
            for frame in incoming.decode(audio=0):
                for converted in resampler.resample(frame):
                    for packet in stream.encode(converted):
                        outgoing.mux(packet)
            for converted in resampler.resample(None):
                for packet in stream.encode(converted):
                    outgoing.mux(packet)
            for packet in stream.encode(None):
                outgoing.mux(packet)
    # A valid empty QuickTime 'free' atom makes this distinct from the M4A,
    # so QTA exercises decoding instead of being satisfied by the dedup cache.
    (inbox / "语音备忘录.qta").write_bytes(
        (inbox / "中文与English.m4a").read_bytes() + b"\x00\x00\x00\x08free"
    )
    shutil.copy2(inbox / "中文与English.wav", inbox / "重复副本.wav")
    with wave.open(str(inbox / "中文与English.wav"), "rb") as source:
        params = source.getparams()
        frames = source.readframes(source.getnframes())
    with wave.open(str(inbox / "跨段长录音.wav"), "wb") as target:
        target.setparams(params)
        target.writeframes(frames * 5)
    with wave.open(str(inbox / "纯静音.wav"), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(b"\0\0" * 48000)
    original = {str(p): (sha256_file(p), p.stat().st_mtime_ns) for p in inbox.iterdir()}
    text = (ROOT / "config.toml").read_text()
    text = text.replace("enabled = true", "enabled = false")
    text = text.replace('models = "models"', 'models = "../../models"')
    text = text.replace("settle_seconds = 30", "settle_seconds = 0")
    config_path = folder / "config.toml"
    config_path.write_text(text)
    command = [
        "/usr/bin/sandbox-exec",
        "-p",
        POLICY,
        str(ROOT / ".venv/bin/audio-notes"),
        "--config",
        str(config_path),
        "run",
    ]
    started = time.monotonic()
    subprocess.run(command, check=True, timeout=900)
    elapsed = time.monotonic() - started
    config = load_config(config_path)
    completed = []
    for raw_path in sorted(config.output.glob("*/raw.json")):
        raw = json.loads(raw_path.read_text())
        assert raw["engine"]["name"] == "mlx-qwen3-asr"
        assert raw["engine"]["device"] == "metal"
        assert exports_valid(raw_path.parent, config.markdown)
        assert raw["text"] == "".join(s["text"] for s in raw["segments"])
        if raw["source"]["name"] == "纯静音":
            assert not raw["segments"] and not raw["text"]
        else:
            assert "今天" in raw["text"] and "结尾" in raw["text"]
            assert "Python" in raw["text"] and "Markdown" in raw["text"]
            assert all(s["words"] for s in raw["segments"])
            assert all(
                0 <= w["start"] <= w["end"] <= raw["info"]["duration"]
                for s in raw["segments"]
                for w in s["words"]
            )
            if raw["source"]["name"] == "跨段长录音":
                assert raw["info"]["duration"] > 30
                assert raw["text"].count("今天") == 5
                assert raw["text"].count("结尾") == 5
        completed.append(
            {
                "source": raw["source"]["name"],
                "duration": raw["info"]["duration"],
                "segments": len(raw["segments"]),
                "text": raw["text"],
                "peak_gpu_memory_bytes": raw["engine"]["peak_gpu_memory_bytes"],
            }
        )
    # The byte-identical WAV duplicate must reuse its result.
    assert len(completed) == 8
    raw_snapshot = {
        str(p): (sha256_file(p), p.stat().st_mtime_ns) for p in config.output.glob("*/raw.json")
    }
    notes = next(config.output.glob("*/notes.md"))
    notes.unlink()
    subprocess.run(command, check=True, timeout=90)
    assert notes.is_file()
    assert raw_snapshot == {
        str(p): (sha256_file(p), p.stat().st_mtime_ns) for p in config.output.glob("*/raw.json")
    }
    assert original == {str(p): (sha256_file(p), p.stat().st_mtime_ns) for p in inbox.iterdir()}
    with sqlite3.connect(config.state / "jobs.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE attempts != 1").fetchone()[0] == 0
        assert (
            db.execute("SELECT COUNT(*) FROM jobs WHERE status != 'completed'").fetchone()[0] == 0
        )
    report = {
        "network_policy": POLICY,
        "elapsed_seconds": elapsed,
        "formats": ["m4a", "qta", "wav", "mp3", "mov", "mp4"],
        "silence_empty": True,
        "long_audio_tail_preserved": True,
        "word_timestamps_with_original_offsets": True,
        "original_audio_unchanged": True,
        "dedup_and_export_repair_without_reinference": True,
        "results": completed,
    }
    atomic_json(folder / "report.json", report)
    (ROOT / "verification/qwen-upgrade-latest.txt").write_text(str(folder))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
