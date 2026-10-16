import json
import os
import time
from dataclasses import replace

import pytest

from audio_notes import pipeline as module
from audio_notes.files import sha256_file
from audio_notes.pipeline import Pipeline, exports_valid


@pytest.fixture
def pipeline(config, fake_worker):
    pipeline = Pipeline(config, "test-profile", worker=fake_worker)
    yield pipeline
    pipeline.close()


def put(config, name="meeting.m4a", text="今天讨论 Python 项目。"):
    path = config.inbox / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def output(config):
    return next(config.output.glob("*/raw.json")).parent


def test_batch_dedup_and_original_untouched(config, pipeline, fake_worker):
    source = put(config)
    before = (sha256_file(source), source.stat().st_mtime_ns)
    put(config, "sub/renamed.MP4")
    counts = pipeline.scan()
    assert counts.completed == 1 and counts.skipped == 1
    assert len(fake_worker.calls) == 1
    assert pipeline.scan().skipped == 2
    assert len(fake_worker.calls) == 1
    assert before == (sha256_file(source), source.stat().st_mtime_ns)
    assert exports_valid(output(config), config.markdown)
    assert pipeline.store.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 2


def test_failure_does_not_block_batch_and_retries(config, pipeline, fake_worker):
    broken = put(config, "a.m4a", "broken")
    put(config, "b.wav", "good")
    counts = pipeline.scan()
    assert (counts.failed, counts.completed) == (1, 1)
    # Re-scans while waiting for other files do not retry a known failure in the same run.
    assert pipeline.scan().failed == 0
    assert len(fake_worker.calls) == 2
    restarted = Pipeline(config, "test-profile", worker=fake_worker)
    assert restarted.scan().failed == 1
    assert len(fake_worker.calls) == 3
    restarted.close()
    # Replacing contents creates a new job; the failed historical job remains auditable.
    broken.write_text("fixed")
    assert pipeline.scan().completed == 1


def test_empty_and_filtered_inbox(config, pipeline):
    put(config, ".hidden/test.m4a")
    put(config, "ignored.txt")
    outside = config.inbox.parent / "outside.m4a"
    outside.write_text("outside")
    (config.inbox / "link.m4a").symlink_to(outside)
    assert pipeline.scan().completed == 0


def test_settling_file_deferred(config, fake_worker):
    config = replace(config, processing={**config.processing, "settle_seconds": 30})
    pipeline = Pipeline(config, "profile", worker=fake_worker)
    source = put(config)
    assert pipeline.scan().waiting == 1
    os.utime(source, (time.time() - 60, time.time() - 60))
    assert pipeline.scan().completed == 1
    pipeline.close()


def test_missing_markdown_repaired_without_asr(config, pipeline, fake_worker):
    put(config)
    pipeline.scan()
    (output(config) / "notes.md").unlink()
    assert pipeline.scan().completed == 1
    assert len(fake_worker.calls) == 1


def test_markdown_setting_change_reuses_asr(config, pipeline, fake_worker):
    put(config)
    pipeline.scan()
    pipeline.config = replace(config, markdown={**config.markdown, "paragraph_max_chars": 200})
    assert pipeline.scan().completed == 1
    assert len(fake_worker.calls) == 1


def test_old_exports_upgraded_to_simplified_without_asr(config, pipeline, fake_worker):
    put(config, text="這個 Whisper 會說中文。")
    pipeline.scan()
    directory = output(config)
    original_names = ("raw.json", "transcript.md", "transcript.srt", "transcript.txt")
    before = {name: sha256_file(directory / name) for name in original_names}
    for path in directory.glob("transcript.simplified.*"):
        path.unlink()
    assert not exports_valid(directory, config.markdown)
    assert pipeline.scan().completed == 1
    assert len(fake_worker.calls) == 1
    assert before == {name: sha256_file(directory / name) for name in original_names}
    assert exports_valid(directory, config.markdown)
    assert "这个 Whisper 会说中文。" in (directory / "notes.md").read_text()
    assert "transcript.simplified.md" in (config.output / "index.md").read_text()
    assert pipeline.store.get(directory.name)["attempts"] == 1


def test_crash_after_raw_before_exports_recovered(config, fake_worker, monkeypatch):
    put(config)
    pipeline = Pipeline(config, "profile", worker=fake_worker)

    def crash(*args):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(module, "write_exports", crash)
        with pytest.raises(KeyboardInterrupt):
            pipeline.scan()
    pipeline.close()
    restarted = Pipeline(config, "profile", worker=fake_worker)
    assert restarted.scan().completed == 1
    assert len(fake_worker.calls) == 1
    restarted.close()


def test_stale_running_job_recovers_from_raw(config, pipeline, fake_worker):
    put(config)
    pipeline.scan()
    job_id = output(config).name
    pipeline.store.mark(job_id, "running")
    (output(config) / "complete.json").unlink()
    restarted = Pipeline(config, "test-profile", worker=fake_worker)
    assert restarted.store.get(job_id)["status"] == "interrupted"
    assert restarted.scan().completed == 1
    assert len(fake_worker.calls) == 1
    restarted.close()


def test_corrupt_raw_quarantined_and_transcribed_again(config, pipeline, fake_worker):
    put(config)
    pipeline.scan()
    (output(config) / "raw.json").write_text("{broken")
    assert pipeline.scan().completed == 1
    assert len(fake_worker.calls) == 2
    assert list(output(config).glob("raw.invalid-*.json"))


def test_new_inference_profile_creates_separate_result(config, pipeline, fake_worker):
    put(config)
    pipeline.scan()
    restarted = Pipeline(config, "new-profile", worker=fake_worker)
    assert restarted.scan().completed == 1
    assert len(list(config.output.glob("*/raw.json"))) == 2
    restarted.close()


def test_watch_cooldown(config, pipeline, fake_worker):
    put(config, text="broken")
    assert pipeline.scan(watch=True).failed == 1
    assert pipeline.scan(watch=True).waiting == 1
    assert len(fake_worker.calls) == 1


def test_bad_cache_identity_not_reused(config, pipeline, fake_worker):
    put(config)
    pipeline.scan()
    path = output(config) / "raw.json"
    raw = json.loads(path.read_text())
    raw["job_id"] = "another-job"
    path.write_text(json.dumps(raw))
    assert pipeline.scan().completed == 1
    assert len(fake_worker.calls) == 2


def test_empty_transcript_is_valid(config, pipeline):
    put(config, text="")
    assert pipeline.scan().completed == 1
    assert "未识别出语音" not in (output(config) / "notes.md").read_text()  # one empty segment
