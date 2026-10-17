"""Stage estimates learned from local timing samples, with explicit cold-start bounds."""

import json
import statistics
import time
from functools import lru_cache
from pathlib import Path

from .knowledge import Knowledge


def record(config, job_id, stage, duration, elapsed):
    if duration and duration > 0 and elapsed >= 0:
        Knowledge(config.state).put(
            "timings",
            job_id + ":" + stage,
            {"stage": stage, "duration": duration, "elapsed": elapsed},
        )


@lru_cache(maxsize=512)
def _duration(path, size, modified):
    try:
        import av

        with av.open(path) as audio:
            if audio.duration:
                return float(audio.duration / av.time_base)
            stream = audio.streams.audio[0]
            return float(stream.duration * stream.time_base) if stream.duration else None
    except Exception:
        # Best-effort probe. PyAV also raises EOFError/LookupError subclasses for damaged or
        # half-synced files; one such file must not break the whole library listing.
        return None


def audio_duration(path):
    try:
        stat = Path(path).stat()
        return _duration(str(path), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return None


def estimates(config, items, requests=None, backlog=None):
    samples = Knowledge(config.state).all("timings")
    ratios = {}
    for stage, default in [("asr", 0.3), ("speaker", 0.2), ("text", 0.5)]:
        values = [s["elapsed"] / s["duration"] for s in samples if s["stage"] == stage][-20:]
        ratios[stage] = (
            statistics.median(values) if len(values) >= 3 else default,
            len(values) < 3,
        )
    wait = 0
    # Pipeline consumes recordings in creation order; active work always comes first.
    pending = sorted(
        items,
        key=lambda i: (
            not any(i.get(s + "_status") == "running" for s in ("asr", "speaker", "text")),
            i["created"],
        ),
    )
    for item in pending:
        times = {}
        offset = wait
        duration = item.get("duration")
        for stage in ("asr", "speaker", "text"):
            status = item.get(stage + "_status")
            factor, rough = ratios[stage]
            if status in ("completed", "disabled"):
                times[stage] = {
                    "seconds": 0,
                    "ready": status == "completed",
                    "disabled": status == "disabled",
                    "rough": False,
                }
                continue
            remaining = (duration or 600) * factor
            overdue = False
            if status == "running":
                progress = item.get("progress", {})
                elapsed = max(
                    0, time.time() - progress.get("started", item.get("updated", time.time()))
                )
                overdue = elapsed > remaining
                if progress.get("total", 0) > 0 and progress.get("completed", 0) > 0:
                    fraction = min(1, progress["completed"] / progress["total"])
                    remaining = elapsed * (1 - fraction) / fraction
                else:
                    remaining = max(0, remaining - elapsed)
            offset += remaining
            times[stage] = {
                "seconds": round(offset),
                "rough": rough or duration is None,
                "overdue": overdue,
                "ready": False,
            }
        item["estimates"] = times
        if any(
            item.get(s + "_status") not in ("completed", "disabled")
            for s in ("asr", "speaker", "text")
        ):
            wait = offset

    def waiting_estimate(duration, only_text=False):
        nonlocal wait
        times = {}
        for stage in ("asr", "speaker", "text"):
            disabled = (stage == "speaker" and not config.speakers.get("enabled")) or (
                stage == "text" and not config.analysis.get("enabled")
            )
            if disabled or (only_text and stage != "text"):
                times[stage] = {
                    "ready": only_text and not disabled,
                    "disabled": disabled,
                    "seconds": 0,
                    "rough": False,
                }
                continue
            factor, rough = ratios[stage]
            wait += (duration or 600) * factor
            times[stage] = {
                "seconds": round(wait),
                "rough": rough or duration is None,
                "ready": False,
            }
        return times

    for request in sorted(requests or [], key=lambda r: (-r.get("priority", 0), r["created"])):
        if request["status"] != "queued":
            continue
        payload = json.loads(request.get("payload") or "{}")
        if request["kind"] == "transcribe":
            request["recordings"] = []
            for path in payload.get("paths", []):
                duration = audio_duration(path)
                request["recordings"].append(
                    {
                        "name": Path(path).name,
                        "duration": duration,
                        "estimates": waiting_estimate(duration),
                    }
                )
        elif request["kind"] in ("enrich", "diarize"):
            item = next((i for i in items if i["id"] == payload.get("job_id")), None)
            request["estimates"] = item.get("estimates") if item else waiting_estimate(None, True)
    for recording in backlog or []:
        recording["estimates"] = waiting_estimate(recording.get("duration"))
