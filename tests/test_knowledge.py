"""PRD acceptance tests on synthetic recordings only."""

from dataclasses import replace

import pytest

from audio_notes.automatic import Watcher, allowed
from audio_notes.files import atomic_json, sha256_file
from audio_notes.knowledge import Knowledge, revised_paragraph
from audio_notes.knowledge_api import knowledge_action, knowledge_read, match_voices
from audio_notes.pipeline import Pipeline
from audio_notes.retrieval import validate_retrieval
from audio_notes.workbench import Workbench
from audio_notes.workbench_library import detail
from audio_notes.workbench_store import Conflict


@pytest.fixture
def app(config, fake_worker):
    source = config.inbox / "例子.wav"
    source.write_text("先确认阿里云。阿里云的预算还没决定。周五提交草稿。")
    pipeline = Pipeline(config, "test", worker=fake_worker)
    pipeline.scan()
    entry = pipeline.store.completed()[0]
    pipeline.close()
    app = Workbench(config)
    app.test_id = entry["id"]
    return app


def act(app, action, **data):
    handled, result = knowledge_action(app, dict(action=action, **data))
    assert handled
    return result


def test_revision_undo_preserves_raw_and_exact_occurrence(app):
    path = app.config.output / app.test_id / "raw.json"
    digest = sha256_file(path)
    d = detail(app.config, app.store, app.test_id)
    offset = d["segments"][0]["text"].rfind("阿里云")
    r = act(
        app,
        "revise",
        id=app.test_id,
        segment=1,
        old="阿里云",
        new="阿里雲",
        scope="one",
        offset=offset,
    )
    updated = detail(app.config, app.store, app.test_id)
    assert updated["segments"][0]["text"].startswith("先确认阿里云。阿里雲")
    assert sha256_file(path) == digest
    act(app, "undo_revision", id=app.test_id, revision_id=r["id"])
    assert (
        detail(app.config, app.store, app.test_id)["segments"][0]["text"]
        == d["segments"][0]["text"]
    )


def test_null_word_timestamps_allow_details_search_and_tasks(app):
    import json

    path = app.config.output / app.test_id / "raw.json"
    raw = json.loads(path.read_text())
    raw["segments"][0]["words"] = None
    atomic_json(path, raw)
    d = detail(app.config, app.store, app.test_id)
    assert d["segments"][0]["confidence"] is None
    assert knowledge_read(app, {"kind": "search", "query": "预算"})["items"]
    assert knowledge_read(app, {"kind": "tasks"}) == {"items": []}


def test_source_correction_preserves_polished_paragraphs_and_undo(app):
    directory = app.config.output / app.test_id
    fingerprint = "a" * 64
    original = detail(app.config, app.store, app.test_id)["segments"][0]["text"]
    pieces = ["先确认阿里云。", "阿里云的预算还没决定。", "周五提交草稿。"]
    polished = ["先确认阿里云。", "阿里云的预算尚未决定。", "请于周五提交草稿。"]
    assert "".join(pieces) == original
    atomic_json(directory / "enrichment.complete.json", {"fingerprint": fingerprint})
    atomic_json(directory / "enrichment" / fingerprint / "result.json", {
        "units": [{"id": i, "segment": 1, "text": text}
                  for i, text in enumerate(pieces, 1)],
        "polished": [{"paragraphs": [{"text": text, "unit_ids": [i]}
                                     for i, text in enumerate(polished, 1)]}],
        "analysis": None,
    })
    revision = act(app, "revise", id=app.test_id, segment=1,
                   old="预算", new="经费", scope="one")
    d = detail(app.config, app.store, app.test_id)
    assert [p["text"] for p in d["smart"]["body"]] == [
        polished[0], "阿里云的经费尚未决定。", polished[2],
    ]
    act(app, "undo_revision", id=app.test_id, revision_id=revision["id"])
    assert [p["text"] for p in detail(app.config, app.store, app.test_id)["smart"]["body"]] == polished


def test_source_correction_distinguishes_repeated_words_across_speakers():
    units = [{"id": 1, "segment": 1, "text": "预算未定。"},
             {"id": 2, "segment": 2, "text": "预算未定。"}]
    segments = {1: {"text": "预算未定。"},
                2: {"text": "经费未定。", "revised": True}}
    paragraph = {"text": "预算未定。预算未定。", "unit_ids": [1, 2]}
    assert revised_paragraph(paragraph, units, segments) == "预算未定。经费未定。"


