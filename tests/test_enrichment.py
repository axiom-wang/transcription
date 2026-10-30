import copy
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from audio_notes import enrichment as module
from audio_notes.enrichment import (
    chunks_for,
    delivery_valid,
    enrich_completed,
    enrich_one,
    fingerprint,
    polish_chunks_for,
    units_for,
    validate_analysis,
    validate_polish,
)
from audio_notes.files import atomic_json, sha256_file
from audio_notes.pipeline import Pipeline, exports_valid


def analysis(ids):
    return {
        "brief": {"text": "讨论项目提议，尚未做出决定。", "source_ids": ids},
        "highlights": [{"text": "尚未决定。", "source_ids": ids}],
        "themes": [],
        "insights": [],
        "actions": [],
        "questions": [],
        "suggestions": [],
    }


def fake_text_worker(config, request, request_path, worker_log):
    chunks = polish_chunks_for(request["units"], config.analysis["chunk_chars"])
    polished = [
        {
            "paragraphs": [{"unit_ids": [u["id"]], "text": u["text"]} for u in chunk],
            "corrections": [],
            "uncertainties": [],
        }
        for chunk in chunks
    ]
    atomic_json(
        Path(request["work"]) / "result.json",
        {
            "fingerprint": request["fingerprint"],
            "units": request["units"],
            "polished": polished,
            "analysis": analysis(sorted({u["segment"] for u in request["units"]}))
            if request["units"]
            else None,
        },
    )


@pytest.fixture
def document(config, fake_worker):
    source = config.inbox / "sample.wav"
    source.write_text("预算3000元。只是提议，尚未决定。")
    pipeline = Pipeline(config, "profile", worker=fake_worker)
    pipeline.scan()
    jobs = pipeline.store.completed()
    pipeline.close()
    directory = Path(jobs[0]["output"])
    enabled = replace(config, analysis={**config.analysis, "enabled": True})
    return enabled, directory, jobs


def test_coverage_and_numbers_are_required():
    units = [
        {"id": 1, "segment": 1, "text": "预算3000元。"},
        {"id": 2, "segment": 2, "text": "不是30000元。只是提议，尚未决定。"},
    ]
    result = {
        "paragraphs": [{"unit_ids": [1, 2], "text": "".join(u["text"] for u in units)}],
        "corrections": [],
        "uncertainties": [],
    }
    validate_polish(result, units)
    for altered in (
        {"unit_ids": [1], "text": "预算3000元。"},
        {"unit_ids": [1, 2, 2], "text": result["paragraphs"][0]["text"]},
        {"unit_ids": [2, 1], "text": result["paragraphs"][0]["text"]},
        {"unit_ids": [1, 2], "text": result["paragraphs"][0]["text"].replace("3000", "4000")},
        {"unit_ids": [1, 2], "text": result["paragraphs"][0]["text"] + "5个人"},
        {"unit_ids": [1, 2], "text": "3000、30000"},
    ):
        with pytest.raises(ValueError):
            validate_polish({**result, "paragraphs": [altered]}, units)


def test_analysis_rejects_fabricated_citations_and_missing_action_fields():
    result = analysis([1])
    validate_analysis(result, {1})
    result["brief"]["source_ids"] = [99]
    with pytest.raises(ValueError):
        validate_analysis(result, {1})
    result = analysis([1])
    result["actions"] = [{"text": "提议上线", "source_ids": [1]}]
    with pytest.raises(KeyError):
        validate_analysis(result, {1})


def test_chunking_does_not_drop_oversized_or_empty_segments():
    raw = {"segments": [{"text": "這是資料。" * 1000}, {"text": " "}, {"text": "第二段"}]}
    units = units_for(raw)
    chunks = chunks_for(units, 2000)
    assert "".join(u["text"] for c in chunks for u in c) == "这是资料。" * 1000 + "第二段"
    assert all(len(u["text"]) <= 1600 for u in units)
    assert units[-1]["segment"] == 3
    assert [u["id"] for c in chunks for u in c] == list(range(1, len(units) + 1))


def test_delivery_preserves_asr_and_repairs_exports_from_cache(document):
    config, directory, jobs = document
    before = {p.name: sha256_file(p) for p in directory.iterdir() if p.is_file()}
    manifest = {"model": "synthetic"}
    assert enrich_one(config, directory, manifest, worker=fake_text_worker)
    key = fingerprint(config, directory / "raw.json", manifest)
    assert delivery_valid(directory, key)
    assert not enrich_one(config, directory, manifest, worker=lambda *a: pytest.fail("重复生成"))
    (directory / "polished.md").write_text("损坏")
    assert enrich_one(config, directory, manifest, worker=lambda *a: pytest.fail("应复用缓存"))
    assert delivery_valid(directory, key)
    assert before == {name: sha256_file(directory / name) for name in before}
    assert "transcript.simplified.md#segment-1" in (directory / "analysis.md").read_text()
    assert "3000" in (directory / "polished.txt").read_text()
    assert exports_valid(directory, config.markdown)


def test_failure_does_not_change_asr_and_cooldown_allows_other_jobs(document, monkeypatch):
    config, directory, jobs = document
    monkeypatch.setattr(module, "check_text_model", lambda *a, **k: {"model": "synthetic"})
    calls = []

    def broken(*args):
        calls.append(True)
        raise RuntimeError("模拟文本模型失败")

    before = sha256_file(directory / "raw.json")
    assert enrich_completed(config, jobs, worker=broken) == 0
    assert json.loads((directory / "enrichment.status.json").read_text())["status"] == "failed"
    assert enrich_completed(config, jobs, worker=broken) == 0
    assert len(calls) == 1
    assert before == sha256_file(directory / "raw.json")
    assert exports_valid(directory, config.markdown)
    assert not (directory / "enrichment.complete.json").exists()
    assert enrich_completed(config, jobs, force=True, worker=fake_text_worker) == 1


def test_model_correction_log_must_match_source_and_revision():
    units = [{"id": 1, "segment": 1, "text": "这个项木还没有决定。"}]
    result = {
        "paragraphs": [{"unit_ids": [1], "text": "这个项目还没有决定。"}],
        "corrections": [
            {"unit_ids": [1], "original": "项木", "revised": "项目", "reason": "上下文"}
        ],
        "uncertainties": [],
    }
    validate_polish(result, units)
    bad = copy.deepcopy(result)
    bad["corrections"][0]["original"] = "原文未说"
    with pytest.raises(ValueError):
        validate_polish(bad, units)


def test_editing_raw_during_inference_does_not_publish(document):
    config, directory, jobs = document

    def changed(*args):
        fake_text_worker(*args)
        raw = json.loads((directory / "raw.json").read_text())
        raw["segments"][0]["text"] = "发生变化"
        atomic_json(directory / "raw.json", raw)

    with pytest.raises(ValueError, match="发生变化"):
        enrich_one(config, directory, {}, worker=changed)
    assert not (directory / "polished.md").exists()


def test_generated_markdown_does_not_create_model_supplied_links(document):
    config, directory, jobs = document

    def injected(*args):
        fake_text_worker(*args)
        path = Path(args[1]["work"]) / "result.json"
        result = json.loads(path.read_text())
        result["analysis"]["themes"] = [
            {
                "title": "[恶意链接](https://example.com) <script>",
                "points": [{"text": "预算3000元。", "source_ids": [1]}],
            }
        ]
        atomic_json(path, result)

    enrich_one(config, directory, {}, worker=injected)
    text = (directory / "analysis.md").read_text()
    assert "\\[恶意链接\\]" in text
    assert "<script>" not in text


