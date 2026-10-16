import signal
from contextlib import contextmanager

import pytest

from audio_notes import cli, manual
from audio_notes.config import Config
from audio_notes.files import AlreadyRunning, sha256_file
from audio_notes.pipeline import Pipeline, config_lock_fd


@pytest.fixture
def manual_environment(config, fake_worker, monkeypatch):
    monkeypatch.setattr(manual, "check_model", lambda directory: None)
    monkeypatch.setattr(Config, "profile", lambda self: "manual-profile")
    monkeypatch.setattr(
        manual, "Pipeline", lambda config, profile: Pipeline(config, profile, worker=fake_worker)
    )
    return config


@pytest.mark.parametrize("extension", ["wav", "qta", "QTA"])
def test_only_selected_external_recording_processed(manual_environment, fake_worker, extension):
    config = manual_environment
    source = config.inbox.parent / f"录音 '带空格.{extension}"
    source.write_text("指定录音")
    (config.inbox / "other.wav").write_text("不应处理")
    before = (sha256_file(source), source.stat().st_mtime_ns)
    previous_handler = signal.getsignal(signal.SIGTERM)
    directories = manual.transcribe(config, [source, source])
    assert fake_worker.calls == [str(source)]
    assert len(directories) == 1
    assert (directories[0] / "notes.md").is_file()
    assert before == (sha256_file(source), source.stat().st_mtime_ns)
    assert signal.getsignal(signal.SIGTERM) == previous_handler
    with pytest.raises(RuntimeError, match="未持有运行锁"):
        config_lock_fd()
    assert manual.transcribe(config, [source]) == directories
    assert fake_worker.calls == [str(source)]


def test_multiple_selected_files_and_reveal_index(manual_environment, monkeypatch):
    config = manual_environment
    first, second = (config.inbox.parent / name for name in ("one.wav", "two.MP3"))
    first.write_text("first")
    second.write_text("second")
    calls = []
    monkeypatch.setattr(manual.subprocess, "run", lambda *a, **kw: calls.append(a[0]))
    assert len(manual.transcribe(config, [first, second], reveal=True)) == 2
    assert calls == [["/usr/bin/open", "-R", str(config.output / "index.md")]]


def test_validate_entire_selection_before_transcribing(manual_environment, fake_worker):
    config = manual_environment
    valid = config.inbox.parent / "valid.wav"
    valid.write_text("valid")
    invalid = config.inbox.parent / "invalid.txt"
    invalid.write_text("text")
    with pytest.raises(ValueError, match="不支持此文件格式"):
        manual.transcribe(config, [valid, invalid])
    assert not fake_worker.calls
    assert not (config.state / "jobs.sqlite3").exists()
    with pytest.raises(ValueError, match="文件夹"):
        manual.selected_files(config, [config.inbox])
    empty = config.inbox.parent / "empty.wav"
    empty.touch()
    with pytest.raises(ValueError, match="录音为空"):
        manual.selected_files(config, [empty])
    with pytest.raises(FileNotFoundError):
        manual.selected_files(config, [config.inbox / "missing.wav"])


def test_busy_automatic_instance_waits_and_continues(config, monkeypatch):
    attempts, notices, sleeps = [], [], []

    @contextmanager
    def busy_then_available(state):
        attempts.append(state)
        if len(attempts) < 3:
            raise AlreadyRunning("busy")
        with (state / "test.lock").open("a+") as handle:
            yield handle

    monkeypatch.setattr(manual, "run_lock", busy_then_available)
    monkeypatch.setattr(manual.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(manual, "notify", lambda *args: notices.append(args))
    with manual.wait_for_turn(config):
        assert config_lock_fd() >= 0
    assert len(attempts) == 3
    assert sleeps == [1, 1]
    assert len(notices) == 1
    assert notices[0][1] == "手动转写已排队"


def test_broken_selection_continues_other_file_and_releases_lock(manual_environment, fake_worker):
    config = manual_environment
    bad, good = (config.inbox.parent / name for name in ("bad.wav", "good.wav"))
    bad.write_text("broken")
    good.write_text("good")
    with pytest.raises(RuntimeError, match="部分所选录音转写失败"):
        manual.transcribe(config, [bad, good])
    assert fake_worker.calls == [str(bad), str(good)]
    assert len(list(config.output.glob("*/notes.md"))) == 1
    with manual.wait_for_turn(config):
        assert config_lock_fd() >= 0


def test_manual_cli_dispatch_and_error_dialog(config, monkeypatch):
    calls, errors = [], []
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(manual, "show_error", lambda message: errors.append(message))

    def transcribe(config, files, reveal):
        calls.append((files, reveal))
        raise ValueError("文件不可用")

    monkeypatch.setattr(manual, "transcribe", transcribe)
    assert cli.main(["transcribe", "--gui", "--reveal", "--", "录音.wav"]) == 2
    assert calls[0][1] is True
    assert errors == ["文件不可用"]
