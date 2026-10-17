"""The connector only imports synthetic files into an isolated real workbench server."""

import io
import json
import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from audio_notes import native_host, workbench
from audio_notes.pipeline import Pipeline


def exchange(config, message):
    incoming, outgoing = io.BytesIO(), io.BytesIO()
    native_host.write_message(incoming, message)
    incoming.seek(0)
    assert native_host.run(config, stdin=incoming, stdout=outgoing) == 0
    outgoing.seek(0)
    result = native_host.read_message(outgoing)
    assert outgoing.read() == b""  # No status text or extra reply frames on stdout.
    return result


def test_frame_unicode_and_partial_reads():
    frame = io.BytesIO()
    message = {"type": "import", "path": "/下载/测试.mkv"}
    native_host.write_message(frame, message)
    data = frame.getvalue()
    assert struct.unpack("<I", data[:4])[0] == len(data[4:])

    class ShortReads(io.BytesIO):
        def read(self, length):
            return super().read(min(length, 2))

    assert native_host.read_message(ShortReads(data)) == message


@pytest.mark.parametrize(
    "frame",
    [
        b"",
        b"\x01",
        struct.pack("<I", 0),
        struct.pack("<I", native_host.MAX_MESSAGE + 1),
        struct.pack("<I", 4) + b"{}",
        struct.pack("<I", 1) + b"\xff",
        struct.pack("<I", 2) + b"[]",
        struct.pack("<I", 1) + b"{",
    ],
)
def test_invalid_frames_return_single_error(config, frame):
    output = io.BytesIO()
    native_host.run(config, stdin=io.BytesIO(frame), stdout=output)
    output.seek(0)
    result = native_host.read_message(output)
    assert result["ok"] is False and result["error"]
    assert output.read() == b""


def test_ping_never_starts_or_opens_workbench(config, monkeypatch):
    def unexpected(*args):
        pytest.fail("ping must not start the workbench")

    monkeypatch.setattr(workbench, "ensure_workbench", unexpected)
    monkeypatch.setattr(workbench, "open_workbench", unexpected)
    result = exchange(config, {"type": "ping"})
    assert result["ok"] and result["version"] and result["api_version"] == workbench.API_VERSION
    assert not (config.state / "workbench-server.json").exists()


@pytest.fixture
def server(config, monkeypatch):
    app = workbench.Workbench(config)
    server = workbench.Server(("127.0.0.1", 0), app)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    monkeypatch.setattr(
        workbench,
        "ensure_workbench",
        lambda config: {
            "url": server.origin,
            "token": app.token,
        },
    )
    yield app
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.mark.parametrize("extension", ["mkv", "webm"])
def test_import_queues_and_reuses_completed_recording(config, server, fake_worker, extension):
    source = config.inbox / f"新的录音 #1 &讨论.{extension}"
    source.write_text("测试录音，仅用于连接器回归。")
    original = source.read_bytes(), source.stat().st_mtime_ns
    result = exchange(config, {"type": "import", "path": str(source)})
    assert result["ok"] and not result["reused"] and result["request"]
    assert result["name"] == source.name
    row = server.store.requests()[0]
    staged = Path(json.loads(row["payload"])["paths"][0])
    assert staged.read_bytes() == source.read_bytes()
    assert staged.is_relative_to(config.state)
    # Complete the synthetic recording so /api/upload follows its completed-content reuse path.
    pipeline = Pipeline(config, "test-profile", worker=fake_worker)
    pipeline.scan(sources=[staged])
    key = pipeline.store.completed()[0]["id"]
    pipeline.close()
    reused = exchange(config, {"type": "import", "path": str(source)})
    assert reused == {"ok": True, "reused": True, "id": key, "name": source.name}
    assert original == (source.read_bytes(), source.stat().st_mtime_ns)