def test_corrupt_result_cache_is_retained_and_rebuilt(document):
    config, directory, jobs = document
    enrich_one(config, directory, {}, worker=fake_text_worker)
    cached = next(directory.glob("enrichment/*/result.json"))
    cached.write_text("{broken")
    (directory / "analysis.md").unlink()
    assert enrich_one(config, directory, {}, worker=fake_text_worker)
    assert list(cached.parent.glob("result.invalid-*.json"))
    assert (directory / "analysis.md").is_file()


def test_automatic_stage_runs_on_completed_asr_without_retranscribing(
    config, fake_worker, monkeypatch
):
    source = config.inbox / "selected.wav"
    source.write_text("只是提议。")
    p = Pipeline(config, "profile", worker=fake_worker)
    p.scan()
    calls = []
    monkeypatch.setattr(
        module, "enrich_completed", lambda config, jobs, **kw: calls.append((kw["phase"], len(jobs)))
    )
    p.config = replace(config, analysis={**config.analysis, "enabled": True})
    p.scan()
    assert len(fake_worker.calls) == 1
    assert calls == [("new", 1), ("retry", 1)]
    p.close()


def test_new_recordings_finish_every_stage_before_old_failures_retry(
    config, fake_worker, monkeypatch
):
    (config.inbox / "selected.wav").write_text("只是提议。")
    config = replace(
        config,
        analysis={**config.analysis, "enabled": True},
        speakers={**config.speakers, "enabled": True},
    )
    p = Pipeline(config, "profile", worker=fake_worker)
    calls = []
    monkeypatch.setattr(
        "audio_notes.diarization.diarize_completed",
        lambda config, jobs, **kw: calls.append(("speaker", kw["phase"])),
    )
    monkeypatch.setattr(
        module, "enrich_completed", lambda config, jobs, **kw: calls.append(("text", kw["phase"]))
    )
    p.scan()
    assert calls == [
        ("speaker", "new"),
        ("text", "new"),
        ("speaker", "retry"),
        ("text", "retry"),
    ]
    p.close()


def test_failed_delivery_waits_for_the_retry_phase_and_backs_off(document, monkeypatch):
    config, directory, jobs = document
    monkeypatch.setattr(module, "check_text_model", lambda *a, **k: {"model": "synthetic"})
    status_path = directory / "enrichment.status.json"
    calls = []

    def broken(*args):
        calls.append(True)
        raise RuntimeError("模拟文本模型失败")

    def age(seconds):
        status = json.loads(status_path.read_text())
        atomic_json(status_path, {**status, "updated": status["updated"] - seconds})

    enrich_completed(config, jobs, worker=broken, phase="new")
    assert len(calls) == 1
    assert json.loads(status_path.read_text())["attempts"] == 1
    age(10_000)
    enrich_completed(config, jobs, worker=broken, phase="new")
    assert len(calls) == 1
    enrich_completed(config, jobs, worker=broken, phase="retry")
    assert len(calls) == 2
    assert json.loads(status_path.read_text())["attempts"] == 2
    # The second failure waits twice the base interval.
    age(config.analysis["retry_seconds"])
    enrich_completed(config, jobs, worker=broken, phase="retry")
    assert len(calls) == 2
    age(config.analysis["retry_seconds"])
    enrich_completed(config, jobs, worker=broken, phase="retry")
    assert len(calls) == 3
    monkeypatch.setattr("audio_notes.control.queued_work", lambda config: True)
    age(10_000)
    enrich_completed(config, jobs, worker=broken, phase="retry")
    assert len(calls) == 3
    monkeypatch.setattr("audio_notes.control.queued_work", lambda config: False)
    for _ in range(4):
        age(100_000)
        enrich_completed(config, jobs, worker=broken, phase="retry")
    # Automatic attempts stop at the cap; an explicit retry still runs.
    status = json.loads(status_path.read_text())
    assert len(calls) == 5 and status["attempts"] == 5 and "手动重试" in status["error"]
    assert enrich_completed(config, jobs, retry_now=True, worker=fake_text_worker) == 1
    status = json.loads(status_path.read_text())
    assert status["status"] == "completed" and "attempts" not in status


def test_markers_written_before_counting_retry_once_more():
    from audio_notes.control import failed_attempts, failed_status, retry_due

    legacy = {"status": "failed", "fingerprint": "k", "error": "旧错误", "updated": 0}
    assert failed_attempts(legacy, "k") == 1
    assert retry_due(legacy, 600, "k")
    assert failed_status(legacy, RuntimeError("再次失败"), "k")["attempts"] == 2
    # A changed input starts over instead of inheriting the old failures.
    assert failed_attempts(legacy, "other") == 0
    assert retry_due(legacy, 600, "other")
    interrupted = {"status": "running", "fingerprint": "k", "attempts": 3, "updated": 0}
    assert failed_attempts(interrupted, "k") == 3
    assert retry_due(interrupted, 600, "k")


def test_force_generation_is_used_for_future_repairs(document):
    config, directory, jobs = document
    enrich_one(config, directory, {}, worker=fake_text_worker)

    def new_revision(*args):
        fake_text_worker(*args)
        path = Path(args[1]["work"]) / "result.json"
        data = json.loads(path.read_text())
        data["analysis"]["themes"] = [
            {"title": "新的分析版本", "points": [{"text": "预算3000元。", "source_ids": [1]}]}
        ]
        atomic_json(path, data)

    enrich_one(config, directory, {}, force=True, worker=new_revision)
    (directory / "analysis.md").unlink()
    enrich_one(config, directory, {}, worker=lambda *a: pytest.fail("应复用最新版本"))
    assert "新的分析版本" in (directory / "analysis.md").read_text()
    assert list(directory.glob("enrichment/*/result.previous-*.json"))


def test_empty_recording_does_not_invent_analysis(document):
    config, directory, jobs = document
    raw = json.loads((directory / "raw.json").read_text())
    raw["segments"] = []
    raw["text"] = ""
    atomic_json(directory / "raw.json", raw)
    enrich_one(config, directory, {}, worker=fake_text_worker)
    assert "未识别到可供分析" in (directory / "analysis.md").read_text()
    assert (directory / "polished.txt").read_text() == "\n"


def test_shared_gpu_lock_survives_parent_descriptor_close(tmp_path):
    import subprocess
    import sys

    from audio_notes.files import model_lock

    probe = """import fcntl,sys
with open(sys.argv[1], 'a+') as f:
 try:
  fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
 except BlockingIOError:
  sys.exit(7)
"""
    with model_lock(tmp_path) as handle:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(60)"], pass_fds=(handle.fileno(),)
        )
    try:
        assert (
            subprocess.run(
                [sys.executable, "-c", probe, str(tmp_path / ".inference.lock")]
            ).returncode
            == 7
        )
    finally:
        child.terminate()
        child.wait(timeout=5)
    assert (
        subprocess.run([sys.executable, "-c", probe, str(tmp_path / ".inference.lock")]).returncode
        == 0
    )


