"""Delete, generated names, speaker renaming and agent context, on synthetic recordings."""

import json
import threading
from http.client import HTTPConnection

import pytest

from audio_notes import workbench_tools
from audio_notes.diarization import rename_speakers
from audio_notes.files import atomic_json, atomic_text
from audio_notes.pipeline import Pipeline
from audio_notes.titles import clean_title, display_name
from audio_notes.txt_exports import publish_txt
from audio_notes.workbench import Server, Workbench


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

    def request(path, method="GET", body=None):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        headers = {"Authorization": "Bearer " + app.token}
        if body is not None:
            body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    yield request, app, recording[1]["id"]
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_clean_title_normalises_model_output():
    assert clean_title("标题：「讨论预算方案与上线时间。」\n解释") == "讨论预算方案与上线时间"
    for bad in ("", "好的", "。。", "这" * 31):
        with pytest.raises(ValueError):
            clean_title(bad)


def test_rename_speakers_only_touches_numbered_labels():
    text = "说话人 1：你好\n说话人12：嗯\n说话人 2：好\n说话人待确认：？"
    renamed = rename_speakers(text, {"speaker_1": "张三", "speaker_12": "李四"})
    assert renamed == "张三：你好\n李四：嗯\n说话人 2：好\n说话人待确认：？"
    assert rename_speakers(text, {}) == text


def test_generated_name_renames_txt_and_removes_old_copy(config, recording):
    _, entry = recording
    directory = config.output / entry["id"]
    raw = json.loads((directory / "raw.json").read_text())
    old = list(config.txt_output.glob(f"*（{entry['id'][:12]}）.txt"))
    assert [p.name.startswith("会议") for p in old] == [True]
    atomic_json(directory / "title.json", {"title": "讨论预算方案尚未决定"})
    name = display_name(directory, raw)
    assert name.startswith("讨论预算方案尚未决定 2026-10-0")
    published = publish_txt(config, raw, directory)
    assert published.name.startswith("讨论预算方案尚未决定 2026-10-0")
    assert "：" in published.name and ":" not in published.name
    assert list(config.txt_output.glob(f"*（{entry['id'][:12]}）.txt")) == [published]


def test_library_title_prefers_custom_then_generated(http, config):
    request, app, key = http
    atomic_json(config.output / key / "title.json", {"title": "讨论预算方案尚未决定"})
    item = json.loads(request("/api/library")[1])["items"][0]
    assert item["title"].startswith("讨论预算方案尚未决定 ") and item["title_source"] == "generated"
    request("/api/action", "POST", {"action": "metadata", "id": key, "changes": {"title": "自定"}})
    item = json.loads(request("/api/library")[1])["items"][0]
    assert item["title"] == "自定" and item["title_source"] == "custom"
    assert [p.name for p in config.txt_output.iterdir()] == [f"自定（{key[:12]}）.txt"]


def test_speaker_rename_syncs_documents_downloads_and_txt(http, config):
    request, app, key = http
    directory = config.output / key
    atomic_text(directory / "polished.md", "# 稿\n\n**说话人 1**\n\n预算。\n\n说话人 2：好。\n")
    atomic_text(directory / "transcript.simplified.txt", "说话人 1：预算3000元。\n")
    code, body = request(
        "/api/action",
        "POST",
        {"action": "rename_speaker", "id": key, "speaker": "speaker_1", "name": "张三"},
    )
    assert code == 200 and json.loads(body)["speakers"] == {"speaker_1": "张三"}
    text = json.loads(request(f"/api/document?id={key}&name=polished.md")[1])["text"]
    assert "**张三**" in text and "说话人 2" in text
    assert "张三" in request(f"/api/download?id={key}&name=polished.md")[1].decode()
    assert (directory / "polished.md").read_text().count("说话人 1") == 1  # disk untouched
    assert "张三：预算" in next(config.txt_output.iterdir()).read_text()
    bad = {"action": "rename_speaker", "id": key, "speaker": "speaker_1", "name": "a*b"}
    assert request("/api/action", "POST", bad)[0] == 400
    request("/api/action", "POST", {**bad, "name": ""})
    assert "说话人 1" in json.loads(request(f"/api/document?id={key}&name=polished.md")[1])["text"]


def test_agent_context_contains_transcript(http):
    request, app, key = http
    code, body = request(f"/api/agent-context?id={key}&part=transcript")
    text = json.loads(body)["text"]
    assert code == 200 and "## 完整转写" in text and "预算3000元" in text
    assert request(f"/api/agent-context?id={key}&part=transcript&download=1")[0] == 200


def test_delete_keeps_audio_and_prevents_rescan(http, config, recording, fake_worker):
    request, app, key = http
    source, _ = recording
    code, body = request(
        "/api/action", "POST", {"action": "delete", "id": key, "delete_audio": False}
    )
    assert code == 200 and json.loads(body)["trashed"] == 0
    assert not (config.output / key).exists() and source.is_file()
    assert not list(config.txt_output.iterdir())
    snapshot = json.loads(request("/api/library")[1])
    assert snapshot["items"] == [] and snapshot["backlog"] == []
    pipeline = Pipeline(config, "test-profile", worker=fake_worker)
    calls = len(fake_worker.calls)
    pipeline.scan()
    assert len(fake_worker.calls) == calls and pipeline.store.completed() == []
    # Explicitly choosing the file again (manual transcription) restores it.
    pipeline.scan(sources=[source])
    assert len(pipeline.store.completed()) == 1
    pipeline.close()


def test_delete_with_audio_moves_source_to_trash(http, config, recording, monkeypatch):
    request, app, key = http
    source, _ = recording
    trashed = []
    monkeypatch.setattr(workbench_tools, "move_to_trash", lambda path: trashed.append(path))
    code, body = request(
        "/api/action", "POST", {"action": "delete", "id": key, "delete_audio": True}
    )
    assert code == 200 and json.loads(body)["trashed"] == 1 and trashed == [source]
    assert (
        request("/api/action", "POST", {"action": "delete", "id": key, "delete_audio": True})[0]
        == 404
    )


def test_delete_refuses_pending_requests(http):
    request, app, key = http
    app.store.enqueue("enrich", {"job_id": key, "force": False}, "补齐")
    code, body = request(
        "/api/action", "POST", {"action": "delete", "id": key, "delete_audio": False}
    )
    assert code == 400 and "取消" in json.loads(body)["error"]
