from copy import deepcopy
from dataclasses import replace

import pytest

from audio_notes.diarization import (
    REPO,
    REVISION,
    assign_segments,
    diarize_completed,
    diarize_one,
    speaker_label,
    speaker_view,
    validate_turns,
)
from audio_notes.enrichment import polish_chunks_for, units_for
from audio_notes.files import atomic_json, sha256_file
from audio_notes.pipeline import exports_valid, write_exports


def turn(a, b, speaker=1):
    return {"start": a, "end": b, "speaker": f"speaker_{speaker}"}


def conversation():
    return [
        {
            "start": 0,
            "end": 4,
            "text": "你好。好的，我们明天讨论！",
            "words": [
                {"start": 0, "end": 1, "word": "你好"},
                {"start": 2, "end": 2.5, "word": "好的"},
                {"start": 2.5, "end": 3, "word": "我们"},
                {"start": 3, "end": 3.5, "word": "明天"},
                {"start": 3.5, "end": 4, "word": "讨论"},
            ],
        }
    ]


def test_exact_text_and_speaker_switch_preserve_punctuation():
    segments = conversation()
    original = deepcopy(segments)
    turns = [turn(0, 1.5), turn(1.5, 4, 2)]
    result = assign_segments(segments, turns, turns)
    assert segments == original
    assert [s["speaker"] for s in result] == ["speaker_1", "speaker_2"]
    assert [s["text"] for s in result] == ["你好。", "好的，我们明天讨论！"]
    assert [s["original_segment"] for s in result] == [1, 1]
    assert [s["id"] for s in result] == [0, 1]
    assert result[1]["start"] == 2


def test_overlap_has_no_guessed_owner():
    result = assign_segments(conversation(), [turn(0, 4)], [turn(0, 4), turn(0.2, 0.7, 2)])
    assert result[0]["overlap"] is True
    assert result[0]["speaker"] is None
    assert speaker_label(result[0]) == "重叠发言（归属待确认）"


@pytest.mark.parametrize(
    "words",
    [[], [{"word": "错字", "start": 0, "end": 1}], [{"word": "好的", "start": 0, "end": 1}]],
)
def test_missing_alignment_never_assigns_mixed_segment_to_majority(words):
    segments = conversation()
    segments[0]["words"] = words
    turns = [turn(0, 3), turn(3, 4, 2)]
    result = assign_segments(segments, turns, turns)
    assert len(result) == 1
    assert result[0]["text"] == segments[0]["text"]
    assert result[0]["speaker"] is None


def test_no_nearest_speaker_guess_in_silence():
    result = assign_segments([{"start": 5, "end": 6, "text": "嗯"}], [turn(0, 1)], [turn(0, 1)])
    assert speaker_label(result[0]) == "说话人待确认"
    assert speaker_label({"text": "旧稿"}) == ""


@pytest.mark.parametrize(
    "turns,exclusive",
    [
        ([turn(-1, 1)], False),
        ([turn(0, 11)], False),
        ([turn(0, float("nan"))], False),
        ([turn(1, 2), turn(0, 1)], False),
        ([turn(0, 2), turn(1, 3, 2)], True),
        ([{"start": 0, "end": 1, "speaker": "<script>"}], False),
    ],
)
def test_rejects_invalid_turns(turns, exclusive):
    with pytest.raises(ValueError):
        validate_turns(turns, 10, exclusive)


def fixture_job(config):
    source = config.inbox / "conversation.wav"
    source.write_bytes(b"synthetic-audio-placeholder")
    key = "b" * 64
    directory = config.output / key
    raw = {
        "schema_version": 1,
        "job_id": key,
        "audio_sha256": sha256_file(source),
        "profile": "test",
        "source": {"name": "对话", "path": str(source)},
        "created_at": "now",
        "info": {"duration": 4, "language": "zh"},
        "settings": {"model": "qwen3-asr-1.7b"},
        "segments": conversation(),
        "text": conversation()[0]["text"],
    }
    atomic_json(directory / "raw.json", raw)
    write_exports(raw, directory, config.markdown)
    return {"id": key, "source": str(source), "output": str(directory)}, raw, directory


