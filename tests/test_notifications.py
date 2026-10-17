import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from audio_notes import notifications
from audio_notes import pipeline as pipeline_module
from audio_notes.config import load_config
from audio_notes.files import atomic_json, sha256_file
from audio_notes.pipeline import Pipeline


@pytest.fixture
def enabled_config(config):
    return replace(config, notifications={"enabled": True})


def test_notification_text_is_data(enabled_config, monkeypatch):
    calls = []
    monkeypatch.setattr(notifications.sys, "platform", "darwin")
    monkeypatch.setattr(notifications.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    title = '会议 "测试" \\ 换行\n do shell script "touch /tmp/unwanted"'
    assert notifications.notify(enabled_config, "开始转写", title)
    args, kwargs = calls[0]
    assert args[0] == ["/usr/bin/osascript", "-", "开始转写", title]
    assert title not in kwargs["input"]
    assert kwargs["timeout"] == 5
    assert kwargs["check"] is True


def test_disabled_and_non_mac_do_not_launch(config, enabled_config, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("不应启动通知进程")

    monkeypatch.setattr(notifications.subprocess, "run", unexpected)
    monkeypatch.setattr(notifications.sys, "platform", "darwin")
    assert not notifications.notify(config, "标题", "消息")
    monkeypatch.setattr(notifications.sys, "platform", "linux")
    assert not notifications.notify(enabled_config, "标题", "消息")


@pytest.mark.parametrize(
    "error",
    [OSError("unavailable"), subprocess.CalledProcessError(1, "osascript"),
     subprocess.TimeoutExpired("osascript", 5)],
)
def test_notification_failure_does_not_fail_job(enabled_config, fake_worker, monkeypatch, error):
    monkeypatch.setattr(notifications.sys, "platform", "darwin")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(notifications.subprocess, "run", fail)
    (enabled_config.inbox / "meeting.wav").write_text("测试录音")
    pipeline = Pipeline(enabled_config, "profile", worker=fake_worker)
    try:
        counts = pipeline.scan()
        assert (counts.completed, counts.failed) == (1, 0)
        assert len(pipeline.store.completed()) == 1
        assert (enabled_config.output / "index.md").is_file()
    finally:
        pipeline.close()


def test_events_follow_real_work_and_skip_duplicates_and_repairs(config, fake_worker, monkeypatch):
    events = []
    source = config.inbox / "meeting.wav"
    source.write_text("测试录音")
    atomic_json(source.with_suffix(".wav.origin.json"), {
        "audio_sha256": sha256_file(source),
        "original_path": "/recordings/meeting.wav",
        "recording_title": "产品讨论",
    })

    def notify(config, title, message):
        if title == "开始转写":
            assert not fake_worker.calls
        if title == "原文已可读":
            assert (config.output / "index.md").is_file()
            assert len(pipeline.store.completed()) == 1
        events.append((title, message))

    monkeypatch.setattr(pipeline_module, "notify", notify)
    pipeline = Pipeline(config, "profile", worker=fake_worker)
    try:
        assert pipeline.scan(watch=True).completed == 1
        assert [title for title, _ in events] == ["原文已可读"]
        assert all("产品讨论" in message for _, message in events)
        assert all("测试录音" not in message for _, message in events)
        (config.inbox / "duplicate.wav").write_text("测试录音")
        assert pipeline.scan(watch=True).skipped == 2
        next(config.output.glob("*/notes.md")).unlink()
        assert pipeline.scan(watch=True).completed == 1
        assert len(events) == 1
    finally:
        pipeline.close()


def test_failed_file_notifies_and_cooldown_stays_quiet(config, fake_worker, monkeypatch):
    events = []
    monkeypatch.setattr(pipeline_module, "notify", lambda config, title, msg: events.append(title))
    (config.inbox / "broken.wav").write_text("broken")
    pipeline = Pipeline(config, "profile", worker=fake_worker)
    try:
        assert pipeline.scan(watch=True).failed == 1
        assert events == ["转写失败"]
        assert pipeline.scan(watch=True).waiting == 1
        assert events == ["转写失败"]
    finally:
        pipeline.close()


def test_notification_config_defaults_and_validation(tmp_path):
    original = Path(__file__).resolve().parents[1] / "config.toml"
    text = original.read_text()
    path = tmp_path / "config.toml"
    text_without_notifications = text.split("[notifications]")[0] + "[markdown]" + text.split(
        "[markdown]", 1
    )[1]
    path.write_text(text_without_notifications)
    assert load_config(path).notifications == {"enabled": True}
    path.write_text(text.replace('enabled = true # macOS', 'enabled = "yes" # macOS'))
    with pytest.raises(ValueError, match="必须是 true 或 false"):
        load_config(path)