def test_source_correction_keeps_model_rewrites_without_an_exact_match():
    units = [{"id": 1, "segment": 1, "text": "预算还没决定。"}]
    segments = {1: {"text": "经费还没决定。", "revised": True}}
    paragraph = {"text": "尚未确定开支。", "unit_ids": [1]}
    assert revised_paragraph(paragraph, units, segments) == paragraph["text"]


def test_dependent_revisions_must_undo_in_reverse_order(app):
    a = act(app, "revise", id=app.test_id, segment=1, old="预算", new="经费", scope="one")
    b = act(app, "revise", id=app.test_id, segment=1, old="经费", new="费用", scope="one")
    with pytest.raises(Conflict):
        act(app, "undo_revision", id=app.test_id, revision_id=a["id"])
    act(app, "undo_revision", id=app.test_id, revision_id=b["id"])
    assert "经费" in detail(app.config, app.store, app.test_id)["segments"][0]["text"]


def test_glossary_batch_requires_current_preview(app):
    act(app, "term", term={"correct": "费用", "wrong": ["预算"]})
    preview = act(app, "preview_terms")
    assert len(preview["hits"]) == 1
    act(app, "revise", id=app.test_id, segment=1, old="草稿", new="初稿", scope="one")
    with pytest.raises(ValueError, match="变化"):
        act(app, "apply_terms", token=preview["token"])
    preview = act(app, "preview_terms")
    assert act(app, "apply_terms", token=preview["token"])["count"] == 1
    assert "费用" in detail(app.config, app.store, app.test_id)["segments"][0]["text"]


def test_dictionary_import_validates_before_writing(app):
    with pytest.raises(ValueError):
        act(
            app, "import_terms", terms=[{"correct": "A", "wrong": []}, {"correct": "", "wrong": []}]
        )
    assert knowledge_read(app, {"kind": "terms"})["items"] == []


def test_hotwords_are_applied_to_future_asr(config, fake_worker):
    Knowledge(config.state).term({"correct": "听记", "wrong": ["听季"]})
    source = config.inbox / "hotwords.wav"
    source.write_text("听季")
    captured = []

    def worker(config, request, log):
        captured.append(request["settings"]["initial_prompt"])
        fake_worker(config, request, log)

    p = Pipeline(config, "test", worker=worker)
    try:
        p.scan()
    finally:
        p.close()
    assert "听记" in captured[0] and "听季" in captured[0]


@pytest.mark.parametrize(
    "mode,ac,idle,expected",
    [
        ("saving", False, True, False),
        ("saving", True, False, False),
        ("saving", True, True, True),
        ("balanced", False, False, False),
        ("balanced", True, False, True),
        ("balanced", False, True, True),
        ("instant", False, False, True),
    ],
)
def test_power_policies(mode, ac, idle, expected):
    assert allowed(mode, ac, idle) is expected


def test_watch_startup_change_and_manual_mode(config, monkeypatch):
    watched = config.inbox / "apple"
    watched.mkdir()
    config = replace(config, voice_memos={"enabled": True, "directories": [str(watched)]})
    k = Knowledge(config.state)
    k.preferences({"timing": "instant"})
    monkeypatch.setattr("audio_notes.automatic.power_and_idle", lambda: (False, False))
    watcher = Watcher(config, k.store)
    watcher.tick()
    assert len(k.store.requests()) == 1
    first = k.store.requests()[0]["id"]
    k.store.finish(first, "completed")
    watcher.last_check = 0
    watcher.tick()
    assert len(k.store.requests()) == 1
    (watched / "new.m4a").write_bytes(b"new")
    watcher.last_check = 0
    watcher.tick()
    assert len(k.store.requests()) == 2
    k.preferences({"automatic": False})
    (watched / "newer.m4a").write_bytes(b"newer")
    watcher.last_check = 0
    watcher.tick()
    assert len(k.store.requests()) == 2


def add_generation(app):
    directory = app.config.output / app.test_id
    fingerprint = "a" * 64
    atomic_json(directory / "enrichment.complete.json", {"fingerprint": fingerprint})
    atomic_json(
        directory / "enrichment" / fingerprint / "result.json",
        {
            "units": [{"id": 1, "segment": 1}],
            "polished": [],
            "analysis": {
                "brief": {"text": "讨论预算与草稿", "source_ids": [1]},
                "actions": [
                    {
                        "text": "周五提交草稿。",
                        "source_ids": [1],
                        "owner": "张三",
                        "deadline": "周五",
                    }
                ],
                "questions": [],
                "themes": [],
            },
        },
    )


