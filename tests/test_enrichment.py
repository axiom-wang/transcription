import copy
import json
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
    monkeypatch.setattr(module, "enrich_completed", lambda config, jobs, **kw: calls.extend(jobs))
    p.config = replace(config, analysis={**config.analysis, "enabled": True})
    p.scan()
    assert len(fake_worker.calls) == 1
    assert len(calls) == 1
    p.close()


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


def test_long_recording_worker_covers_all_chunks_and_merges_original_evidence(
    tmp_path, monkeypatch
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
    assert result["analysis"]["brief"]["source_ids"] == [1, 2, 3, 4, 5]
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