@pytest.mark.parametrize("fragment", [None, "天", "不", "3000元"])
def test_long_recording_worker_covers_all_chunks_and_merges_original_evidence(
    tmp_path, monkeypatch, fragment
):
    import sys
    from types import ModuleType, SimpleNamespace

    from audio_notes.text_worker import process

    seen = []
    tokenizer = SimpleNamespace(
        apply_chat_template=lambda messages, **kw: messages[-1]["content"],
        encode=lambda text: list(map(ord, text)),
    )
    mx = ModuleType("mlx.core")
    mx.set_cache_limit = mx.set_memory_limit = mx.clear_cache = lambda *a: None
    mx.random = SimpleNamespace(seed=lambda *a: None)
    mlx = ModuleType("mlx")
    mlx.core = mx
    lm = ModuleType("mlx_lm")
    lm.load = lambda *a, **k: (None, tokenizer)

    def generate(model, tokenizer, prompt, **kwargs):
        prompt = "".join(map(chr, prompt))
        if prompt.startswith(module.POLISH):
            body = prompt.split("\n", 1)[1]
            seen.append(("polish", body))
            yield SimpleNamespace(
                text=body,
                finish_reason="stop",
                prompt_tokens=len(prompt),
                generation_tokens=100,
                generation_tps=10,
                peak_memory=0,
            )
            return
        payload = json.loads(prompt.rsplit("\n", 1)[-1])
        if isinstance(payload, dict) and "draft" in payload:
            result = {"approved": True, "issues": []}
            seen.append(("review", payload))
        elif "每个unit" in prompt:
            result = {
                "paragraphs": [{"unit_ids": [u["id"]], "text": u["text"]} for u in payload],
                "corrections": [],
                "uncertainties": [],
            }
            seen.append(("polish", payload))
        elif "brief" in payload[0]:
            result = analysis(sorted({i for s in payload for i in s["brief"]["source_ids"]}))
            seen.append(("merge", payload))
        else:
            result = analysis(sorted({u["segment"] for u in payload}))
            seen.append(("analyze", payload))
        yield SimpleNamespace(
            text=json.dumps(result, ensure_ascii=False),
            finish_reason="stop",
            prompt_tokens=len(prompt),
            generation_tokens=100,
            generation_tps=10,
            peak_memory=0,
        )

    lm.stream_generate = generate
    sampling = ModuleType("mlx_lm.sample_utils")
    sampling.make_logits_processors = sampling.make_sampler = lambda **kw: None
    for name, value in {
        "mlx": mlx,
        "mlx.core": mx,
        "mlx_lm": lm,
        "mlx_lm.sample_utils": sampling,
    }.items():
        monkeypatch.setitem(sys.modules, name, value)
    units = [
        {"id": i, "segment": i, "text": f"第{i}段只是提议，尚未决定。" * 30} for i in range(1, 6)
    ]
    if fragment is not None:
        units.append({"id": 6, "segment": 6, "text": fragment, "overlap": True})
    request = {
        "work": str(tmp_path),
        "units": units,
        "fingerprint": "test",
        "model_dir": "unused",
        "settings": {"chunk_chars": 1000, "max_input_tokens": 16000, "max_output_tokens": 6000},
    }
    process(request)
    result = json.loads((tmp_path / "result.json").read_text())
    assert len(result["polished"]) > 1
    assert result["analysis"]["brief"]["source_ids"] == [u["segment"] for u in units]
    if fragment is not None:
        assert result["polished"][-1]["paragraphs"] == [{"unit_ids": [6], "text": fragment}]
        assert all(data.strip() != fragment for kind, data in seen if kind == "polish")
    assert any(kind == "merge" for kind, _ in seen)
    assert [u["text"] for kind, data in seen if kind == "analyze" for u in data] == [
        u["text"] for u in units
    ]
    assert "原始转写" in module.ANALYZE


def test_json_repair_changes_only_missing_quotes_on_known_keys():
    from audio_notes.enrichment import parse_model_json

    text = """{"highlights":[{text": "保留 {text\\\": 字样及原文。", source_ids": [1]}]}"""
    result, count = parse_model_json(text)
    assert count == 2
    assert result["highlights"][0]["text"] == '保留 {text": 字样及原文。'
    assert result["highlights"][0]["source_ids"] == [1]
    valid = json.dumps({"text": '原文中 {text": 的字样'}, ensure_ascii=False)
    assert parse_model_json(valid) == (json.loads(valid), 0)
    for invalid in ('{unknown":1}', '{text":"未完成', '{"text":"ok"'):
        with pytest.raises(json.JSONDecodeError):
            parse_model_json(invalid)


def test_unresolved_model_review_is_visible_in_delivery(document):
    config, directory, jobs = document

    def with_doubts(*args):
        fake_text_worker(*args)
        path = Path(args[1]["work"]) / "result.json"
        result = json.loads(path.read_text())
        result["review_issues"] = [{"stage": "analysis-0", "issues": ["需核对是否已决定上线。"]}]
        atomic_json(path, result)

    enrich_one(config, directory, {}, worker=with_doubts)
    marker = json.loads((directory / "enrichment.complete.json").read_text())
    assert marker["quality_status"] == "needs_review"
    assert "需核对是否已决定上线" in (directory / "analysis.md").read_text()
    assert (directory / "polished.md").is_file()


def test_facts_are_literal_source_quotes_and_do_not_invent_owners():
    from audio_notes.enrichment import ground_analysis, validate_grounding

    result = analysis([1])
    result["highlights"] = [{"text": "已决定周五上线。", "source_ids": [1]}]
    result["actions"] = [
        {"text": "张三负责上线。", "source_ids": [1], "owner": "张三", "deadline": "周五"}
    ]
    sources = {1: "只是提议周五上线，还没有决定。"}
    grounded = ground_analysis(result, sources)
    assert grounded["highlights"][0]["text"] == sources[1]
    assert grounded["actions"][0]["owner"] == "未明确"
    validate_grounding(grounded, sources)
    grounded["highlights"][0]["text"] = "已决定周五上线。"
    with pytest.raises(ValueError):
        validate_grounding(grounded, sources)


def test_inferences_do_not_add_unsupported_times_or_speaker_roles():
    from audio_notes.enrichment import ground_analysis

    result = analysis([1])
    result["insights"] = [
        {"text": "导师上午道歉，可能缓和关系。", "source_ids": [1]},
        {"text": "导师提到两地选择，可能需对比安排。", "source_ids": [1]},
    ]
    grounded = ground_analysis(result, {1: "中午的信息向你道歉。这里有两地选择。"})
    assert len(grounded["insights"]) == 1
    assert "导师" not in grounded["insights"][0]["text"]


def test_regeneration_keeps_downloadable_history_and_personal_notes(document):
    from audio_notes.workbench_store import WorkbenchStore

    config, directory, jobs = document
    store = WorkbenchStore(config.state)
    store.update(jobs[0]["audio_hash"], {"notes": "我的独立核对记录", "revision": 0})
    enrich_one(config, directory, {}, worker=fake_text_worker)
    old = (directory / "polished.md").read_bytes()
    enrich_one(config, directory, {}, force=True, worker=fake_text_worker)
    saved = list(directory.glob("history/*/polished.md"))
    assert len(saved) == 1 and saved[0].read_bytes() == old
    assert store.metadata()[jobs[0]["audio_hash"]]["notes"] == "我的独立核对记录"


def naming_worker(config, request, request_path, worker_log):
    if request.get("mode") != "title":
        fake_text_worker(config, request, request_path, worker_log)
    assert request["prompts"]["title"] == module.TITLE_PROMPT
    atomic_json(Path(request["work"]) / "title.json", {"title": "讨论预算提议尚未决定。"})


def test_delivery_names_recording_and_txt(document, monkeypatch):
    config, directory, jobs = document
    monkeypatch.setattr(module, "check_text_model", lambda *a, **k: {"model": "synthetic"})
    assert enrich_completed(config, jobs, worker=naming_worker) == 1
    assert json.loads((directory / "title.json").read_text())["title"] == "讨论预算提议尚未决定"
    names = [p.name for p in config.txt_output.iterdir()]
    assert len(names) == 1 and names[0].startswith("讨论预算提议尚未决定 2026-10-0")
    assert "讨论预算提议尚未决定" in (config.output / "index.md").read_text()


