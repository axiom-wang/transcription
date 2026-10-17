import json
import os
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest

from audio_notes import service
from audio_notes import voice_memos as module
from audio_notes.files import sha256_file
from audio_notes.pipeline import Pipeline
from audio_notes.voice_memos import VoiceMemosImporter, inspect_sources, recording_metadata


@pytest.fixture
def imported_config(config, tmp_path):
    source = tmp_path / "apple-cache" / "Recordings"
    source.mkdir(parents=True)
    return replace(config, voice_memos={"enabled": True, "directories": [str(source)]})


def audio(config, name="old.m4a", content="今天讨论本地转写".encode()):
    path = Path(config.voice_memos["directories"][0]) / name
    path.write_bytes(content)
    os.utime(path, (time.time() - 60, time.time() - 60))
    return path


def test_old_and_new_discovery_copy_resume_unchanged_original(imported_config):
    source = audio(imported_config, content="今天讨论本地转写".encode())
    before = (sha256_file(source), source.stat().st_mtime_ns, source.stat().st_ctime_ns)
    importer = VoiceMemosImporter(imported_config)
    assert importer.scan().copied == 1
    destination = next(imported_config.inbox.rglob("*.m4a"))
    assert source.read_bytes() == destination.read_bytes()
    after_copy = destination.stat().st_ctime_ns
    importer.close()
    restarted = VoiceMemosImporter(imported_config)
    assert restarted.scan().skipped == 1
    assert destination.stat().st_ctime_ns == after_copy
    audio(imported_config, "new.m4a", b"new audio")
    assert restarted.scan().copied == 1
    assert before == (sha256_file(source), source.stat().st_mtime_ns, source.stat().st_ctime_ns)
    restarted.close()


def test_edited_original_preserves_both_snapshots(imported_config):
    source = audio(imported_config, content=b"old version")
    importer = VoiceMemosImporter(imported_config)
    importer.scan()
    source.write_bytes(b"new version")
    assert importer.scan().copied == 1
    assert {p.read_bytes() for p in imported_config.inbox.rglob("*.m4a")} == {
        b"old version",
        b"new version",
    }
    importer.close()


def test_deleted_private_copy_reimported(imported_config):
    audio(imported_config, content=b"audio")
    importer = VoiceMemosImporter(imported_config)
    importer.scan()
    next(imported_config.inbox.rglob("*.m4a")).unlink()
    assert importer.scan().copied == 1
    importer.close()


def test_duplicate_same_name_same_content_no_copy_churn(imported_config):
    source = audio(imported_config, content=b"audio")
    second = source.parent / "sub"
    second.mkdir()
    (second / source.name).write_bytes(source.read_bytes())
    importer = VoiceMemosImporter(imported_config)
    assert importer.scan().copied == 2
    assert importer.scan().skipped == 2
    assert len(list(imported_config.inbox.rglob("*.m4a"))) == 1
    importer.close()


def test_permission_denied_is_reported_not_silently_empty(imported_config, monkeypatch):
    def denied(path):
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(module.os, "scandir", denied)
    importer = VoiceMemosImporter(imported_config)
    counts = importer.scan()
    assert counts.failed == 1 and counts.errors[0]["permission_denied"]
    report = json.loads((imported_config.state / "voice-memos-status.json").read_text())
    assert report["failed"] == 1
    importer.close()


def test_hidden_recovery_and_symlink_excluded(imported_config):
    source = audio(imported_config)
    root = source.parent
    for name in (".hidden", "CaptureRecovery", "RecentlyDeleted", "CloudRecordings_ckAssets"):
        (root / name).mkdir()
        (root / name / "ignored.m4a").write_bytes(b"ignored")
    (root / "link.m4a").symlink_to(source)
    assert inspect_sources(imported_config)[0] == [source]


def test_fresh_recording_waits_then_imports(imported_config, monkeypatch):
    config = replace(
        imported_config, processing={**imported_config.processing, "settle_seconds": 30}
    )
    source = audio(config)
    os.utime(source, None)
    importer = VoiceMemosImporter(config)
    assert importer.scan().waiting == 1
    original_time = time.time
    monkeypatch.setattr(module.time, "time", lambda: original_time() + 60)
    assert importer.scan().copied == 1
    importer.close()