def known_worker(config, request, log):
    turns = [turn(0, 1.5), turn(1.5, 4, 2)]
    atomic_json(
        __import__("pathlib").Path(request["result_path"]),
        {"turns": turns, "exclusive_turns": turns},
    )


def test_independent_stage_cache_exports_and_legacy_anchors(config):
    job, raw, directory = fixture_job(config)
    before = sha256_file(directory / "raw.json")
    assert diarize_one(config, job, {"model": "test"}, worker=known_worker)
    assert not diarize_one(
        config, job, {"model": "test"}, worker=lambda *args: pytest.fail("cache miss")
    )
    assert sha256_file(directory / "raw.json") == before
    view = speaker_view(raw, directory)
    assert len(view["segments"]) == 2
    assert view["diarization"]["repository"] == REPO
    assert not exports_valid(directory, config.markdown)
    write_exports(raw, directory, config.markdown)
    assert exports_valid(directory, config.markdown)
    text = (directory / "transcript.simplified.md").read_text()
    assert text.count('id="segment-1"') == 1
    assert 'id="turn-1"' in text and 'id="turn-2"' in text
    assert "说话人 1" in text and "说话人 2" in text
    assert "说话人 2：好的，我们明天讨论！" in (directory / "transcript.simplified.txt").read_text()
    assert "说话人 1：你好。" in (directory / "transcript.srt").read_text()
    assert "#turn-2" in (directory / "notes.md").read_text()
    assert sha256_file(directory / "raw.json") == before
    chunks = polish_chunks_for(units_for(view), 4000)
    assert len(chunks) == 2  # each voice must be edited independently
    assert all(len({u["speaker"] for u in chunk}) == 1 for chunk in chunks)


def test_bad_sidecar_falls_back_without_touching_asr(config):
    _, raw, directory = fixture_job(config)
    atomic_json(directory / "speakers.json", {"schema": 1, "audio_sha256": "other"})
    assert speaker_view(raw, directory) is raw


def test_raw_change_invalidates_speaker_sidecar(config):
    job, raw, directory = fixture_job(config)
    diarize_one(config, job, {}, worker=known_worker)
    changed = {**raw, "created_at": "new"}
    atomic_json(directory / "raw.json", changed)
    assert speaker_view(changed, directory) is changed


def test_audio_change_refuses_result(config):
    job, raw, directory = fixture_job(config)
    before = sha256_file(directory / "raw.json")

    def modifies_audio(config, request, log):
        known_worker(config, request, log)
        __import__("pathlib").Path(job["source"]).write_bytes(b"changed")

    with pytest.raises(ValueError, match="音频改变"):
        diarize_one(config, job, {}, worker=modifies_audio)
    assert not (directory / "speakers.json").exists()
    assert sha256_file(directory / "raw.json") == before


def test_failure_is_independent_of_asr_with_retry_cooldown(config, monkeypatch):
    config = replace(config, speakers={**config.speakers, "enabled": True})
    job, raw, directory = fixture_job(config)
    monkeypatch.setattr("audio_notes.diarization.check_speaker_model", lambda c: {})
    calls = []

    def fails(*args):
        calls.append(1)
        raise RuntimeError("synthetic failure")

    assert diarize_completed(config, [job], worker=fails) == 0
    assert exports_valid(directory, config.markdown)
    assert diarize_completed(config, [job], worker=fails) == 0
    assert len(calls) == 1
    assert diarize_completed(config, [job], worker=known_worker, retry_now=True) == 1
    assert exports_valid(directory, config.markdown)


def test_unready_model_does_not_block_transcript(config):
    config = replace(config, speakers={**config.speakers, "enabled": True})
    job, raw, directory = fixture_job(config)
    assert diarize_completed(config, [job]) == 0
    from audio_notes.control import read_json

    assert read_json(directory / "speakers.status.json")["status"] == "blocked"
    assert exports_valid(directory, config.markdown)


def test_new_source_anchors_do_not_break_old_links(config):
    job, raw, directory = fixture_job(config)
    raw["segments"].append({"start": 4, "end": 5, "text": "再见。"})
    raw["info"]["duration"] = 5
    view = {
        **raw,
        "segments": assign_segments(
            raw["segments"], [turn(0, 2), turn(2, 5, 2)], [turn(0, 2), turn(2, 5, 2)]
        ),
        "diarization": {"repository": REPO, "revision": REVISION},
    }
    from audio_notes.markdown import render

    render(view, directory, config.markdown)
    text = (directory / "transcript.md").read_text()
    assert text.count('id="segment-2"') == 1
    assert text.index('id="segment-2"') > text.index('id="turn-2"')