def test_tasks_are_grounded_and_user_changes_survive_generation(app):
    add_generation(app)
    tasks = knowledge_read(app, {"kind": "tasks"})["items"]
    assert len(tasks) == 1 and tasks[0]["owner"] == "" and tasks[0]["deadline"] == "周五"
    assert "草稿" in tasks[0]["sources"][0]["text"]
    act(app, "task", task={"id": tasks[0]["id"], "text": "我修改的任务", "status": "done"})
    again = knowledge_read(app, {"kind": "tasks"})["items"]
    assert len(again) == 1 and again[0]["text"] == "我修改的任务" and again[0]["status"] == "done"


def test_full_text_search_reaches_beyond_preview_and_filters(app):
    hits = knowledge_read(app, {"kind": "search", "query": "草稿"})["items"]
    assert hits and hits[0]["start"] == 0
    assert not knowledge_read(app, {"kind": "search", "query": "草稿", "favorite": "1"})["items"]
    app.store.update(detail(app.config, app.store, app.test_id)["audio_hash"], {"favorite": True})
    assert knowledge_read(app, {"kind": "search", "query": "草稿", "favorite": "1"})["items"]
    assert not knowledge_read(app, {"kind": "search", "query": "草稿", "ids": "unknown"})["items"]


def test_grounded_answer_rejects_invented_quote_or_source():
    sources = [{"text": "这是提议，尚未决定。"}]
    validate_retrieval({"claims": [{"source": 0, "quote": "尚未决定"}]}, "answer", sources)
    for claim in ({"source": 0, "quote": "已经决定"}, {"source": 1, "quote": "尚未决定"}):
        with pytest.raises(ValueError):
            validate_retrieval({"claims": [claim]}, "answer", sources)
    validate_retrieval({"claims": []}, "answer", sources)


def test_matching_voice_thresholds_and_deletion(app):
    k = Knowledge(app.config.state)
    k.put(
        "people",
        "person",
        {"id": "person", "name": "老王", "voices": [{"source": "test", "vector": [1.0, 0.0]}]},
    )
    d = detail(app.config, app.store, app.test_id)
    match_voices(
        app.config,
        app.test_id,
        d["audio_hash"],
        {"speaker_1": [1.0, 0.0], "speaker_2": [0.7, 0.7], "speaker_3": [0.0, 1.0]},
    )
    names = app.store.metadata()[d["audio_hash"]]["speakers"]
    assert names["speaker_1"] == "老王"
    assert names["speaker_2"] == "可能是 老王？"
    assert "speaker_3" not in names
    act(app, "delete_person", person_id="person")
    assert not k.all("people") and not k.all("members")


def test_suggested_voice_label_fits_long_person_names(app):
    name = "张" * 40
    Knowledge(app.config.state).put(
        "people", "long", {"id": "long", "name": name, "voices": [{"source": "x", "vector": [1.0, 0.0]}]}
    )
    d = detail(app.config, app.store, app.test_id)
    match_voices(app.config, app.test_id, d["audio_hash"], {"speaker_1": [0.7, 0.7]})
    label = app.store.metadata()[d["audio_hash"]]["speakers"]["speaker_1"]
    assert label.startswith("可能是 张") and label.endswith("？") and len(label) <= 40


def test_review_invalidates_when_text_changes(app):
    path = app.config.output / app.test_id / "raw.json"
    import json

    raw = json.loads(path.read_text())
    raw["segments"][0]["words"] = [{"word": "预算", "probability": 0.2}]
    atomic_json(path, raw)
    d = detail(app.config, app.store, app.test_id)
    assert d["review"]["total"] == 1
    s = d["segments"][0]
    act(app, "review_segment", id=app.test_id, segment=1, signature=s["review_signature"])
    assert detail(app.config, app.store, app.test_id)["review"]["complete"]
    act(app, "revise", id=app.test_id, segment=1, old="草稿", new="初稿", scope="one")
    assert not detail(app.config, app.store, app.test_id)["review"]["complete"]


def test_invalid_scene_does_not_enqueue(app):
    with pytest.raises(ValueError):
        act(app, "scene", id=app.test_id, scene="invented")
    assert not app.store.requests()