def test_changing_during_copy_not_published(imported_config, monkeypatch):
    source = audio(imported_config, content=b"old audio")
    original = module.source_signature
    calls = 0

    def changed(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            source.write_bytes(b"changed audio")
        return original(path)

    monkeypatch.setattr(module, "source_signature", changed)
    importer = VoiceMemosImporter(imported_config)
    assert importer.scan().failed == 1
    assert not list(imported_config.inbox.rglob("*.m4a"))
    assert not list(imported_config.inbox.rglob(".importing-*"))
    importer.close()


def test_read_only_apple_database_metadata(imported_config):
    source = audio(imported_config)
    database = source.parent / "CloudRecordings.db"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE ZCLOUDRECORDING (ZPATH TEXT,ZCUSTOMLABEL TEXT,ZDATE REAL,ZDURATION REAL,ZUNIQUEID TEXT)"
        )
        db.execute(
            "INSERT INTO ZCLOUDRECORDING VALUES (?,?,?,?,?)",
            (source.name, "真实录音标题", 0, 8.5, "uuid"),
        )
    before = sha256_file(database)
    metadata = recording_metadata(source.parent)
    assert metadata[source.name]["recording_title"] == "真实录音标题"
    assert metadata[source.name]["recorded_at"] == "2001-01-01T00:00:00+00:00"
    importer = VoiceMemosImporter(imported_config)
    assert importer.scan().copied == 1
    origin = json.loads(next(imported_config.inbox.rglob("*.origin.json")).read_text())
    assert origin["recording_title"] == "真实录音标题"
    assert origin["original_path"] == str(source)
    assert sha256_file(database) == before
    importer.close()


@pytest.mark.parametrize("name", ["old.m4a", "new.qta", "new.QTA"])
def test_auto_import_feeds_pipeline_and_deduplicates(imported_config, fake_worker, name):
    audio(imported_config, name=name, content="来自旧录音的内容。".encode())
    importer = VoiceMemosImporter(imported_config)
    pipeline = Pipeline(imported_config, "profile", worker=fake_worker)
    importer.scan()
    assert pipeline.scan().completed == 1
    assert len(fake_worker.calls) == 1
    assert importer.scan().skipped == 1
    assert pipeline.scan().skipped == 1
    assert len(fake_worker.calls) == 1
    importer.close()
    pipeline.close()


def test_legacy_agent_is_removed_and_cannot_be_reinstalled(imported_config, tmp_path, monkeypatch):
    monkeypatch.setattr(service.Path, "home", lambda: tmp_path / "home")
    calls = []
    monkeypatch.setattr(service, "registered", lambda config: not calls)
    monkeypatch.setattr(service, "launchctl", lambda *args, **kw: calls.append(args))
    plist = service.agent_path(imported_config)
    plist.parent.mkdir(parents=True)
    plist.write_text("legacy")
    assert service.cleanup_legacy(imported_config)
    assert not plist.exists()
    assert calls == [("bootout", f"gui/{os.getuid()}/{service.identity(imported_config)}")]
    assert not service.cleanup_legacy(imported_config)
    with pytest.raises(ValueError, match="自动处理已并入听记"):
        service.manage(imported_config, "install")
    assert not plist.exists()


def test_retired_background_agent_cleans_up_instead_of_scanning(imported_config, monkeypatch):
    from audio_notes import cli

    removed = []
    monkeypatch.setenv("AUDIO_NOTES_BACKGROUND", "1")
    monkeypatch.setattr(cli, "load_config", lambda path: imported_config)
    monkeypatch.setattr(service, "cleanup_legacy", lambda config: removed.append(config) or True)
    monkeypatch.setattr(cli, "VoiceMemosImporter", lambda config: pytest.fail("不应扫描"))
    assert cli.main(["run", "--no-wait"]) == 0
    assert removed == [imported_config]


@pytest.mark.parametrize("corrupt", [False, True])
def test_origin_sidecar_repaired(imported_config, corrupt):
    audio(imported_config, content=b"audio")
    importer = VoiceMemosImporter(imported_config)
    importer.scan()
    origin_path = next(imported_config.inbox.rglob("*.origin.json"))
    if corrupt:
        origin_path.write_text("corrupt json")
    else:
        origin_path.unlink()
    assert importer.scan().copied == 1
    assert json.loads(origin_path.read_text())["kind"] == "apple-voice-memos"
    assert importer.scan().skipped == 1
    importer.close()


def test_manual_run_waits_only_up_to_limit_and_records_summary(imported_config, monkeypatch):
    from audio_notes import cli
    from audio_notes.pipeline import Counts
    from audio_notes.voice_memos import ImportCounts

    calls = []

    class Importer:
        def __init__(self, config):
            pass

        def scan(self):
            return ImportCounts(copied=1 if not calls else 0, waiting=1, found=3)

        def close(self):
            pass

    class FakePipeline:
        def __init__(self, config, profile):
            pass

        def scan(self, watch=False, followup_limit=True):
            calls.append((watch, followup_limit))
            return Counts(completed=1 if len(calls) == 1 else 0)

        def close(self):
            pass

    monkeypatch.setattr(cli, "load_config", lambda path: imported_config)
    monkeypatch.setattr(cli, "VoiceMemosImporter", Importer)
    monkeypatch.setattr(cli, "Pipeline", FakePipeline)
    monkeypatch.setattr(cli, "check_model", lambda path: None)
    monkeypatch.setattr(type(imported_config), "profile", lambda self: "test-profile")
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    assert cli.main(["run", "--wait-limit", "0"]) == 3
    # A manual round finishes every follow-up stage instead of the per-pass limit.
    assert calls == [(False, False)]
    report = json.loads((imported_config.state / "last-scan.json").read_text())
    assert report["copied"] == 1 and report["completed"] == 1 and report["waiting"] == 1
