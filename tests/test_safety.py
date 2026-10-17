import json
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from audio_notes.config import load_config
from audio_notes.files import atomic_json, run_lock
from audio_notes.markdown import join_text, render, timestamp
from audio_notes.pipeline import run_worker, set_lock_fd


def test_timestamp_rounding():
    assert timestamp(59.9996) == "00:01:00.000"
    assert timestamp(3661.123) == "01:01:01.123"


def test_reading_text_preserves_every_nonspace_character():
    texts = [
        " 今天讨论 Python。",
        " The release",
        " plan is ready. ",
        "嗯，嗯，重复不要删除。",
        "*不要编造*",
    ]
    combined = join_text(texts)
    assert re.sub(r"\s", "", combined) == re.sub(r"\s", "", "".join(texts))
    assert "The release plan" in combined


def test_atomic_write_does_not_replace_target_on_serialization_error(tmp_path):
    path = tmp_path / "raw.json"
    atomic_json(path, {"ok": True})
    with pytest.raises(ValueError):
        atomic_json(path, {"nan": float("nan")})
    assert json.loads(path.read_text()) == {"ok": True}
    assert not list(tmp_path.glob(".writing-*"))


def test_cross_process_lock(config):
    with run_lock(config.state):
        script = (
            "from pathlib import Path; from audio_notes.files import run_lock; "
            f'\nwith run_lock(Path({str(config.state)!r})): print("unexpected")'
        )
        process = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        assert process.returncode != 0
        assert "另一个" in process.stderr
    with run_lock(config.state):
        pass


def test_worker_crash_is_reported_and_offline_environment(config, monkeypatch):
    captured = {}

    class Process:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        def wait(self, timeout):
            return -11

        def poll(self):
            return -11

    monkeypatch.setattr("audio_notes.pipeline.subprocess.Popen", Process)
    with run_lock(config.state) as handle:
        set_lock_fd(handle.fileno())
        with pytest.raises(RuntimeError, match="退出 -11"):
            run_worker(config, {"job_id": "abc", "source": "/tmp/test.wav"}, config.state / "log")
        set_lock_fd(None)
    assert captured["env"]["HF_HUB_OFFLINE"] == "1"
    assert captured["env"]["TRANSFORMERS_OFFLINE"] == "1"
    assert captured["pass_fds"]


def test_config_relative_to_file_and_rejects_overlap(tmp_path):
    project = Path(__file__).resolve().parents[1]
    text = (project / "config.toml").read_text()
    path = tmp_path / "config.toml"
    path.write_text(text)
    assert load_config(path).inbox == tmp_path / "inbox"
    path.write_text(text.replace('output = "output"', 'output = "inbox/notes"'))
    with pytest.raises(ValueError, match="目录不能重叠"):
        load_config(path)


def test_config_unknown_and_bad_model(tmp_path):
    text = (Path(__file__).resolve().parents[1] / "config.toml").read_text()
    path = tmp_path / "config.toml"
    path.write_text(re.sub(r'(?m)^model = "[^"]+"', 'model = "small.en"', text))
    with pytest.raises(ValueError, match="多语言模型"):
        load_config(path)
    path.write_text(text.replace("beam_size = 5", "beam_size = 5\nbeem_size = 9"))
    with pytest.raises(ValueError, match="未知配置"):
        load_config(path)


def test_config_extensions_are_normalized(tmp_path):
    text = (Path(__file__).resolve().parents[1] / "config.toml").read_text()
    path = tmp_path / "config.toml"
    path.write_text(re.sub(r"(?m)^extensions = .*$", 'extensions = ["M4A", ".wav"]', text))
    assert load_config(path).processing["extensions"] == ["m4a", "wav"]
    path.write_text(re.sub(r"(?m)^extensions = .*$", 'extensions = ["."]', text))
    with pytest.raises(ValueError, match="extensions"):
        load_config(path)


def test_render_zero_segments_and_markdown_escaping(config):
    raw = {
        "source": {"name": "会议 [测试]", "path": str(config.inbox / "会议 [测试].m4a")},
        "audio_sha256": "hash",
        "created_at": "now",
        "settings": {"model": "small"},
        "info": {"language": "zh", "duration": 1},
        "job_id": "job",
        "segments": [],
    }
    directory = config.output / "empty"
    render(raw, directory, config.markdown)
    assert "未识别出语音" in (directory / "notes.md").read_text()
    assert "%25" not in (directory / "notes.md").read_text()
    assert "%20" in (directory / "notes.md").read_text()
    assert "会议 \\[测试\\]" in (directory / "transcript.md").read_text()


def test_whisper_internal_infinity_preserved_in_strict_json(tmp_path):
    from audio_notes.worker import json_safe

    payload = {"options": {"max_speech_duration_s": float("inf")}, "text": "没有改写。"}
    path = tmp_path / "raw.json"
    atomic_json(path, json_safe(payload))
    saved = json.loads(path.read_text())
    assert saved["options"]["max_speech_duration_s"] == {"$float": "inf"}
    assert saved["text"] == payload["text"]


@pytest.mark.parametrize(
    "key,value", [("poll_seconds", "inf"), ("paragraph_max_chars", "nan"), ("poll_seconds", "true")]
)
def test_nonfinite_or_boolean_numeric_config_rejected(tmp_path, key, value):
    text = (Path(__file__).resolve().parents[1] / "config.toml").read_text()
    text = re.sub(rf"{key} = [\d.]+", f"{key} = {value}", text)
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(ValueError, match="有限数值"):
        load_config(path)


def test_worker_timeout_stops_job(config, monkeypatch):
    config = replace(config, processing={**config.processing, "timeout_seconds": 0.01})
    process_state = {"stopped": False}

    class Process:
        pid = 123456

        def __init__(self, *args, **kwargs):
            pass

        def wait(self, timeout=None):
            if process_state["stopped"]:
                return -15
            raise subprocess.TimeoutExpired("worker", timeout)

        def poll(self):
            return -15 if process_state["stopped"] else None

    def kill_group(pid, sig):
        process_state["stopped"] = True

    # Simulate deadline expiration, avoiding a slow or timing-sensitive unit test.
    times = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr("audio_notes.pipeline.time.monotonic", lambda: next(times))
    monkeypatch.setattr("audio_notes.pipeline.subprocess.Popen", Process)
    monkeypatch.setattr("audio_notes.pipeline.os.killpg", kill_group)
    with run_lock(config.state) as handle:
        set_lock_fd(handle.fileno())
        with pytest.raises(RuntimeError, match="超时"):
            run_worker(
                config, {"job_id": "timeout", "source": "/tmp/test.wav"}, config.state / "log"
            )
        set_lock_fd(None)
    assert process_state["stopped"]
