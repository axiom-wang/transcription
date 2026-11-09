import json
import os
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest

from audio_notes.original_cleanup import HALT_FILE, OriginalCleaner, duration_matches
from audio_notes.pipeline import Pipeline
from audio_notes.voice_memos import VoiceMemosImporter


@pytest.fixture
def library(config, tmp_path):
    root = tmp_path / "apple" / "Recordings"
    root.mkdir(parents=True)
    with sqlite3.connect(root / "CloudRecordings.db") as db:
        db.execute(
            """CREATE TABLE ZCLOUDRECORDING (Z_PK INTEGER PRIMARY KEY, Z_OPT INTEGER,
            ZFLAGS INTEGER, ZFOLDER INTEGER, ZDATE REAL, ZDURATION REAL, ZPATH TEXT,
            ZUNIQUEID TEXT, ZCUSTOMLABEL TEXT, ZENCRYPTEDTITLE TEXT)"""
        )
    config = replace(
        config,
        voice_memos={
            "enabled": True,
            "directories": [str(root)],
            "delete_original_after_transcription": True,
            "delete_shortcut": "听记删除语音备忘录",
        },
    )
    return config, root


def add(root, name, title, content, duration=8.0):
    path = root / name
    path.write_bytes(content)
    os.utime(path, (time.time() - 60, time.time() - 60))
    with sqlite3.connect(root / "CloudRecordings.db") as db:
        db.execute(
            "INSERT INTO ZCLOUDRECORDING (Z_OPT,ZFLAGS,ZFOLDER,ZDATE,ZDURATION,ZPATH,ZUNIQUEID,"
            "ZCUSTOMLABEL,ZENCRYPTEDTITLE) VALUES (1,4,NULL,800000000,?,?,?,?,?)",
            (duration, name, "ID-" + title, "2026-10-01T00:00:00Z", title),
        )
    return path


def deleting_runner(root, calls, extra=None):
    """Simulates the Shortcut: Voice Memos removes the recording with that exact title."""

    def runner(name, title):
        calls.append((name, title))
        with sqlite3.connect(root / "CloudRecordings.db") as db:
            for (path,) in db.execute(
                "SELECT ZPATH FROM ZCLOUDRECORDING WHERE ZENCRYPTEDTITLE IN (?,?)", (title, extra)
            ):
                (root / path).unlink(missing_ok=True)
            db.execute("DELETE FROM ZCLOUDRECORDING WHERE ZENCRYPTEDTITLE IN (?,?)", (title, extra))

    return runner


def process(config, fake_worker, runner, installed=True):
    importer = VoiceMemosImporter(config)
    pipeline = Pipeline(config, "profile", worker=fake_worker)
    try:
        importer.scan()
        pipeline.scan()
        cleaner = OriginalCleaner(
            config,
            importer.db,
            pipeline.store,
            "profile",
            runner=runner,
            installed=lambda name: installed,
        )
        return cleaner.run()
    finally:
        pipeline.close()
        importer.close()


def test_deletes_original_only_after_verified_transcription(library, fake_worker):
    config, root = library
    source = add(root, "a.m4a", "Recording 8", "今天讨论本地转写".encode())
    calls = []
    counts = process(config, fake_worker, deleting_runner(root, calls))
    assert counts.deleted == 1
    assert calls == [("听记删除语音备忘录", "Recording 8")]
    assert not source.exists()
    copy = next(config.inbox.rglob("a.m4a"))
    assert copy.read_bytes() == "今天讨论本地转写".encode()
    # A recording restored from Recently Deleted is never deleted again.
    add(root, "a.m4a", "Recording 8", "今天讨论本地转写".encode())
    with sqlite3.connect(root / "CloudRecordings.db") as db:
        db.execute("UPDATE ZCLOUDRECORDING SET ZUNIQUEID='ID-Recording 8'")
    process(config, fake_worker, deleting_runner(root, calls))
    assert len(calls) == 1


@pytest.mark.parametrize(
    "content,duration,reason",
    [
        (b"broken", 8.0, "转写尚未完成"),  # decode/transcription failed
        ("内容".encode(), 60.0, "解码时长"),  # decoded length doesn't match Voice Memos
    ],
)
def test_keeps_original_when_not_confirmed(library, fake_worker, content, duration, reason):
    config, root = library
    source = add(root, "a.m4a", "Recording 8", content, duration)
    calls = []
    counts = process(config, fake_worker, deleting_runner(root, calls))
    assert counts.deleted == 0 and calls == [] and source.exists()
    if reason != "转写尚未完成":
        status = json.loads((config.state / "original-deletions-status.json").read_text())
        assert reason in status["details"][0]["reason"]


def test_ambiguous_names_and_missing_shortcut_keep_original(library, fake_worker):
    config, root = library
    first = add(root, "a.m4a", "Recording", "一".encode())
    second = add(root, "b.m4a", "Recording 2", "二".encode())
    calls = []
    counts = process(config, fake_worker, deleting_runner(root, calls), installed=False)
    assert first.exists() and second.exists() and calls == []
    assert counts.blocked == 1 and counts.deleted == 0
    counts = process(config, fake_worker, deleting_runner(root, calls))
    # "Recording" is contained in "Recording 2" and stays for manual deletion.
    assert calls == [("听记删除语音备忘录", "Recording 2")]
    assert first.exists() and not second.exists()


def test_unexpected_extra_deletion_halts(library, fake_worker):
    config, root = library
    add(root, "a.m4a", "Alpha", "一".encode())
    add(root, "b.m4a", "Beta", "二".encode())
    calls = []
    counts = process(config, fake_worker, deleting_runner(root, calls, extra="Beta"))
    assert counts.failed == 1
    assert len(calls) == 1 and (config.state / HALT_FILE).is_file()
    add(root, "c.m4a", "Gamma", "三".encode())
    process(config, fake_worker, deleting_runner(root, calls))
    assert len(calls) == 1 and (root / "c.m4a").exists()


def test_config_key_and_duration_tolerance():
    root = Path(__file__).resolve().parents[1]
    from audio_notes.config import load_config

    loaded = load_config(root / "config.toml")
    assert "delete_original_after_transcription" in loaded.voice_memos
    assert duration_matches(100.0, 101.5) and not duration_matches(100.0, 110.0)