def test_text_delivery_respects_voices_and_quotes(config):
    import json
    from pathlib import Path

    from audio_notes.enrichment import enrich_one, fingerprint

    job, raw, directory = fixture_job(config)
    old_key = fingerprint(config, directory / "raw.json", {})
    diarize_one(config, job, {}, worker=known_worker)
    assert fingerprint(config, directory / "raw.json", {}) != old_key
    write_exports(raw, directory, config.markdown)
    seen = []

    def text_worker(config, request, request_path, worker_log):
        seen.append(request)
        paragraphs = [
            {
                "paragraphs": [
                    {"unit_ids": [u["id"] for u in c], "text": "".join(u["text"] for u in c)}
                ],
                "corrections": [],
                "uncertainties": [],
            }
            for c in polish_chunks_for(request["units"], 4000)
        ]
        atomic_json(
            Path(request["work"]) / "result.json",
            {
                "fingerprint": request["fingerprint"],
                "units": request["units"],
                "polished": paragraphs,
                "analysis": {
                    "brief": {"text": "对话", "source_ids": [1, 2]},
                    "highlights": [{"text": "明天讨论", "source_ids": [2]}],
                    "themes": [],
                    "insights": [],
                    "actions": [],
                    "questions": [],
                    "suggestions": [],
                },
            },
        )

    enrich_one(config, directory, {}, worker=text_worker)
    assert "原稿没有说话人标签" not in seen[0]["prompts"]["analyze"]
    assert "speaker编号" in seen[0]["prompts"]["analyze"]
    plain = (directory / "polished.txt").read_text()
    assert "说话人 1：你好。" in plain
    assert "说话人 2：好的，我们明天讨论！" in plain
    report = (directory / "analysis.md").read_text()
    assert "原稿未标注说话人" not in report
    assert "说话人 2 ·" in report and "#turn-2" in report
    receipt = json.loads((directory / "enrichment.complete.json").read_text())
    assert receipt["speakers_sha256"] == sha256_file(directory / "speakers.json")


def test_pyannote_numpy_progress_counts_are_json_safe(tmp_path):
    import numpy as np

    from audio_notes.control import read_json
    from audio_notes.speaker_worker import progress_hook

    target = tmp_path / "activity.json"
    progress_hook(target)("segmentation", None, completed=np.int64(32), total=np.int64(9))
    report = read_json(target)
    assert report["completed"] == report["total"] == 9
    assert report["stage"] == "检测声音与重叠发言"


def test_zero_length_word_does_not_prevent_speaker_changes():
    segments = conversation()
    segments[0]["words"][1]["end"] = 2
    turns = [turn(0, 1.5), turn(1.5, 4, 2)]
    result = assign_segments(segments, turns, turns)
    assert [s["speaker"] for s in result] == ["speaker_1", "speaker_2"]
    assert "".join(s["text"] for s in result) == segments[0]["text"]


def test_zero_length_word_at_speaker_boundary_stays_uncertain():
    segments = [
        {"start": 0, "end": 2, "text": "好。", "words": [{"word": "好", "start": 1, "end": 1}]}
    ]
    turns = [turn(0, 1), turn(1, 2, 2)]
    result = assign_segments(segments, turns, turns)
    assert result[0]["speaker"] is None
    assert result[0]["text"] == "好。"


def test_interval_index_matches_overlaps_with_nested_turns():
    from audio_notes.diarization import TurnIndex

    turns = [turn(0, 100), turn(1, 2, 2), turn(2, 3, 3), turn(7, 9, 2), turn(101, 102)]
    index = TurnIndex(turns)
    for start, end in [(0, 0.1), (1, 2), (2, 3), (8, 10), (99, 101), (102, 103)]:
        expected = [t for t in turns if t["start"] < end and t["end"] > start]
        selected = [
            t for t in index.overlapping(start, end) if t["start"] < end and t["end"] > start
        ]
        assert selected == expected
