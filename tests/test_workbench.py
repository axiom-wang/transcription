"""Workbench integration tests use isolated synthetic recordings, never personal audio."""

import io
import json
import threading
import time
import wave
import zipfile
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import quote

import pytest

from audio_notes.control import paused, set_paused
from audio_notes.files import atomic_json, run_lock, sha256_file
from audio_notes.pipeline import Pipeline
from audio_notes.workbench import Server, Workbench, execute_request
from audio_notes.workbench_library import audio_path, compatible_audio, library
from audio_notes.workbench_store import Conflict, WorkbenchStore


@pytest.fixture
def recording(config, fake_worker):
    source = config.inbox / "讨论.wav"
    source.write_text("预算3000元。只是提议，尚未决定。")
    pipeline = Pipeline(config, "test-profile", worker=fake_worker)
    pipeline.scan()
    entry = pipeline.store.completed()[0]
    pipeline.close()
    return source, entry


@pytest.fixture
def http(config, recording):
    app = Workbench(config)
    app.legacy_service = lambda: {"removed": False}
    server = Server(("127.0.0.1", 0), app)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()

    def request(path, method="GET", body=None, headers=None, auth=True):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        given = {"Authorization": "Bearer " + app.token} if auth else {}
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            given["Content-Type"] = "application/json"
        given.update(headers or {})
        connection.request(method, path, body=body, headers=given)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    yield request, app, server, recording[1]["id"]
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_local_auth_origin_and_host(http):
    request, app, server, key = http
    assert request("/api/library", auth=False)[0] == 403
    assert request("/api/library", headers={"Host": "attacker.test"})[0] == 403
    assert (
        request(
            "/api/action",
            "POST",
            {"action": "pause", "paused": True},
            headers={"Origin": "https://attacker.test"},
        )[0]
        == 403
    )
    assert not paused(app.config)
    assert request("/api/library")[0] == 200
    assert request("/api/library", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    code, headers, _ = request("/api/session", "POST")
    assert code == 200
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    assert request("/api/library", headers={"Cookie": cookie}, auth=False)[0] == 200


def test_static_assets_do_not_need_credentials(http):
    request, _, _, _ = http
    for path in ("/", "/style.css", "/app.js"):
        code, headers, body = request(path, auth=False)
        assert code == 200 and body
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert request("/../config.toml", auth=False)[0] == 403


def test_original_available_when_analysis_failed(http):
    request, app, _, key = http
    directory = app.config.output / key
    atomic_json(directory / "enrichment.status.json", {"status": "failed", "error": "模拟文本失败"})
    data = json.loads(request("/api/library")[2])
    item = data["items"][0]
    assert item["asr_status"] == "completed" and item["text_status"] == "failed"
    assert "transcript.simplified.md" in item["available"]
    assert request(f"/api/document?id={key}&name=transcript.simplified.md")[0] == 200


def test_range_and_missing_or_changed_audio(http, recording):
    request, app, _, key = http
    content = recording[0].read_bytes()
    code, headers, body = request(f"/api/audio?id={key}", headers={"Range": "bytes=0-3"})
    assert code == 206 and body == content[:4]
    assert headers["Content-Range"] == f"bytes 0-3/{len(content)}"
    assert request(f"/api/audio?id={key}", headers={"Range": "bytes=-4"})[2] == content[-4:]
    assert request(f"/api/audio?id={key}", headers={"Range": "bytes=99999-"})[0] == 416
    assert request(f"/api/audio?id={key}", headers={"Range": "bytes=0-1,4-5"})[0] == 416
    recording[0].write_text("音频已经编辑，与这份转写不对应")
    assert request(f"/api/audio?id={key}")[0] == 404
    assert request(f"/api/detail?id={key}")[0] == 200
    assert json.loads(request(f"/api/detail?id={key}")[2])["audio_name"] is None


def test_artifact_allowlist_and_export(http):
    request, app, _, key = http
    for name in ("../../config.toml", "enrichment.complete.json", "../raw.json"):
        assert request(f"/api/download?id={key}&name={quote(name)}")[0] == 400
    target = app.config.output / key / "notes.md"
    target.unlink()
    target.symlink_to(app.config.file)
    assert request(f"/api/download?id={key}&name=notes.md")[0] == 404
    code, _, data = request(f"/api/export?id={key}")
    assert code == 200
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert len(archive.namelist()) == 1
        assert "预算3000" in archive.read(archive.namelist()[0]).decode()


def test_notes_are_independent_and_conflicts_preserve_saved_content(http):
    request, app, _, key = http
    before = sha256_file(app.config.output / key / "transcript.md")
    change = {"action": "metadata", "id": key, "changes": {"notes": "人工核对记录", "revision": 0}}
    code, _, body = request("/api/action", "POST", change)
    assert code == 200 and json.loads(body)["revision"] == 1
    change["changes"]["notes"] = "另一个窗口的旧稿"
    assert request("/api/action", "POST", change)[0] == 409
    saved = json.loads(request(f"/api/detail?id={key}")[2])
    assert saved["notes"] == "人工核对记录"
    assert before == sha256_file(app.config.output / key / "transcript.md")
    new_store = WorkbenchStore(app.config.state)
    assert next(iter(new_store.metadata().values()))["notes"] == "人工核对记录"
    with new_store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM note_history").fetchone()[0] == 1


def test_metadata_validation_and_archive(http):
    request, _, _, key = http
    assert (
        request(
            "/api/action", "POST", {"action": "metadata", "id": key, "changes": {"archived": "yes"}}
        )[0]
        == 400
    )
    assert (
        request(
            "/api/action",
            "POST",
            {
                "action": "metadata",
                "id": key,
                "changes": {"title": "新名字", "favorite": True, "archived": True},
            },
        )[0]
        == 200
    )
    item = json.loads(request("/api/library")[2])["items"][0]
    assert (item["title"], item["favorite"], item["archived"]) == ("新名字", True, True)


def test_upload_dedup_keeps_original_and_adds_recovery_source(http, recording):
    request, app, _, key = http
    source, _ = recording
    content = source.read_bytes()
    before = (sha256_file(source), source.stat().st_mtime_ns)
    code, _, result = request(
        "/api/upload", "POST", content, headers={"X-Filename": quote("新的文件名.wav")}
    )
    assert code == 200 and json.loads(result)["reused"]
    assert app.store.requests() == []
    assert before == (sha256_file(source), source.stat().st_mtime_ns)
    source.unlink()
    assert audio_path(app.config, key).read_bytes() == content


def test_cancelled_upload_is_not_seen_by_background_scan(http):
    request, app, _, _ = http
    code, _, data = request(
        "/api/upload", "POST", b"brand new audio", headers={"X-Filename": quote("新录音.wav")}
    )
    assert code == 201
    rid = json.loads(data)["request"]
    assert request("/api/action", "POST", {"action": "cancel", "id": rid})[0] == 200
    row = app.store.requests()[0]
    path = Path(json.loads(row["payload"])["paths"][0])
    assert path.is_relative_to(app.config.state)
    assert not path.is_relative_to(app.config.inbox)
    assert app.store.pending() is None


def test_empty_and_unsupported_upload_rejected(http):
    request, _, _, _ = http
    assert request("/api/upload", "POST", b"", headers={"X-Filename": "empty.wav"})[0] == 400
    assert request("/api/upload", "POST", b"script", headers={"X-Filename": "file.html"})[0] == 400


def test_queue_idempotency_priority_and_race(config):
    store = WorkbenchStore(config.state)
    first = store.enqueue("scan", {}, "扫描")
    assert store.enqueue("scan", {}, "扫描")["id"] == first["id"]
    second = store.enqueue("notify", {}, "通知")
    store.request_action(second["id"], "prioritize")
    assert store.pending()["id"] == second["id"]
    assert store.claim(second["id"])
    assert not store.claim(second["id"])
    with pytest.raises(Conflict):
        store.request_action(second["id"], "cancel")
    store.finish(second["id"], "failed", "失败")
    store.request_action(second["id"], "retry")
    assert store.pending()["id"] == second["id"]


def test_pause_stops_between_recordings_and_resumes(config, fake_worker):
    (config.inbox / "one.wav").write_text("first")
    (config.inbox / "two.wav").write_text("second")

    def worker(*args):
        fake_worker(*args)
        set_paused(config, True)

    pipeline = Pipeline(config, "profile", worker=worker)
    assert pipeline.scan().completed == 1
    assert len(fake_worker.calls) == 1
    assert pipeline.scan().completed == 0
    set_paused(config, False)
    pipeline.worker = fake_worker
    assert pipeline.scan().completed == 1
    pipeline.close()
    assert len(fake_worker.calls) == 2


def test_automatic_scan_yields_to_explicit_work(config, fake_worker):
    (config.inbox / "one.wav").write_text("first")
    store = WorkbenchStore(config.state)
    store.enqueue("scan", {}, "扫描")
    pipeline = Pipeline(config, "profile", worker=fake_worker)
    with run_lock(config.state / "workbench-server"):
        assert pipeline.scan().completed == 0
    # A closed workbench must never block the ordinary background service.
    assert pipeline.scan().completed == 1
    pipeline.close()


def test_versions_grouped_by_audio_content(config, fake_worker, recording):
    pipeline = Pipeline(config, "different-profile", worker=fake_worker)
    pipeline.scan()
    pipeline.close()
    items = library(config, WorkbenchStore(config.state))
    assert len(items) == 1 and len(items[0]["versions"]) == 2


def test_stale_running_state_is_not_claimed_as_live(config, recording):
    source, entry = recording
    from audio_notes.store import Store

    store = Store(config.state)
    store.mark(entry["id"], "running")
    store.close()
    assert library(config, WorkbenchStore(config.state))[0]["asr_status"] == "interrupted"


def test_compatible_preview_decodes_without_modifying_source(config):
    source = config.inbox / "sample.wav"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\0\0" * 8000)
    digest = sha256_file(source)
    key = "c" * 64
    from audio_notes.store import Store

    store = Store(config.state)
    store.register(key, digest, "profile", source, config.output / key)
    store.close()
    target = compatible_audio(config, key)
    with wave.open(str(target), "rb") as preview:
        assert preview.getframerate() == 16000
        assert preview.getnframes() == 16000
    assert sha256_file(source) == digest
    assert compatible_audio(config, key) == target


def test_execute_request_checks_enrichment_result(config, monkeypatch, recording):
    from audio_notes import cli

    store = WorkbenchStore(config.state)
    request = store.enqueue("enrich", {"job_id": recording[1]["id"]}, "补齐文稿")
    monkeypatch.setattr(cli, "main", lambda args: 0)
    monkeypatch.setenv("AUDIO_NOTES_WORKBENCH_TASK", "1")
    assert execute_request(config, request["id"]) == 1
    assert store.requests()[0]["status"] == "failed"


def test_manual_scan_round_reports_summary_and_tolerates_syncing_files(config, monkeypatch):
    from audio_notes import cli

    store = WorkbenchStore(config.state)
    request = store.enqueue("scan", {}, "扫描")
    assert store.enqueue("scan", {}, "扫描")["id"] == request["id"]
    seen = []

    def fake_main(args):
        seen.append(args)
        atomic_json(
            config.state / "last-scan.json",
            {"copied": 2, "completed": 2, "failed": 0, "waiting": 1, "finished_at": time.time()},
        )
        return 3

    monkeypatch.setattr(cli, "main", fake_main)
    monkeypatch.setenv("AUDIO_NOTES_WORKBENCH_TASK", "1")
    assert execute_request(config, request["id"]) == 0
    assert seen[0][-3:] == ["run", "--wait-limit", str(config.processing["settle_seconds"] + 30)]
    row = store.requests()[0]
    assert row["status"] == "completed" and row["error"] is None
    assert "发现 2 条新录音" in row["summary"] and "1 条录音仍在同步" in row["summary"]


def test_scan_permission_denial_names_the_fix(config, monkeypatch):
    from audio_notes import cli

    store = WorkbenchStore(config.state)
    request = store.enqueue("scan", {}, "扫描")

    def fake_main(args):
        atomic_json(
            config.state / "voice-memos-status.json",
            {"errors": [{"path": "/x", "error": "denied", "permission_denied": True}]},
        )
        return 1

    monkeypatch.setattr(cli, "main", fake_main)
    monkeypatch.setenv("AUDIO_NOTES_WORKBENCH_TASK", "1")
    assert execute_request(config, request["id"]) == 1
    row = store.requests()[0]
    assert row["status"] == "failed" and "完全磁盘访问权限" in row["error"]


def test_retired_service_request_and_action_are_not_run(http, config, monkeypatch):
    from audio_notes import cli

    store = WorkbenchStore(config.state)
    request = store.enqueue("service", {}, "启用自动接收")
    monkeypatch.setattr(cli, "main", lambda args: pytest.fail("不应安装后台服务"))
    assert execute_request(config, request["id"]) == 0
    assert store.requests()[0]["status"] == "cancelled"
    request_api = http[0]
    assert request_api("/api/action", "POST", {"action": "service"})[0] >= 400
    assert json.loads(request_api("/api/library")[2])["legacy_service"] == {"removed": False}


def test_speaker_retry_can_be_queued_without_retranscribing(http):
    from dataclasses import replace

    request, app, _, key = http
    app.config = replace(app.config, speakers={**app.config.speakers, "enabled": True})
    code, _, result = request("/api/action", "POST", {"action": "diarize", "id": key})
    assert code == 200
    entry = next(row for row in app.store.requests() if row["id"] == json.loads(result)["id"])
    assert entry["kind"] == "diarize"
    assert json.loads(entry["payload"]) == {"job_id": key}


def test_launcher_reuses_authenticated_local_server_without_new_process(http, monkeypatch):
    from audio_notes import workbench

    _, app, server, _ = http
    atomic_json(app.config.state / "workbench-server.json", {"url": server.origin, "token": app.token})
    opened = []
    monkeypatch.setattr(workbench.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(workbench.subprocess, "Popen", lambda *a, **k: pytest.fail("不应重复启动服务"))
    workbench.open_workbench(app.config)
    assert opened == [server.origin + "/#token=" + app.token]


def test_native_connection_mode_returns_json_without_opening_browser(http, monkeypatch, capsys):
    from audio_notes import cli, workbench

    _, app, server, _ = http
    atomic_json(app.config.state / "workbench-server.json", {"url": server.origin, "token": app.token})
    monkeypatch.setattr(cli, "load_config", lambda path: app.config)
    monkeypatch.setattr(workbench.webbrowser, "open", lambda *a: pytest.fail("原生模式不能打开浏览器"))
    monkeypatch.setattr(workbench.subprocess, "Popen", lambda *a, **k: pytest.fail("服务应被复用"))
    assert cli.main(["workbench", "--url"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["url"] == server.origin and result["token"] == app.token


def test_failed_enrichment_request_exposes_specific_error(config, monkeypatch, recording):
    from audio_notes import cli
    key = recording[1]["id"]
    detail = "文稿生成失败：polish-1 两次生成仍未通过检查。原始转写已保留。"
    atomic_json(config.output / key / "enrichment.status.json", {"status": "failed", "error": detail})
    store = WorkbenchStore(config.state)
    request = store.enqueue("enrich", {"job_id": key}, "补齐文稿")
    monkeypatch.setattr(cli, "main", lambda args: 1)
    monkeypatch.setenv("AUDIO_NOTES_WORKBENCH_TASK", "1")
    assert execute_request(config, request["id"]) == 1
    assert store.requests()[0]["error"] == detail