def test_already_delivered_recordings_are_named_once(document, monkeypatch):
    config, directory, jobs = document
    monkeypatch.setattr(module, "check_text_model", lambda *a, **k: {"model": "synthetic"})
    enrich_completed(config, jobs, worker=fake_text_worker)
    # Simulate a recording delivered before naming existed.
    (directory / "title.status.json").unlink()
    modes = []

    def title_only(config, request, *args):
        modes.append(request.get("mode"))
        naming_worker(config, request, *args)

    enrich_completed(config, jobs, worker=title_only)
    assert modes == ["title"] and (directory / "title.json").is_file()
    enrich_completed(config, jobs, worker=lambda *a: pytest.fail("名称已生成，不应重复"))


def test_naming_failure_keeps_delivery_and_backs_off(document, monkeypatch):
    config, directory, jobs = document
    monkeypatch.setattr(module, "check_text_model", lambda *a, **k: {"model": "synthetic"})
    enrich_completed(config, jobs, worker=fake_text_worker)
    (directory / "title.status.json").unlink()
    calls = []

    def broken(*args):
        calls.append(True)
        raise RuntimeError("模拟命名失败")

    enrich_completed(config, jobs, worker=broken)
    enrich_completed(config, jobs, worker=broken)
    assert len(calls) == 1 and (directory / "polished.md").is_file()
    assert json.loads((directory / "title.status.json").read_text())["status"] == "failed"


def test_scene_switch_reuses_body_and_preserves_original(document):
    from audio_notes.knowledge import Knowledge
    config, directory, _ = document
    raw = json.loads((directory / 'raw.json').read_text())
    before = sha256_file(directory / 'raw.json')
    enrich_one(config, directory, {}, worker=fake_text_worker)
    old_body = (directory / 'polished.txt').read_text()
    Knowledge(config.state).put('scene', raw['job_id'], {'scene': 'interview'})
    captured = []
    def worker(config, request, request_path, log):
        captured.append(request)
        fake_text_worker(config, request, request_path, log)
    enrich_one(config, directory, {}, force=True, worker=worker)
    assert captured[0]['reuse_polished']
    assert '问答脉络' in captured[0]['prompts']['analyze']
    assert (directory / 'polished.txt').read_text() == old_body
    assert list(directory.glob('history/*/polished.txt'))
    assert sha256_file(directory / 'raw.json') == before


def test_new_glossary_does_not_rewrite_existing_delivery(document):
    from audio_notes.enrichment import existing_delivery_valid
    from audio_notes.knowledge import Knowledge
    config, directory, jobs = document
    enrich_one(config, directory, {}, worker=fake_text_worker)
    old_fingerprint = fingerprint(config, directory / 'raw.json', {})
    Knowledge(config.state).term({'correct': '费用', 'wrong': ['预算']})
    assert fingerprint(config, directory / 'raw.json', {}) != old_fingerprint
    assert existing_delivery_valid(config, directory)
    Knowledge(config.state).put('scene', jobs[0]['id'], {'scene': 'class'})
    assert not existing_delivery_valid(config, directory)


def test_worker_reports_structured_failure(tmp_path, monkeypatch):
    from audio_notes import text_worker
    request = tmp_path / "request.json"
    atomic_json(request, {"work": str(tmp_path)})
    monkeypatch.setattr("sys.argv", ["worker", str(request)])
    def fail(request):
        raise ValueError("polish-1 两次生成仍未通过检查")
    monkeypatch.setattr(text_worker, "process", fail)
    assert text_worker.main() == 1
    assert json.loads((tmp_path / "worker-error.json").read_text())["error"] == "polish-1 两次生成仍未通过检查"


def fake_mlx(monkeypatch, respond):
    """Stand-in MLX stack; respond(prompt) returns the complete model reply."""
    import sys
    from types import ModuleType, SimpleNamespace

    tokenizer = SimpleNamespace(
        apply_chat_template=lambda messages, **kw: messages[-1]["content"],
        encode=lambda text: list(map(ord, text)),
    )
    mx = ModuleType("mlx.core")
    mx.set_cache_limit = mx.set_memory_limit = mx.clear_cache = lambda *a: None
    mx.random = SimpleNamespace(seed=lambda *a: None)
    mlx = ModuleType("mlx")
    mlx.core = mx
    lm = ModuleType("mlx_lm")
    lm.load = lambda *a, **k: (None, tokenizer)

    def generate(model, tokenizer, prompt, **kwargs):
        prompt = "".join(map(chr, prompt))
        yield SimpleNamespace(
            text=respond(prompt),
            finish_reason="stop",
            prompt_tokens=len(prompt),
            generation_tokens=1,
            generation_tps=1,
            peak_memory=0,
        )

    lm.stream_generate = generate
    sampling = ModuleType("mlx_lm.sample_utils")
    sampling.make_logits_processors = sampling.make_sampler = lambda **kw: None
    for name, value in {
        "mlx": mlx,
        "mlx.core": mx,
        "mlx_lm": lm,
        "mlx_lm.sample_utils": sampling,
    }.items():
        monkeypatch.setitem(sys.modules, name, value)


WORKER_SETTINGS = {"chunk_chars": 1000, "max_input_tokens": 16000, "max_output_tokens": 6000}


def test_worker_retries_json_of_the_wrong_shape(tmp_path, monkeypatch):
    from audio_notes.text_worker import process

    replies = iter(["[]", '{"terms": ["预算"]}'])
    fake_mlx(monkeypatch, lambda prompt: next(replies))
    process(
        {
            "mode": "retrieval",
            "retrieval_mode": "expand",
            "work": str(tmp_path),
            "units": [{"id": 1, "segment": 1, "text": "预算"}],
            "model_dir": "unused",
            "settings": WORKER_SETTINGS,
            "generation": {**module.GENERATION, "analysis_thinking": False},
            "instruction": "扩展检索词",
            "question": "预算",
            "sources": [],
        }
    )
    assert json.loads((tmp_path / "answer.json").read_text())["terms"] == ["预算"]
    assert (tmp_path / "answer.rejected-0.json").is_file()


def test_invalid_reused_body_is_regenerated(tmp_path, monkeypatch):
    from audio_notes.text_worker import process

    text = "第一段只是提议，尚未决定，下周再讨论。"
    fake_mlx(
        monkeypatch,
        lambda prompt: prompt.split("\n", 1)[1]
        if prompt.startswith(module.POLISH)
        else json.dumps(analysis([1]), ensure_ascii=False),
    )
    process(
        {
            "work": str(tmp_path),
            "units": [{"id": 1, "segment": 1, "text": text}],
            "fingerprint": "test",
            "model_dir": "unused",
            "settings": WORKER_SETTINGS,
            "generation": {**module.GENERATION, "analysis_thinking": False},
            "reuse_polished": [{"paragraphs": [], "corrections": [], "uncertainties": []}],
        }
    )
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["polished"][0]["paragraphs"] == [{"unit_ids": [1], "text": text}]


def test_stubborn_polish_chunk_keeps_source_text_and_recording_ships(tmp_path, monkeypatch):
    from audio_notes.text_worker import process

    source = "这里讨论的是十九个候选人的安排，还没有最后决定。"

    def respond(prompt):
        if prompt.startswith(module.POLISH):
            return "这里讨论的是20个候选人的安排，还没有最后决定。"
        result = analysis([1])
        # An uncited item used to fail the whole recording after two attempts.
        result["questions"] = [{"text": "没有依据的问题", "source_ids": []}]
        return json.dumps(result, ensure_ascii=False)

    fake_mlx(monkeypatch, respond)
    process(
        {
            "work": str(tmp_path),
            "units": [{"id": 1, "segment": 1, "text": source}],
            "fingerprint": "test",
            "model_dir": "unused",
            "settings": WORKER_SETTINGS,
            "generation": {**module.GENERATION, "analysis_thinking": False},
        }
    )
    result = json.loads((tmp_path / "result.json").read_text())
    chunk = result["polished"][0]
    assert chunk["paragraphs"] == [{"unit_ids": [1], "text": source}]
    assert chunk["uncertainties"][0]["unit_ids"] == [1]
    assert "保留原文" in chunk["uncertainties"][0]["text"]
    assert result["analysis"]["questions"] == []
    validate_polish(chunk, [{"id": 1, "segment": 1, "text": source}])
    # Resuming reuses the flagged chunk instead of spending another two attempts.
    assert json.loads((tmp_path / "polish-0.json").read_text()) == chunk


