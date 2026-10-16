"""Real offline recovery/isolation smoke tests in the separate verification directory.

Run create_verification_audio.py first. Never touches the daily inbox/output/state.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "verification"
CLI = ROOT / ".venv" / "bin" / "audio-notes"
POLICY = "(version 1)(allow default)(deny network*)"


def command(config):
    return ["/usr/bin/sandbox-exec", "-p", POLICY, str(CLI), "--config", str(config), "run"]


def run(config, expected=0):
    result = subprocess.run(command(config), capture_output=True, text=True, timeout=120)
    if result.returncode != expected:
        raise AssertionError(result.stdout + result.stderr)
    return result.stdout + result.stderr


def jobs(state):
    with sqlite3.connect(state / "jobs.sqlite3") as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM jobs")]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    config = VERIFY / "config.toml"
    media = sorted((VERIFY / "inbox").glob("中文与English测试.*"))
    assert {path.suffix for path in media} == {".m4a", ".mp3", ".wav", ".mov", ".mp4"}
    original = {str(path): (sha(path), path.stat().st_mtime_ns) for path in media}
    run(config)
    records = [
        job
        for job in jobs(VERIFY / "state")
        if job["status"] == "completed" and job["source"] in original
    ]
    assert len(records) == 5
    output = Path(records[0]["output"])
    raw_path = output / "raw.json"
    raw = json.loads(raw_path.read_text())
    assert "今天" in raw["text"] and "Markdown" in raw["text"]
    assert raw["segments"] and raw["segments"][0]["words"]
    assert all(
        word["start"] <= word["end"] for segment in raw["segments"] for word in segment["words"]
    )
    attempts = {job["id"]: job["attempts"] for job in records}
    duplicate = VERIFY / "inbox" / "重复副本.wav"
    shutil.copy2(next(path for path in media if path.suffix == ".wav"), duplicate)
    run(config)
    assert {
        job["id"]: job["attempts"] for job in jobs(VERIFY / "state") if job["id"] in attempts
    } == attempts
    raw_snapshot = (sha(raw_path), raw_path.stat().st_mtime_ns)
    (output / "notes.md").unlink()
    run(config)
    assert raw_snapshot == (sha(raw_path), raw_path.stat().st_mtime_ns)
    assert (output / "notes.md").is_file()
    assert {
        job["id"]: job["attempts"] for job in jobs(VERIFY / "state") if job["id"] in attempts
    } == attempts

    broken = VERIFY / "inbox" / "00_损坏测试.m4a"
    broken.write_bytes(b"This is not audio")
    silence = VERIFY / "inbox" / "99_静音测试.wav"
    with wave.open(str(silence), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0\0" * 16000 * 3)
    log = run(config, expected=1)
    silence_record = next(job for job in jobs(VERIFY / "state") if job["source"] == str(silence))
    assert silence_record["status"] == "completed"
    assert json.loads((Path(silence_record["output"]) / "raw.json").read_text())["segments"] == []
    assert "损坏测试" in log
    broken.unlink()  # Only delete the fixture we just created, never user recordings.
    assert original == {str(path): (sha(path), path.stat().st_mtime_ns) for path in media}

    # Interrupt genuine native inference, using a longer generated recording.
    interruption = VERIFY / f"interruption-{time.time_ns()}"
    (interruption / "inbox").mkdir(parents=True, exist_ok=True)
    wav_path = next(path for path in media if path.suffix == ".wav")
    long_audio = interruption / "inbox" / "中断恢复.wav"
    with wave.open(str(wav_path), "rb") as incoming:
        parameters = incoming.getparams()
        frames = incoming.readframes(incoming.getnframes())
    with wave.open(str(long_audio), "wb") as outgoing:
        outgoing.setparams(parameters)
        outgoing.writeframes(frames * 4)
    interruption_config = interruption / "config.toml"
    interruption_config.write_text(
        config.read_text().replace('models = "../models"', 'models = "../../models"')
    )
    with (interruption / "process.log").open("w") as log_handle:
        process = subprocess.Popen(
            command(interruption_config), stdout=log_handle, stderr=log_handle
        )
        deadline = time.monotonic() + 30
        started = False
        while time.monotonic() < deadline and process.poll() is None:
            logs = list((interruption / "state" / "logs").glob("*.log"))
            if any("解码音频" in path.read_text() for path in logs):
                started = True
                break
            time.sleep(0.05)
        if not started:
            process.terminate()
            process.wait(timeout=10)
            raise AssertionError("未进入实际转写阶段")
        process.terminate()
        assert process.wait(timeout=10) == 130
    interrupted = jobs(interruption / "state")
    assert len(interrupted) == 1 and interrupted[0]["status"] == "interrupted"
    attempt = interrupted[0]["attempts"]
    run(interruption_config)
    restored = jobs(interruption / "state")[0]
    assert restored["status"] == "completed" and restored["attempts"] == attempt + 1
    # A freshly copied audio completes in one run; a failed neighbor isn't hot-retried.
    waiting = VERIFY / f"waiting-{time.time_ns()}"
    (waiting / "inbox").mkdir(parents=True)
    fresh_audio = waiting / "inbox" / "fresh.wav"
    shutil.copyfile(wav_path, fresh_audio)
    broken_neighbor = waiting / "inbox" / "00_broken.m4a"
    broken_neighbor.write_bytes(b"not audio")
    os.utime(broken_neighbor, (time.time() - 60, time.time() - 60))
    waiting_config = waiting / "config.toml"
    waiting_config.write_text(
        interruption_config.read_text()
        .replace("settle_seconds = 0", "settle_seconds = 2")
        .replace("poll_seconds = 10", "poll_seconds = 1")
    )
    waiting_log = run(waiting_config, expected=1)
    waiting_jobs = jobs(waiting / "state")
    assert "等待文件同步" in waiting_log
    assert (
        next(job for job in waiting_jobs if job["source"] == str(fresh_audio))["status"]
        == "completed"
    )
    assert (
        next(job for job in waiting_jobs if job["source"] == str(broken_neighbor))["attempts"] == 1
    )
    broken_neighbor.unlink()
    report = {
        "offline_network_policy": POLICY,
        "formats_transcribed": sorted(path.suffix for path in media),
        "original_audio_content_and_mtime_unchanged": True,
        "duplicate_skipped_without_inference": True,
        "missing_markdown_rebuilt_without_inference": True,
        "corrupt_media_isolated": True,
        "silence_handled_without_fabricated_text": True,
        "sigterm_during_native_inference_and_resume": True,
        "word_timestamps_preserved": True,
        "fresh_audio_waited_and_processed_in_one_run": True,
        "failed_neighbor_not_retried_during_wait": True,
    }
    (VERIFY / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