def test_body_correction_is_separate_from_original(app):
    add_generation(app)
    original = detail(app.config, app.store, app.test_id)["segments"][0]["text"]
    r = act(
        app, "revise", id=app.test_id, segment=1, view="body", old="预算", new="费用", scope="one"
    )
    d = detail(app.config, app.store, app.test_id)
    assert "费用" in d["smart"]["body"][0]["text"]
    assert d["segments"][0]["text"] == original
    act(app, "undo_revision", id=app.test_id, revision_id=r["id"])
    assert "预算" in detail(app.config, app.store, app.test_id)["smart"]["body"][0]["text"]


def test_invalid_glossary_change_is_not_partially_saved(app):
    with pytest.raises(ValueError):
        act(app, "revise", id=app.test_id, segment=1, old="预算", new="长" * 101, scope="glossary")
    assert not Knowledge(app.config.state).all("revisions")


def test_semantic_phrase_expansion_keeps_content_words():
    from audio_notes.retrieval import retrieval_terms

    terms = retrieval_terms(["预算确认", "预算金额"])
    assert "预算" in terms


def test_estimates_learn_local_speed_and_include_queue(config):
    from audio_notes.estimates import estimates, record

    for n in range(3):
        record(config, str(n), "asr", 100, 50)
    items = [
        {
            "id": str(n),
            "created": n,
            "duration": 100,
            "asr_status": "pending",
            "speaker_status": "disabled",
            "text_status": "disabled",
        }
        for n in range(2)
    ]
    estimates(config, items)
    assert items[0]["estimates"]["asr"]["seconds"] == 50
    assert items[1]["estimates"]["asr"]["seconds"] == 100
    assert not items[1]["estimates"]["asr"]["rough"]


def test_damaged_audio_has_no_estimate_instead_of_breaking_library(tmp_path, monkeypatch):
    import av

    from audio_notes.estimates import audio_duration

    def damaged(*args, **kwargs):
        raise av.error.EOFError(-541478725, "End of file")

    monkeypatch.setattr(av, "open", damaged)
    path = tmp_path / "half-synced.m4a"
    path.write_bytes(b"partial")
    assert audio_duration(path) is None


def test_overdue_estimate_never_negative(config):
    import time

    from audio_notes.estimates import estimates

    items = [
        {
            "id": "test",
            "created": 0,
            "updated": time.time() - 600,
            "duration": 10,
            "asr_status": "running",
            "speaker_status": "disabled",
            "text_status": "disabled",
        }
    ]
    estimates(config, items)
    assert items[0]["estimates"]["asr"]["overdue"]
    assert items[0]["estimates"]["asr"]["seconds"] >= 0


def test_new_files_arriving_during_scan_are_not_lost(config, monkeypatch):
    watched = config.inbox / "apple"
    watched.mkdir()
    config = replace(config, voice_memos={"enabled": True, "directories": [str(watched)]})
    k = Knowledge(config.state)
    k.preferences({"timing": "instant"})
    monkeypatch.setattr("audio_notes.automatic.power_and_idle", lambda: (True, True))
    watcher = Watcher(config, k.store)
    watcher.tick()
    first = k.store.pending()["id"]
    k.store.claim(first)
    (watched / "late.m4a").write_bytes(b"late")
    watcher.last_check = 0
    watcher.tick()
    assert watcher.dirty
    k.store.finish(first, "completed")
    watcher.last_check = 0
    watcher.tick()
    assert k.store.pending()["id"] != first


def test_manual_scan_can_override_waiting_automatic_scan(config):
    app = Workbench(config)
    queued = app.store.enqueue("scan", {"automatic": True}, "自动")
    result = app.action({"action": "scan"})
    assert result["id"] != queued["id"]
    assert next(r for r in app.store.requests() if r["id"] == queued["id"])["status"] == "cancelled"


def test_regeneration_removes_only_untouched_obsolete_tasks(app):
    add_generation(app)
    tasks = knowledge_read(app, {"kind": "tasks"})["items"]
    assert tasks
    path = app.config.output / app.test_id / "enrichment" / ("a" * 64) / "result.json"
    import json

    result = json.loads(path.read_text())
    result["analysis"]["actions"] = []
    atomic_json(path, result)
    assert not knowledge_read(app, {"kind": "tasks"})["items"]
    add_generation(app)
    task = knowledge_read(app, {"kind": "tasks"})["items"][0]
    act(app, "task", task={"id": task["id"], "status": "done"})
    atomic_json(path, result)
    retained = knowledge_read(app, {"kind": "tasks"})["items"]
    assert len(retained) == 1 and retained[0]["status"] == "done"
