from copy import deepcopy

from audio_notes.markdown import render


def test_simplified_exports_preserve_originals_timestamps_and_source_paths(config):
    source = config.inbox / "學習 [原檔].m4a"
    raw = {
        "source": {"name": "學習 [測試]", "path": str(source)},
        "audio_sha256": "hash",
        "created_at": "now",
        "settings": {"model": "small"},
        "info": {"language": "zh", "duration": 8},
        "job_id": "job",
        "segments": [
            {
                "start": 0.125,
                "end": 2.5,
                "text": "這個 Python API v2.0，學習資料與軟體。",
                "words": [{"start": 0.125, "end": 0.5, "word": "這個"}],
            },
            {"start": 5.25, "end": 8.0, "text": "會說中文，也會說 English."},
        ],
    }
    before = deepcopy(raw)
    directory = config.output / "result"
    render(raw, directory, config.markdown)
    assert raw == before
    original_text = "\n".join(s["text"] for s in before["segments"]) + "\n"
    assert (directory / "transcript.txt").read_text() == original_text
    assert "這個 Python API v2.0" in (directory / "transcript.md").read_text()
    assert "會說中文" in (directory / "transcript.srt").read_text()
    simplified = "这个 Python API v2.0，学习资料与软体。\n会说中文，也会说 English.\n"
    assert (directory / "transcript.simplified.txt").read_text() == simplified
    for name in ("notes.md", "transcript.simplified.md"):
        text = (directory / name).read_text()
        assert "学习 \\[测试\\]" in text
        assert "这个 Python API v2.0，学习资料与软体。" in text
        assert "会说中文，也会说 English." in text
        assert "00:00:00.125 → 00:00:02.500" in text
        assert "00:00:05.250 → 00:00:08.000" in text
        assert source.as_uri() in text
        assert str(source) in text  # metadata retains the exact source path
        assert 'text_script: "simplified"' in text
    srt = (directory / "transcript.simplified.srt").read_text()
    assert "00:00:00,125 --> 00:00:02,500" in srt
    assert "00:00:05,250 --> 00:00:08,000" in srt
    assert "会说中文，也会说 English." in srt
    assert "(transcript.simplified.md#segment-2)" in (directory / "notes.md").read_text()
