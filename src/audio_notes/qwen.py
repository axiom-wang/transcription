"""Offline Apple Silicon ASR adapter; no model or GPU imports in the parent process."""

from importlib.metadata import version
from pathlib import Path


def speech_regions(audio, enabled):
    """Keep original sample offsets and silence between nearby utterances."""
    if not len(audio):
        return []
    if not enabled:
        return [{"start": 0, "end": len(audio)}]
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    spans = get_speech_timestamps(
        audio,
        VadOptions(min_silence_duration_ms=500, speech_pad_ms=200),
    )
    regions = []
    for span in spans:
        if (
            regions
            and span["start"] - regions[-1]["end"] <= 16000
            and span["end"] - regions[-1]["start"] <= 30 * 16000
        ):
            regions[-1]["end"] = span["end"]
        else:
            regions.append(dict(span))
    return regions


def transcribe_local(source, directory, settings, on_progress=None):
    import mlx.core as mx
    from faster_whisper.audio import decode_audio
    from mlx_qwen3_asr import transcribe
    from mlx_qwen3_asr.forced_aligner import ForcedAligner

    if not mx.metal.is_available():
        raise RuntimeError("Qwen3-ASR 需要 Apple Silicon 与可用的 Metal GPU")
    # PyAV supports the same audio/video containers as the previous engine;
    # passing samples avoids an external ffmpeg executable or PATH dependency.
    audio = decode_audio(str(source), sampling_rate=16000)
    print(f"解码音频完成：{source}", flush=True)
    duration = len(audio) / 16000
    regions = speech_regions(audio, settings["vad_filter"])
    language = {"zh": "Chinese", "en": "English", "auto": None}[settings["language"]]
    aligner = ForcedAligner(str(Path(directory) / "aligner"), dtype=mx.float16)
    saved = []
    native = []
    detected_language = language or "unknown"

    for region in regions:
        offset = region["start"] / 16000

        def progress(event):
            if event["event"] == "chunk_completed":
                done = offset + event["processed_audio_sec"]
                if on_progress:
                    on_progress(min(done, duration), duration)
                print(f"识别进度：{done:.1f} / {duration:.1f} 秒", flush=True)

        result = transcribe(
            audio[region["start"] : region["end"]],
            model=str(directory),
            language=language,
            context=settings["initial_prompt"],
            dtype=mx.float16,
            return_timestamps=settings["word_timestamps"],
            return_chunks=True,
            forced_aligner=aligner,
            on_progress=progress,
        )
        if result.truncated:
            raise RuntimeError("Qwen 输出达到长度上限，未将不完整转写保存为成功结果")
        if result.language != "unknown":
            detected_language = result.language
        native.append({"offset": offset, "text": result.text, "chunks": result.chunks})
        for chunk in result.chunks or []:
            text = chunk["text"]
            if not text.strip():
                continue
            words = []
            chunk_start = offset + chunk["start"]
            chunk_end = min(duration, offset + chunk["end"])
            for word in result.segments or []:
                if chunk["start"] <= word["start"] < chunk["end"]:
                    start = min(chunk_end, max(chunk_start, offset + word["start"]))
                    end = min(chunk_end, max(start, offset + word["end"]))
                    words.append({"word": word["text"], "start": start, "end": end})
            if settings["word_timestamps"] and not words and any(c.isalnum() for c in text):
                raise RuntimeError("时间戳对齐未返回有效词边界，未保存不完整的转写结果")
            # Keep the recognizer's complete text, including punctuation.
            # Forced alignment tokenizes text and must never replace that text.
            if saved and saved[-1]["text"][-1:].isascii() and text[:1].isascii():
                if saved[-1]["text"][-1:].isalnum() and text[:1].isalnum():
                    text = " " + text
            saved.append(
                {
                    "id": len(saved),
                    "start": words[0]["start"] if words else offset + chunk["start"],
                    "end": words[-1]["end"] if words else min(duration, offset + chunk["end"]),
                    "text": text,
                    "words": words if settings["word_timestamps"] else None,
                }
            )
        mx.clear_cache()

    info = {
        "language": {"Chinese": "zh", "English": "en"}.get(detected_language, detected_language),
        "duration": duration,
        "duration_after_vad": sum(r["end"] - r["start"] for r in regions) / 16000,
        "timestamp_method": "Qwen3-ForcedAligner-0.6B" if settings["word_timestamps"] else "chunk",
    }
    engine = {
        "name": "mlx-qwen3-asr",
        "version": version("mlx-qwen3-asr"),
        "mlx_version": version("mlx"),
        "av_version": version("av"),
        "device": "metal",
        "precision": "float16",
        "peak_gpu_memory_bytes": mx.get_peak_memory(),
    }
    return saved, info, engine, native


def doctor(directory):
    import mlx.core as mx
    from mlx_qwen3_asr import load_model
    from mlx_qwen3_asr.forced_aligner import ForcedAligner

    if not mx.metal.is_available():
        raise RuntimeError("Qwen3-ASR 需要 Apple Silicon 与可用的 Metal GPU")
    load_model(str(directory), dtype=mx.float16)
    aligner = ForcedAligner(str(Path(directory) / "aligner"), dtype=mx.float16)
    aligner._ensure_loaded()
    print(f"mlx-qwen3-asr: {version('mlx-qwen3-asr')}；MLX: {version('mlx')}")
    print(f"GPU: {mx.device_info()['device_name']}；精度：float16（未量化）")
    print(f"识别与时间戳模型均加载成功：{directory}")
