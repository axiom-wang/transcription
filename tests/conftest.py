from dataclasses import replace

import pytest

from audio_notes.config import load_config
from audio_notes.files import atomic_json


@pytest.fixture
def config(tmp_path):
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    config = load_config(root / "config.toml")
    config = replace(
        config,
        inbox=tmp_path / "inbox",
        output=tmp_path / "output",
        state=tmp_path / "state",
        models=tmp_path / "models",
        processing={**config.processing, "settle_seconds": 0},
        voice_memos={"enabled": False, "directories": []},
        notifications={"enabled": False},
        analysis={**config.analysis, "enabled": False},
        speakers={**config.speakers, "enabled": False},
    )
    config.ensure_dirs()
    return config


@pytest.fixture
def fake_worker():
    calls = []

    def worker(config, request, log):
        calls.append(request["source"])
        text = __import__("pathlib").Path(request["source"]).read_text()
        if text == "broken":
            raise RuntimeError("模拟解码失败")
        atomic_json(
            __import__("pathlib").Path(request["raw_path"]),
            {
                "schema_version": 1,
                "job_id": request["job_id"],
                "audio_sha256": request["audio_hash"],
                "profile": request["profile"],
                "source": {"path": request["source"], "name": "会议"},
                "created_at": "2026-10-02T00:00:00+00:00",
                "settings": request["settings"],
                "info": {"language": "zh", "duration": 8.0},
                "segments": [{"start": 0.0, "end": 2.0, "text": text}],
                "text": text,
            },
        )

    worker.calls = calls
    return worker