def test_repair_citations_never_invents_a_source():
    from audio_notes.enrichment import repair_citations

    result = analysis([1])
    result["highlights"] = [
        {"text": "重复", "source_ids": [2, 2, 99]},
        {"text": "全部无效", "source_ids": [99]},
        {"text": "没有来源", "source_ids": []},
    ]
    result["themes"] = [
        {"title": "保留", "points": [{"text": "有效", "source_ids": [1]}]},
        {"title": "清空", "points": [{"text": "无效", "source_ids": [99]}]},
    ]
    validate_analysis(repair_citations(result, {1, 2}), {1, 2})
    assert result["highlights"] == [{"text": "重复", "source_ids": [2]}]
    assert [theme["title"] for theme in result["themes"]] == ["保留"]

    # The brief is rewritten from surviving items later, so it may only cite those.
    result = analysis([1])
    result["brief"]["source_ids"] = []
    assert repair_citations(result, {1})["brief"]["source_ids"] == [1]

    # With nothing left to cite the output is still rejected and regenerated.
    result = analysis([99])
    with pytest.raises(ValueError):
        validate_analysis(repair_citations(result, {1}), {1})


def test_automatic_delivery_uses_source_body_without_polishing(tmp_path, monkeypatch):
    from audio_notes.text_worker import process

    units = [
        {"id": 1, "segment": 1, "text": "这是第一句比较长的识别原文，"},
        {"id": 2, "segment": 2, "text": "这是第二句比较长的识别原文。"},
    ]

    def respond(prompt):
        assert not prompt.startswith(module.POLISH), "自动流程不应调用模型润色"
        return json.dumps(analysis([1, 2]), ensure_ascii=False)

    fake_mlx(monkeypatch, respond)
    process(
        {
            "work": str(tmp_path),
            "units": units,
            "polish": False,
            "fingerprint": "test",
            "model_dir": "unused",
            "settings": WORKER_SETTINGS,
            "generation": module.GENERATION,
        }
    )
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["body"] == "source"
    assert result["polished"][0]["paragraphs"] == [
        {"unit_ids": [1, 2], "text": units[0]["text"] + units[1]["text"]}
    ]
    validate_polish(result["polished"][0], units)


def test_analysis_thinking_is_off_for_automatic_delivery():
    assert module.GENERATION["analysis_thinking"] is False


def test_worker_polish_mode_stops_before_analysis(tmp_path, monkeypatch):
    from audio_notes.text_worker import process

    text = "这是一段需要润色的比较长的识别原文。"

    def respond(prompt):
        assert prompt.startswith(module.POLISH), "润色模式不应重新分析"
        return "这是润色后的一段比较长的识别原文。"

    fake_mlx(monkeypatch, respond)
    process(
        {
            "mode": "polish",
            "work": str(tmp_path),
            "units": [{"id": 1, "segment": 1, "text": text}],
            "fingerprint": "test",
            "model_dir": "unused",
            "settings": WORKER_SETTINGS,
            "generation": module.GENERATION,
        }
    )
    polished = json.loads((tmp_path / "polished.json").read_text())
    assert polished[0]["paragraphs"][0]["text"] == "这是润色后的一段比较长的识别原文。"
    assert not (tmp_path / "result.json").exists()


def source_body_worker(config, request, request_path, worker_log):
    fake_text_worker(config, request, request_path, worker_log)
    path = Path(request["work"]) / "result.json"
    assert request["polish"] is False
    atomic_json(path, {**json.loads(path.read_text()), "body": "source"})


def test_polish_on_demand_updates_body_only(document):
    from audio_notes.enrichment import polish_one

    config, directory, jobs = document
    assert enrich_one(config, directory, {}, worker=source_body_worker)
    receipt = json.loads((directory / "enrichment.complete.json").read_text())
    key = receipt["fingerprint"]
    assert "未经模型润色" in (directory / "polished.md").read_text()
    analysis_before = (directory / "analysis.md").read_bytes()
    generated_before = receipt["created_at"]

    def polishing(config, request, request_path, worker_log):
        assert request["mode"] == "polish" and request["fingerprint"] == key
        chunks = polish_chunks_for(request["units"], config.analysis["chunk_chars"])
        atomic_json(
            Path(request["work"]) / "polished.json",
            [
                {
                    "paragraphs": [{"unit_ids": [u["id"]], "text": u["text"]} for u in chunk],
                    "corrections": [],
                    "uncertainties": [],
                }
                for chunk in chunks
            ],
        )

    assert polish_one(config, directory, worker=polishing)
    assert "本机模型优化" in (directory / "polished.md").read_text()
    assert delivery_valid(directory, key)
    assert (directory / "analysis.md").read_bytes() == analysis_before
    # An open recording notices the new body through this timestamp.
    receipt = json.loads((directory / "enrichment.complete.json").read_text())
    assert receipt["created_at"] != generated_before
    assert list(directory.glob("history/*/polished.md"))
    result = json.loads((directory / "enrichment" / key / "result.json").read_text())
    assert result["body"] == "model"
    # Already polished bodies are left alone, also when the analysis is regenerated.
    assert not polish_one(config, directory, worker=lambda *a: pytest.fail("不应重复润色"))
    captured = []

    def regenerate(config, request, request_path, worker_log):
        captured.append(request)
        fake_text_worker(config, request, request_path, worker_log)

    assert enrich_one(config, directory, {}, force=True, worker=regenerate)
    assert captured[0]["reuse_polished"] == result["polished"]


def test_source_body_is_not_passed_on_as_a_polished_one(document):
    config, directory, jobs = document
    assert enrich_one(config, directory, {}, worker=source_body_worker)
    captured = []

    def regenerate(config, request, request_path, worker_log):
        captured.append(request)
        source_body_worker(config, request, request_path, worker_log)

    assert enrich_one(config, directory, {}, force=True, worker=regenerate)
    assert captured[0]["reuse_polished"] is None


def test_polish_requires_a_current_delivery(document):
    from audio_notes.enrichment import polish_one

    config, directory, jobs = document
    with pytest.raises(ValueError, match="尚未生成"):
        polish_one(config, directory, worker=lambda *a: pytest.fail("不应调用模型"))
    assert enrich_one(config, directory, {}, worker=source_body_worker)
    (directory / "polished.md").write_text("损坏")
    with pytest.raises(ValueError, match="已过期"):
        polish_one(config, directory, worker=lambda *a: pytest.fail("不应调用模型"))


def test_generation_settings_change_does_not_rewrite_delivered_recordings(document, monkeypatch):
    config, directory, jobs = document
    monkeypatch.setattr(module, "check_text_model", lambda *a, **k: {"model": "synthetic"})
    enrich_completed(config, jobs, worker=fake_text_worker)
    before = (directory / "polished.md").read_bytes()
    monkeypatch.setattr(module, "GENERATION", {**module.GENERATION, "temperature": 0.9})
    enrich_completed(config, jobs, worker=lambda *a: pytest.fail("已交付稿件不应重算"))
    assert (directory / "polished.md").read_bytes() == before


