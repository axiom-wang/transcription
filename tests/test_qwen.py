import json
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from audio_notes.config import load_config
from audio_notes.files import atomic_json, sha256_file
from audio_notes.model import QWEN_FILES, QWEN_MODEL, check_model
from audio_notes.qwen import transcribe_local


def test_qwen_manifest_checks_aligner_and_rejects_extra_paths(tmp_path):
    directory = tmp_path / QWEN_MODEL
    for name in QWEN_FILES:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test model")
    manifest = {
        "model": QWEN_MODEL,
        "engine": "mlx-qwen3-asr",
        "files": {name: sha256_file(directory / name) for name in QWEN_FILES},
    }
    atomic_json(directory / "local-model.json", manifest)
    assert check_model(directory)["model"] == QWEN_MODEL
    (directory / "aligner/model.safetensors").write_text("corrupted")
    with pytest.raises(ValueError, match="校验失败"):
        check_model(directory)
    manifest["files"]["../outside"] = "untrusted"
    atomic_json(directory / "local-model.json", manifest)
    with pytest.raises(ValueError, match="清单无效"):
        check_model(directory, verify=False)


def test_qwen_configuration_and_legacy_fallback(config, tmp_path):
    text = config.file.read_text()
    path = tmp_path / "config.toml"
    path.write_text(text.replace('compute_type = "float16"', 'compute_type = "int8"'))
    with pytest.raises(ValueError, match="float16"):
        load_config(path)
    path.write_text(text.replace(QWEN_MODEL, "small").replace('compute_type = "float16"', ""))
    assert load_config(path).whisper["compute_type"] == "int8"


def test_adapter_keeps_punctuation_offsets_and_rejects_truncation(config, monkeypatch):
    import faster_whisper.audio
    import mlx_qwen3_asr

    monkeypatch.setattr(faster_whisper.audio, "decode_audio", lambda *a, **k: np.zeros(160000))
    monkeypatch.setattr(
        "audio_notes.qwen.speech_regions", lambda *a: [{"start": 32000, "end": 96000}]
    )
    result = SimpleNamespace(
        text="你好，Python！",
        language="Chinese",
        truncated=False,
        chunks=[{"start": 0.0, "end": 4.0, "text": "你好，Python！"}],
        segments=[
            {"text": "你好", "start": 0.2, "end": 1.2},
            {"text": "Python", "start": 1.5, "end": 9.0},
        ],
    )
    monkeypatch.setattr(mlx_qwen3_asr, "transcribe", lambda *a, **k: result)
    saved, info, engine, native = transcribe_local("test.wav", config.model_dir, config.whisper)
    assert saved[0]["text"] == native[0]["text"] == "你好，Python！"
    assert saved[0]["words"][0]["start"] == 2.2
    assert saved[0]["words"][-1]["end"] == 6.0
    assert info["duration"] == 10.0 and engine["device"] == "metal"
    result.truncated = True
    with pytest.raises(RuntimeError, match="长度上限"):
        transcribe_local("test.wav", config.model_dir, config.whisper)


def test_silence_skips_recognizer(config, monkeypatch):
    import faster_whisper.audio
    import mlx_qwen3_asr

    monkeypatch.setattr(faster_whisper.audio, "decode_audio", lambda *a, **k: np.zeros(48000))
    monkeypatch.setattr("audio_notes.qwen.speech_regions", lambda *a: [])

    def unexpected(*args, **kwargs):
        raise AssertionError("静音不应调用识别器")

    monkeypatch.setattr(mlx_qwen3_asr, "transcribe", unexpected)
    saved, info, _, _ = transcribe_local("silence.wav", config.model_dir, config.whisper)
    assert saved == [] and info["duration_after_vad"] == 0


def test_worker_dispatches_qwen_and_keeps_engine_manifest(config, monkeypatch):
    from audio_notes.worker import transcribe

    config = replace(config, whisper={**config.whisper, "model": QWEN_MODEL})
    config.model_dir.mkdir(parents=True)
    atomic_json(config.model_dir / "local-model.json", {"model": QWEN_MODEL})
    source = config.inbox / "test.wav"
    source.write_bytes(b"test")
    monkeypatch.setattr(
        "audio_notes.qwen.transcribe_local",
        lambda *args, **kwargs: (
            [{"start": 0.0, "end": 1.0, "text": "你好！"}],
            {"language": "zh", "duration": 1.0},
            {"name": "mlx-qwen3-asr"},
            [],
        ),
    )
    raw_path = config.output / "raw.json"
    transcribe(
        {
            "source": str(source),
            "settings": config.whisper,
            "model_dir": str(config.model_dir),
            "audio_hash": sha256_file(source),
            "profile": "test",
            "job_id": "test",
            "raw_path": str(raw_path),
        }
    )
    raw = json.loads(raw_path.read_text())
    assert raw["text"] == "你好！"
    assert raw["engine"]["model_manifest"]["model"] == QWEN_MODEL