@pytest.mark.parametrize(
    "kind, expected",
    [
        ("missing", "找不到"),
        ("empty", "录音为空"),
        ("unsupported", "不支持"),
        ("directory", "文件夹"),
        ("relative", "绝对路径"),
        ("invalid", "请选择"),
    ],
)
def test_invalid_paths_do_not_start_workbench(config, monkeypatch, kind, expected):
    def unexpected(*args):
        pytest.fail("invalid paths must fail before starting the workbench")

    monkeypatch.setattr(workbench, "ensure_workbench", unexpected)
    path = config.inbox / "测试.mkv"
    if kind == "empty":
        path.touch()
    if kind == "unsupported":
        path = path.with_suffix(".html")
        path.write_text("file")
    if kind == "directory":
        path.mkdir()
    value = "relative.mkv" if kind == "relative" else None if kind == "invalid" else str(path)
    result = exchange(config, {"type": "import", "path": value})
    assert not result["ok"] and expected in result["error"]


def test_open_fallback_does_not_pollute_frame(config, monkeypatch):
    monkeypatch.setattr(Path, "is_dir", lambda path: False)
    monkeypatch.setattr(workbench, "open_workbench", lambda config: print("已打开本地工作台"))
    assert exchange(config, {"type": "open"}) == {"ok": True}
    assert "已打开本地工作台" in (config.state / "logs/native-host.log").read_text()


def test_unknown_message(config):
    assert exchange(config, {"type": "unknown"}) == {"ok": False, "error": "不支持的听记连接操作"}


def test_open_prefers_installed_app(config, monkeypatch):
    monkeypatch.setattr(Path, "is_dir", lambda path: path == Path("/Applications/听记.app"))
    calls = []
    monkeypatch.setattr(native_host.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(workbench, "open_workbench", lambda config: pytest.fail("use native app"))
    assert exchange(config, {"type": "open"}) == {"ok": True}
    assert calls[0][0] == ["/usr/bin/open", "/Applications/听记.app"]


def test_installer_is_idempotent_and_wrapper_speaks_native_protocol(tmp_path):
    import runpy
    import shlex
    import shutil

    project = Path(__file__).resolve().parents[1]
    installer = runpy.run_path(str(project / "scripts/install_browser_extension.py"))
    root = tmp_path / "听记 'connector' $root"
    (root / "browser-extension").mkdir(parents=True)
    (root / ".venv/bin").mkdir(parents=True)
    interpreter = root / ".venv/bin/python"
    interpreter.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    interpreter.chmod(0o755)
    (root / "src").symlink_to(project / "src", target_is_directory=True)
    shutil.copyfile(project / "config.toml", root / "config.toml")
    shutil.copyfile(
        project / "browser-extension/manifest.json", root / "browser-extension/manifest.json"
    )
    home = tmp_path / "home"
    support = home / "Library/Application Support"
    (support / "Arc/User Data").mkdir(parents=True)
    (support / "Microsoft Edge").mkdir(parents=True)
    identifier, written = installer["install"](root, home)
    first = {str(path): path.read_bytes() for path in written}
    assert len(identifier) == 32 and all("a" <= char <= "p" for char in identifier)
    assert len(written) == 3
    assert not (support / "Chromium").exists()
    assert installer["install"](root, home) == (identifier, written)
    assert first == {str(path): path.read_bytes() for path in written}
    wrapper = root / "developer/native-host.sh"
    assert wrapper.stat().st_mode & 0o777 == 0o755
    for path in written:
        manifest = json.loads(path.read_text())
        assert manifest["allowed_origins"] == [f"chrome-extension://{identifier}/"]
        assert manifest["path"] == str(wrapper) and manifest["type"] == "stdio"
    incoming = io.BytesIO()
    native_host.write_message(incoming, {"type": "ping"})
    result = subprocess.run(
        [str(wrapper)], input=incoming.getvalue(), capture_output=True, timeout=15, cwd=tmp_path
    )
    assert result.returncode == 0 and result.stderr == b""
    output = io.BytesIO(result.stdout)
    assert native_host.read_message(output)["ok"]
    assert output.read() == b""
    assert not (root / "state/workbench-server.json").exists()