def test_titles_only_names_the_recording_without_generating_documents(document, monkeypatch):
    config, directory, jobs = document
    monkeypatch.setattr(module, "check_text_model", lambda *a, **k: {"model": "synthetic"})
    modes = []

    def naming(config, request, request_path, worker_log):
        modes.append(request.get("mode"))
        atomic_json(Path(request["work"]) / "title.json", {"title": "预算讨论"})

    assert enrich_completed(config, jobs, worker=naming, titles_only=True) == 0
    assert modes == ["title"]
    assert not (directory / "enrichment.complete.json").exists()
    assert not (directory / "polished.md").exists()
    assert json.loads((directory / "title.json").read_text())["title"] == "预算讨论"
    # Named once; a later scan has nothing to do.
    enrich_completed(config, jobs, worker=lambda *a: pytest.fail("不应再调用模型"), titles_only=True)
    # An explicit request still generates the documents.
    assert enrich_completed(config, jobs, retry_now=True, worker=fake_text_worker) == 1
    assert (directory / "polished.md").is_file()


def test_scan_only_generates_documents_when_automatic(config, fake_worker, monkeypatch):
    (config.inbox / "selected.wav").write_text("只是提议。")
    seen = []
    monkeypatch.setattr(
        module, "enrich_completed", lambda config, jobs, **kw: seen.append(kw["titles_only"])
    )
    for automatic, expected in ((False, True), (True, False)):
        current = replace(
            config, analysis={**config.analysis, "enabled": True, "automatic": automatic}
        )
        p = Pipeline(current, "profile", worker=fake_worker)
        p.scan()
        p.close()
        assert seen[-1] is expected


def test_when_documents_are_generated_does_not_change_their_fingerprint(document):
    config, directory, jobs = document
    manual = replace(config, analysis={**config.analysis, "automatic": False})
    automatic = replace(config, analysis={**config.analysis, "automatic": True})
    assert fingerprint(manual, directory / "raw.json", {}) == fingerprint(
        automatic, directory / "raw.json", {}
    )


def test_json_repair_quotes_bare_known_keys_only():
    repaired, count = module.parse_model_json('{"brief": {text: "a: {text: b}", "source_ids": [1]}}')
    assert repaired == {"brief": {"text": "a: {text: b}", "source_ids": [1]}}
    assert count == 2
    with pytest.raises(json.JSONDecodeError):
        module.parse_model_json('{unknown: 1}')


def test_failed_merge_combines_part_analyses_instead_of_failing(tmp_path, monkeypatch):
    from audio_notes.text_worker import process

    units = [
        {"id": i, "segment": i, "text": f"第{i}段只是提议，尚未决定。" * 30} for i in range(1, 6)
    ]
    merges = []

    def respond(prompt):
        if "以下为覆盖全部录音的分段分析" in prompt:
            merges.append(True)
            return "这不是 JSON"
        segments = [int(i) for i in re.findall(r'"segment": (\d+)', prompt)]
        return json.dumps(analysis(segments), ensure_ascii=False)

    fake_mlx(monkeypatch, respond)
    process(
        {
            "work": str(tmp_path),
            "units": units,
            "polish": False,
            "fingerprint": "test",
            "model_dir": "unused",
            "settings": WORKER_SETTINGS,
            "generation": module.GENERATION,
        }
    )
    result = json.loads((tmp_path / "result.json").read_text())
    assert len(merges) == 2
    cited = {i for item in result["analysis"]["highlights"] for i in item["source_ids"]}
    assert len(cited) > 1
    assert result["analysis"]["brief"]["source_ids"]
    module.validate_grounding(result["analysis"], module.source_texts(units))


@pytest.mark.parametrize(
    "source,edited",
    [
        ("十九个人", "19个人"),
        ("预算3000元", "预算三千元"),
        ("13,300元", "13300元"),
        ("一万五", "1.5万"),
        ("两百五十万", "250万"),
        ("二零二六年", "2026年"),
        ("百分之二十", "20%"),
        ("三点五倍", "3.5倍"),
        ("下午三点半", "下午3点半"),
        ("版本1.2.3", "版本1.2.3"),
        ("3000，3000元", "3000元"),
        ("大家一起看三个方案", "大家一起看3个方案"),
        ("开了十分钟", "开了10分钟"),
        ("两个方案", "2个方案"),
    ],
)
def test_same_number_in_another_notation_is_accepted(source, edited):
    assert module.numbers_preserved(source, edited)


@pytest.mark.parametrize(
    "source,edited",
    [
        ("十九个人", "20个人"),
        ("预算3000元", "预算4000元"),
        ("预算3000元", "预算的金额"),
        ("没有提到日期", "2020年没有提到日期"),
        ("3000和30000", "3000"),
        ("一万五", "1.5千"),
        ("版本1.2.3", "版本1.2.4"),
        ("三个人做了5天", "5个人做了3天"),
        ("3000元和5天", "5天和3000元"),
        ("大家一起看一下", "1个人看一下"),
        ("两个方案", "1个方案"),
    ],
)
def test_changed_lost_or_invented_numbers_stay_rejected(source, edited):
    assert not module.numbers_preserved(source, edited)


def test_polish_batches_respect_size_and_keep_order():
    chunk = lambda i, size: [{"id": i, "segment": i, "text": "字" * size}]  # noqa: E731
    items = [(i, chunk(i, 100)) for i in range(14)] + [(14, chunk(14, 600)), (15, chunk(15, 601))]
    batches = module.polish_batches(items)
    assert [i for batch in batches for i, _ in batch] == list(range(16))
    assert [len(batch) for batch in batches] == [8, 6, 1, 1]
    assert all(sum(len(c[0]["text"]) for _, c in b) <= module.POLISH_BATCH_CHARS for b in batches)


def test_split_polished_parts_takes_only_expected_numbers():
    split = module.split_polished_parts
    assert split("【1】甲。\n\n【2】乙。", {1, 2}) == {1: "甲。", 2: "乙。"}
    assert split("说明\n【2】乙。【9】多余。【2】重复。", {1, 2}) == {2: "乙。"}
    assert split("没有编号的正文", {3}) == {3: "没有编号的正文"}
    assert split("没有编号的正文", {1, 2}) == {}


def batch_units():
    return [
        {"id": 1, "segment": 1, "text": "这是第一位说话的人讲的比较长的一句话。", "speaker": "speaker_1"},
        {"id": 2, "segment": 2, "text": "嗯。", "speaker": "speaker_2"},
        {"id": 3, "segment": 3, "text": "这是第二位说话的人讲的比较长的一句话。", "speaker": "speaker_1"},
        {"id": 4, "segment": 4, "text": "这是第一位说话的人接着讲的第三句话。", "speaker": "speaker_2"},
    ]


def polish_request(work):
    return {
        "mode": "polish",
        "work": str(work),
        "units": batch_units(),
        "fingerprint": "test",
        "model_dir": "unused",
        "settings": WORKER_SETTINGS,
        "generation": module.GENERATION,
    }


def test_one_call_polishes_neighbouring_turns_and_short_ones_stay_verbatim(tmp_path, monkeypatch):
    from audio_notes.text_worker import process

    prompts = []

    def respond(prompt):
        prompts.append(prompt)
        return (
            "【1】第一位说话的人讲了比较长的一句话。\n\n"
            "【2】第二位说话的人讲了比较长的一句话。\n\n"
            "【3】第一位说话的人接着讲了第三句话。"
        )

    fake_mlx(monkeypatch, respond)
    process(polish_request(tmp_path))
    assert len(prompts) == 1 and "嗯。" not in prompts[0]
    polished = json.loads((tmp_path / "polished.json").read_text())
    assert [p["paragraphs"][0]["unit_ids"] for p in polished] == [[1], [2], [3], [4]]
    assert polished[0]["paragraphs"][0]["text"] == "第一位说话的人讲了比较长的一句话。"
    assert polished[1]["paragraphs"][0]["text"] == "嗯。"
    assert polished[3]["paragraphs"][0]["text"] == "第一位说话的人接着讲了第三句话。"


