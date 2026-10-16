"""Faithful rendering: never summarize, omit repetitions, or invent topics."""

import json
import re
from functools import lru_cache
from pathlib import Path

from opencc import OpenCC

from .diarization import speaker_label
from .files import atomic_text


@lru_cache(maxsize=1)
def chinese_converter():
    # Standard script conversion only; do not substitute regional vocabulary.
    return OpenCC("t2s.json")


def simplified_text(text):
    return chinese_converter().convert(text)


def timestamp(seconds):
    millis = max(0, round(seconds * 1000))
    seconds, ms = divmod(millis, 1000)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{sec:02d}.{ms:03d}"


def clean_text(text):
    return re.sub(r"\s+", " ", text).strip()


def join_text(parts):
    text = ""
    for part in parts:
        part = clean_text(part)
        if not part:
            continue
        separator = " " if text and text[-1].isascii() and part[0].isascii() else ""
        text += separator + part
    return text


def escape_inline(text):
    return re.sub(r"([\\`*_{}\[\]<>#!|~])", r"\\\1", text)


def frontmatter(raw, kind, simplified=False):
    fields = {
        "title": raw["source"]["name"],
        "type": kind,
        "source": raw["source"]["path"],
        "audio_sha256": raw["audio_sha256"],
        "created": raw["created_at"],
        "language": raw["info"]["language"],
        "model": raw["settings"]["model"],
        "duration_seconds": raw["info"]["duration"],
        "job_id": raw["job_id"],
        "tags": ["录音", "转写"],
    }
    if simplified:
        fields["title"] = simplified_text(fields["title"])
        fields["text_script"] = "simplified"
        fields["text_conversion"] = "OpenCC t2s"
    origin = raw.get("import_origin", {})
    for key in ("original_path", "recorded_at", "recording_id"):
        if origin.get(key):
            fields[key] = origin[key]
    return (
        "---\n"
        + "\n".join(
            f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items()
        )
        + "\n---\n\n"
    )


def header(raw, kind, simplified=False):
    source = raw["source"]["path"]
    uri = Path(source).as_uri()
    title = raw["source"]["name"]
    if simplified:
        title = simplified_text(title)
    title = escape_inline(title)
    result = (
        frontmatter(raw, kind, simplified)
        + f"# {title}\n\n[音频副本]({uri}) · [原始 JSON](raw.json)"
    )
    original = raw.get("import_origin", {}).get("original_path")
    if original:
        result += f" · [语音备忘录原文件]({Path(original).as_uri()})"
    return result + "\n\n"


def speaker_notice(raw):
    if raw.get("diarization"):
        return "> 说话人编号由本机声音模型自动区分，仅在本录音内有效，不代表真实身份；归属待确认和重叠发言请对照录音。\n\n"
    return ""


def render_transcript(raw, segments, simplified=False):
    transcript = header(raw, "transcript", simplified)
    if simplified:
        transcript += (
            "> 本地语音模型完整识别内容，经本地 OpenCC t2s 转为简体；"
            "时间戳为估计值，存疑内容请对照音频。\n\n"
            "[模型原始逐段稿](transcript.md)\n\n"
        )
    else:
        transcript += "> 本地语音模型识别原文；时间戳为估计值，存疑内容请对照音频。\n\n"
    transcript += speaker_notice(raw) + "## 完整逐段转写\n\n"
    source_anchors = set()
    for i, segment in enumerate(segments, 1):
        original = segment.get("original_segment", i)
        anchor = ""
        if original not in source_anchors:
            anchor += f'<a id="segment-{original}"></a>\n\n'
            source_anchors.add(original)
        if raw.get("diarization"):
            anchor += f'<a id="turn-{i}"></a>\n\n'
        transcript += (
            anchor
            + f"**[{timestamp(segment['start'])} → {timestamp(segment['end'])}]**\n\n"
            + (f"**{speaker_label(segment)}**\n\n" if speaker_label(segment) else "")
            + f"{escape_inline(segment['text']).replace(chr(10), '  ' + chr(10))}\n\n"
        )
    if not segments:
        transcript += "未识别出语音。请对照原始音频检查。\n"
    return transcript


def render_srt(segments):
    return "\n".join(
        f"{i}\n{timestamp(s['start']).replace('.', ',')} --> "
        f"{timestamp(s['end']).replace('.', ',')}\n"
        f"{speaker_label(s) + '：' if speaker_label(s) else ''}{s['text'].strip()}\n"
        for i, s in enumerate(segments, 1)
    )


def render(raw, directory: Path, options):
    original_segments = raw["segments"]
    # Convert only human-readable text, never filenames, URLs, identifiers or raw data.
    segments = [{**s, "text": simplified_text(s["text"])} for s in original_segments]
    readable = header(raw, "reading-note", simplified=True)
    readable += (
        "> 此阅读版保留全部识别内容，经本地 OpenCC t2s 转为简体，"
        "合并空白、按停顿分段并按时间分节；"
        "未生成摘要、主题、结论或待办，也未删去口头禅和重复内容。\n\n"
        "[简体完整逐段稿](transcript.simplified.md) · [模型原始逐段稿](transcript.md)\n\n"
    )
    readable += speaker_notice(raw)
    paragraphs = []
    current = []
    chars = 0
    for i, segment in enumerate(segments, 1):
        if current and (
            (segment.get("speaker"), segment.get("overlap"))
            != (current[-1][1].get("speaker"), current[-1][1].get("overlap"))
            or segment["start"] - current[-1][1]["end"] >= options["paragraph_gap_seconds"]
            or chars + len(segment["text"]) > options["paragraph_max_chars"]
            or int(segment["start"] // options["section_seconds"])
            != int(current[0][1]["start"] // options["section_seconds"])
        ):
            paragraphs.append(current)
            current, chars = [], 0
        current.append((i, segment))
        chars += len(segment["text"])
    if current:
        paragraphs.append(current)
    section = None
    for paragraph in paragraphs:
        first, last = paragraph[0][1], paragraph[-1][1]
        bucket = int(first["start"] // options["section_seconds"])
        if bucket != section:
            section = bucket
            readable += f"## {timestamp(bucket * options['section_seconds'])}\n\n"
        readable += (
            f"[{timestamp(first['start'])} → {timestamp(last['end'])}]"
            f"(transcript.simplified.md#{'turn' if raw.get('diarization') else 'segment'}-{paragraph[0][0]})\n\n"
            + (f"**{speaker_label(first)}**\n\n" if speaker_label(first) else "")
            + escape_inline(join_text([s["text"] for _, s in paragraph]))
            + "\n\n"
        )
    if not segments:
        readable += "未识别出语音。请对照原始音频检查。\n"
    atomic_text(directory / "transcript.md", render_transcript(raw, original_segments))
    atomic_text(
        directory / "transcript.simplified.md", render_transcript(raw, segments, simplified=True)
    )
    atomic_text(directory / "notes.md", readable)
    for suffix, data in (("", original_segments), (".simplified", segments)):
        atomic_text(directory / f"transcript{suffix}.srt", render_srt(data))
        atomic_text(
            directory / f"transcript{suffix}.txt",
            "\n".join(
                (speaker_label(s) + "：" if speaker_label(s) else "") + s["text"] for s in data
            )
            + "\n",
        )