def test_only_the_rejected_part_is_retried_and_then_kept_as_source(tmp_path, monkeypatch):
    from audio_notes.text_worker import process

    prompts = []

    def respond(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return (
                "【1】第一位说话的人讲了比较长的一句话。\n\n"
                "【2】2020年第二位说话的人讲了比较长的一句话。\n\n"
                "【3】第一位说话的人接着讲了第三句话。"
            )
        return "2021年第二位说话的人讲了比较长的一句话。"

    fake_mlx(monkeypatch, respond)
    process(polish_request(tmp_path))
    assert len(prompts) == 2
    assert module.POLISH_PARTS not in prompts[1] and "【" not in prompts[1]
    assert batch_units()[2]["text"] in prompts[1] and batch_units()[0]["text"] not in prompts[1]
    polished = json.loads((tmp_path / "polished.json").read_text())
    assert polished[0]["paragraphs"][0]["text"] == "第一位说话的人讲了比较长的一句话。"
    assert polished[2]["paragraphs"][0]["text"] == batch_units()[2]["text"]
    assert "保留原文" in polished[2]["uncertainties"][0]["text"]
    assert not polished[0]["uncertainties"]


def test_resuming_polish_only_asks_for_unfinished_parts(tmp_path, monkeypatch):
    from audio_notes.text_worker import process

    first = {
        "paragraphs": [{"unit_ids": [1], "text": "第一位说话的人讲了比较长的一句话。"}],
        "corrections": [],
        "uncertainties": [],
    }
    atomic_json(tmp_path / "polish-0.json", first)
    prompts = []

    def respond(prompt):
        prompts.append(prompt)
        return "【1】第二位说话的人讲了比较长的一句话。\n\n【2】第一位说话的人接着讲了第三句话。"

    fake_mlx(monkeypatch, respond)
    process(polish_request(tmp_path))
    assert len(prompts) == 1 and batch_units()[0]["text"] not in prompts[0]
    assert json.loads((tmp_path / "polished.json").read_text())[0] == first


def test_polish_misaligned_detects_text_under_the_wrong_part():
    sources = {1: "今天上午我们讨论了预算的安排。", 2: "好的，那我下午把草稿发给你。"}
    aligned = {1: "今天上午我们讨论了预算安排。", 2: "好的，我下午把草稿发给你。"}
    assert not module.polish_misaligned(aligned, sources)
    assert module.polish_misaligned({1: "好的，我下午把草稿发给你。"}, sources)
    assert module.polish_misaligned({1: "完全无关的另一件事情，和这里没有关系。"}, sources)


def test_batch_with_misplaced_text_is_redone_part_by_part(tmp_path, monkeypatch):
    from audio_notes.text_worker import process

    prompts = []

    def respond(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            # Parts two and three are swapped: every check but alignment would pass.
            return (
                "【1】第一位说话的人讲了比较长的一句话。\n\n"
                "【2】这是第一位说话的人接着讲的第三句话。\n\n"
                "【3】这是第二位说话的人讲的比较长的一句话。"
            )
        return prompt.rsplit("\n", 1)[1]

    fake_mlx(monkeypatch, respond)
    process(polish_request(tmp_path))
    assert len(prompts) == 4
    polished = json.loads((tmp_path / "polished.json").read_text())
    assert polished[2]["paragraphs"][0]["text"] == batch_units()[2]["text"]
    assert polished[3]["paragraphs"][0]["text"] == batch_units()[3]["text"]
    assert polished[0]["paragraphs"][0]["text"] == batch_units()[0]["text"]


def test_stray_brackets_from_the_part_format_are_rejected(tmp_path, monkeypatch):
    from audio_notes.text_worker import process

    prompts = []

    def respond(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return (
                "【1】这是【第一位】说话的人讲的比较长的一句话。\n\n"
                "【2】这是第二位说话的人讲的比较长的一句话。\n\n"
                "【3】这是第一位说话的人接着讲的第三句话。"
            )
        return prompt.rsplit("\n", 1)[1]

    fake_mlx(monkeypatch, respond)
    process(polish_request(tmp_path))
    polished = json.loads((tmp_path / "polished.json").read_text())
    assert len(prompts) == 2
    assert "【" not in polished[0]["paragraphs"][0]["text"]


def fake_polish_worker(config, request, request_path, worker_log):
    reuse = request.get("reuse_polished") or []
    chunks = polish_chunks_for(request["units"], config.analysis["chunk_chars"])
    atomic_json(
        Path(request["work"]) / "polished.json",
        [
            (reuse[i] if i < len(reuse) else None)
            or {
                "paragraphs": [
                    {
                        "unit_ids": [u["id"] for u in chunk],
                        "text": "润色后" + "".join(u["text"] for u in chunk),
                    }
                ],
                "corrections": [],
                "uncertainties": [],
            }
            for i, chunk in enumerate(chunks)
        ],
    )


def delivered_result(directory):
    key = json.loads((directory / "enrichment.complete.json").read_text())["fingerprint"]
    return directory / "enrichment" / key / "result.json"


def test_polish_builds_on_saved_corrections(document):
    from audio_notes.enrichment import polish_one
    from audio_notes.knowledge import Knowledge
    from audio_notes.workbench_library import detail
    from audio_notes.workbench_store import WorkbenchStore

    config, directory, jobs = document
    job = jobs[0]["id"]
    assert enrich_one(config, directory, {}, worker=source_body_worker)
    k = Knowledge(config.state)
    fixed = k.revise(job, [{"id": 1, "text": "预算3000元。只是提议，尚未决定。"}], "预算", "经费", "one", 1)
    worded = k.revise(
        job, [{"id": 1, "text": "经费3000元。只是提议，尚未决定。"}], "提议", "建议", "one", 1, view="body"
    )
    captured = []

    def polishing(config, request, request_path, worker_log):
        captured.append(request)
        fake_polish_worker(config, request, request_path, worker_log)

    assert polish_one(config, directory, worker=polishing)
    assert captured[0]["units"][0]["text"] == "经费3000元。只是建议，尚未决定。"
    # result.json keeps the transcript; the body correction belongs to the polished text alone.
    result = json.loads(delivered_result(directory).read_text())
    assert result["units"][0]["text"] == "经费3000元。只是提议，尚未决定。"
    assert "润色后经费3000元。只是建议，尚未决定。" in (directory / "polished.txt").read_text()
    # The transcript correction still applies and undoes; the body one is now part of the body.
    assert k.get("revisions", fixed["id"])["active"]
    absorbed = k.get("revisions", worded["id"])
    assert not absorbed["active"] and absorbed["edits"] == [] and len(absorbed["absorbed"]) == 1
    d = detail(config, WorkbenchStore(config.state), job)
    assert d["smart"]["body"][0]["text"] == "润色后经费3000元。只是建议，尚未决定。"


def test_body_correction_lands_on_its_own_paragraph_when_text_repeats():
    units = [
        {"id": 1, "segment": 1, "text": "好的。"},
        {"id": 2, "segment": 2, "text": "先看预算。"},
        {"id": 3, "segment": 3, "text": "好的。"},
    ]
    assert module.fold_body_edit(units, {"before": "好的。", "after": "好吧。"}, 3)
    assert [u["text"] for u in units] == ["好的。", "先看预算。", "好吧。"]
    assert not module.fold_body_edit(units, {"before": "不存在", "after": "别的"}, 1)
    # An edit across a unit boundary keeps the following text in its own unit.
    units = [{"id": 1, "segment": 1, "text": "甲乙"}, {"id": 2, "segment": 1, "text": "丙丁"}]
    assert module.fold_body_edit(units, {"before": "甲乙丙丁", "after": "甲戊丁"}, 1)
    assert [u["text"] for u in units] == ["甲戊", "丁"]


def test_regenerating_a_polished_body_only_repolishes_changed_source(document):
    from audio_notes.enrichment import polish_one
    from audio_notes.knowledge import Knowledge

    config, directory, jobs = document
    assert enrich_one(config, directory, {}, worker=source_body_worker)
    assert polish_one(config, directory, worker=fake_polish_worker)
    Knowledge(config.state).revise(
        jobs[0]["id"], [{"id": 1, "text": "预算3000元。只是提议，尚未决定。"}], "预算", "经费", "one", 1
    )
    captured = []

    def regenerate(config, request, request_path, worker_log):
        captured.append(request)
        fake_text_worker(config, request, request_path, worker_log)

    assert enrich_one(config, directory, {}, force=True, worker=regenerate)
    assert captured[0]["polish"] is True and captured[0]["reuse_polished"] == [None]


def test_carried_polish_keeps_unchanged_chunks_under_their_new_units():
    def unit(key, speaker, text):
        return {"id": key, "segment": key, "text": text, "speaker": speaker, "overlap": False}

    def chunk(key, text, doubts=()):
        return {
            "paragraphs": [{"unit_ids": [key], "text": text}],
            "corrections": [],
            "uncertainties": list(doubts),
        }

    units = [unit(1, "speaker_1", "第一段"), unit(2, "speaker_2", "第二段"), unit(3, "speaker_1", "第三段")]
    previous = {"units": units, "polished": [chunk(1, "一"), chunk(2, "二"), chunk(3, "三")]}
    new = [
        unit(1, "speaker_3", "新增的一段"),
        unit(2, "speaker_1", "第一段"),
        unit(3, "speaker_2", "第二段改过"),
        unit(4, "speaker_1", "第三段"),
    ]
    assert module.carried_polish(previous, new, 1000) == [None, chunk(2, "一"), None, chunk(4, "三")]
    assert module.carried_polish({**previous, "body": "source"}, new, 1000) is None
    failed = module.polish_fallback([units[0]], "模拟失败")
    assert module.polish_failed(failed)
    previous["polished"][0] = failed
    assert module.carried_polish(previous, units, 1000)[0] is None


def test_failed_chunks_can_be_polished_again(document):
    from audio_notes.enrichment import polish_one
    from audio_notes.workbench_library import detail
    from audio_notes.workbench_store import WorkbenchStore

    config, directory, jobs = document

    def flagged(config, request, request_path, worker_log):
        fake_text_worker(config, request, request_path, worker_log)
        path = Path(request["work"]) / "result.json"
        result = json.loads(path.read_text())
        atomic_json(path, {**result, "polished": [module.polish_fallback(request["units"], "模拟失败")]})

    assert enrich_one(config, directory, {}, worker=flagged)
    store = WorkbenchStore(config.state)
    assert detail(config, store, jobs[0]["id"])["smart"]["body_retry"]

    def fails_again(config, request, request_path, worker_log):
        work = Path(request["work"])
        # The fallback cached by the previous attempt must not answer this one.
        assert not (work / "polish-0.json").exists()
        assert request["reuse_polished"] == [None]
        fallback = module.polish_fallback(request["units"], "模拟失败")
        atomic_json(work / "polish-0.json", fallback)
        atomic_json(work / "polished.json", [fallback])

    assert polish_one(config, directory, worker=fails_again)
    assert polish_one(config, directory, worker=fails_again)
    assert polish_one(config, directory, worker=fake_polish_worker)
    assert not detail(config, store, jobs[0]["id"])["smart"]["body_retry"]
    assert not polish_one(config, directory, worker=lambda *a: pytest.fail("不应重复润色"))


def test_regenerating_retries_a_failed_chunk_instead_of_carrying_it(document):
    config, directory, jobs = document

    def flagged(config, request, request_path, worker_log):
        fake_text_worker(config, request, request_path, worker_log)
        path = Path(request["work"]) / "result.json"
        result = json.loads(path.read_text())
        atomic_json(path, {**result, "polished": [module.polish_fallback(request["units"], "模拟失败")]})

    assert enrich_one(config, directory, {}, worker=flagged)
    captured = []

    def regenerate(config, request, request_path, worker_log):
        captured.append(request)
        fake_text_worker(config, request, request_path, worker_log)

    assert enrich_one(config, directory, {}, force=True, worker=regenerate)
    assert captured[0]["polish"] is True and captured[0]["reuse_polished"] == [None]


def test_unusable_reused_body_is_not_recorded_as_polished(tmp_path, monkeypatch):
    from audio_notes.text_worker import process

    text = "第一段只是提议，尚未决定，下周再讨论。"
    fake_mlx(monkeypatch, lambda prompt: json.dumps(analysis([1]), ensure_ascii=False))
    process(
        {
            "work": str(tmp_path),
            "units": [{"id": 1, "segment": 1, "text": text}],
            "polish": False,
            "fingerprint": "test",
            "model_dir": "unused",
            "settings": WORKER_SETTINGS,
            "generation": module.GENERATION,
            "reuse_polished": [{"paragraphs": [], "corrections": [], "uncertainties": []}],
        }
    )
    assert json.loads((tmp_path / "result.json").read_text())["body"] == "source"


def test_polish_of_a_full_chunk_may_grow_by_punctuation_but_not_much_more():
    units = [{"id": 1, "segment": 1, "text": "字" * 590}]

    def polished(text):
        return {
            "paragraphs": [{"unit_ids": [1], "text": text}],
            "corrections": [],
            "uncertainties": [],
        }

    validate_polish(polished("字，" * 305), units)
    with pytest.raises(ValueError, match="段落过长"):
        validate_polish(polished("字" * 900), units)


def test_alone_retry_tells_the_model_what_was_wrong(tmp_path, monkeypatch):
    from audio_notes.text_worker import process

    prompts = []

    def respond(prompt):
        prompts.append(prompt)
        return "这里讨论的是20个候选人的安排，还没有最后决定。"

    fake_mlx(monkeypatch, respond)
    process(
        {
            "mode": "polish",
            "work": str(tmp_path),
            "units": [{"id": 1, "segment": 1, "text": "这里讨论的是十九个候选人的安排，还没有最后决定。"}],
            "fingerprint": "test",
            "model_dir": "unused",
            "settings": WORKER_SETTINGS,
            "generation": module.GENERATION,
        }
    )
    assert len(prompts) == 2 and "上次输出未通过检查：润色稿丢失或更改了数字" in prompts[1]


def test_watch_round_stays_within_one_allowance(config, fake_worker, monkeypatch):
    enabled = replace(
        config,
        analysis={**config.analysis, "enabled": True, "automatic": True, "max_jobs_per_scan": 1},
    )
    (enabled.inbox / "first.wav").write_text("第一条录音，预算3000元。")
    (enabled.inbox / "second.wav").write_text("第二条录音，预算4000元。")
    monkeypatch.setattr(module, "check_text_model", lambda *a, **k: {"model": "synthetic"})
    documents = []

    def worker(config, request, request_path, worker_log):
        if request.get("mode") != "title":
            documents.append(request["job_id"])
        fake_text_worker(config, request, request_path, worker_log)

    real = module.enrich_completed
    monkeypatch.setattr(module, "enrich_completed", lambda *a, **k: real(*a, worker=worker, **k))
    pipeline = Pipeline(enabled, "profile", worker=fake_worker)
    pipeline.scan(watch=True, followup_limit=True)
    assert len(documents) == 1
    pipeline.close()
