"""Authenticated knowledge operations shared by the desktop and web UI."""

import hashlib
import json
import subprocess
import time
from pathlib import Path

from .control import read_json
from .knowledge import SCENES, Knowledge, cosine
from .workbench_library import detail, job, library


def selected_details(app, filters):
    k = Knowledge(app.config.state)
    members = set()
    if filters.get("person"):
        members = {
            m["recording"]
            for m in k.all("members")
            if m.get("person") == filters["person"] and m.get("confirmed")
        }
    for item in library(app.config, app.store):
        if filters.get("ids") and item["id"] not in filters["ids"]:
            continue
        if filters.get("person") and item["id"] not in members:
            continue
        if filters.get("favorite") and not item["favorite"]:
            continue
        when = (
            item.get("recorded_at") or time.strftime("%Y-%m-%d", time.localtime(item["created"]))
        )[:10]
        if (
            filters.get("from")
            and when < filters["from"]
            or filters.get("to")
            and when > filters["to"]
        ):
            continue
        d = detail(app.config, app.store, item["id"])
        if (
            filters.get("scene")
            and (
                d["smart"].get("detected_scene")
                if d["smart"]["scene"] == "auto"
                else d["smart"]["scene"]
            )
            != filters["scene"]
        ):
            continue
        yield d


def search(app, filters, terms=None):
    query = str(filters.get("query", "")).strip().casefold()
    if not query:
        return []
    if isinstance(filters.get("ids"), str):
        filters = {**filters, "ids": [i for i in filters["ids"].split(",") if i]}
    terms = terms or [query]
    hits = []
    for d in selected_details(app, filters):
        for i, s in enumerate(d["segments"]):
            score = sum(1 for t in terms if t.casefold() in s["text"].casefold())
            if score:
                hits.append(
                    {
                        "recording": d["id"],
                        "title": d["title"],
                        **s,
                        "score": score,
                        "before": d["segments"][i - 1]["text"] if i else "",
                        "after": d["segments"][i + 1]["text"] if i + 1 < len(d["segments"]) else "",
                    }
                )
    return sorted(hits, key=lambda h: -h["score"])[:100]


def knowledge_read(app, params):
    k = Knowledge(app.config.state)
    kind = params.get("kind")
    if kind == "terms":
        return {"items": k.all("terms")}
    if kind == "query":
        return k.get("queries", params.get("id", ""))
    if kind == "search":
        return {"items": search(app, params)}
    if kind == "tasks":
        # Populate new source tasks without replacing user changes.
        for _ in selected_details(app, {}):
            pass
        return {"items": k.all("tasks")}
    if kind == "people":
        people = k.all("people")
        members = k.all("members")
        return {
            "items": [{key: value for key, value in p.items() if key != "voices"} for p in people],
            "members": members,
        }
    if kind == "person":
        return {
            "items": [
                {
                    "id": d["id"],
                    "title": d["title"],
                    "segments": [
                        s
                        for s in d["segments"]
                        if any(
                            m.get("person") == params.get("person")
                            and m["recording"] == d["id"]
                            and m["speaker"] == s.get("speaker")
                            and m.get("confirmed")
                            for m in k.all("members")
                        )
                    ],
                }
                for d in selected_details(app, params)
            ]
        }
    raise ValueError("未知资料类型")


def knowledge_action(app, data):
    from .files import run_lock

    with run_lock(app.config.state / "knowledge-write", blocking=True):
        return _knowledge_action(app, data)


def _knowledge_action(app, data):
    action = data.get("action")
    if action not in {
        "auto_scan",
        "preferences",
        "term",
        "delete_term",
        "import_terms",
        "preview_terms",
        "apply_terms",
        "revise",
        "undo_revision",
        "review_segment",
        "scene",
        "task",
        "merge_tasks",
        "reminder",
        "remember_voice",
        "delete_person",
        "query",
        "reveal_txt",
        "seen_results",
    }:
        return False, None
    k = Knowledge(app.config.state)
    if action == "auto_scan":
        app.watcher.dirty = True
        return True, {"ok": True}
    if action == "preferences":
        return True, k.preferences(data["changes"])
    if action == "seen_results":
        return True, k.put("settings", "seen", {"time": time.time()})
    if action == "reveal_txt":
        subprocess.run(["/usr/bin/open", str(app.config.txt_output)], check=True, timeout=10)
        return True, {"ok": True}
    if action == "term":
        return True, k.term(data["term"])
    if action == "delete_term":
        k.delete("terms", data["term_id"])
        return True, {"ok": True}
    if action == "import_terms":
        terms = data.get("terms")
        if not isinstance(terms, list) or len(terms) > 2000:
            raise ValueError("词库格式无效，最多导入 2000 项")
        # Validate everything first, so a bad row cannot partially import.
        for t in terms:
            if (
                not isinstance(t, dict)
                or not isinstance(t.get("correct"), str)
                or not 0 < len(t["correct"].strip()) <= 100
                or not isinstance(t.get("wrong", []), list)
                or len(t.get("wrong", [])) > 50
                or any(
                    not isinstance(w, str) or not 0 < len(w.strip()) <= 100
                    for w in t.get("wrong", [])
                )
            ):
                raise ValueError("词库条目无效")
        return True, {"items": [k.term(t) for t in terms]}
    if action == "preview_terms":
        hits = []
        for d in selected_details(app, {}):
            for s in d["segments"]:
                for t in k.all("terms"):
                    for wrong in t["wrong"]:
                        if wrong in s["text"]:
                            hits.append(
                                {
                                    "recording": d["id"],
                                    "title": d["title"],
                                    "segment": s["id"],
                                    "start": s["start"],
                                    "before": s["text"],
                                    "old": wrong,
                                    "new": t["correct"],
                                }
                            )
        token = hashlib.sha256(json.dumps(hits, sort_keys=True).encode()).hexdigest()
        k.put("previews", token, {"hits": hits, "created": time.time()})
        return True, {"hits": hits, "token": token}
    if action == "apply_terms":
        preview = k.get("previews", data.get("token", ""))
        if not preview or time.time() - preview["created"] > 3600:
            raise ValueError("预览已过期，请重新预览")
        details = {
            h["recording"]: detail(app.config, app.store, h["recording"]) for h in preview["hits"]
        }
        for h in preview["hits"]:
            s = next(s for s in details[h["recording"]]["segments"] if s["id"] == h["segment"])
            if s["text"] != h["before"]:
                raise ValueError("原文修订已变化，请重新预览")
        # Apply all matched substitutions per segment in a single reversible revision.
        grouped = {}
        for h in preview["hits"]:
            group = grouped.setdefault((h["recording"], h["segment"]), [h["before"], h["before"]])
            group[1] = group[1].replace(h["old"], h["new"])
        for (recording, segment), (before, after) in grouped.items():
            k.revise(recording, details[recording]["segments"], before, after, "one", segment)
        k.delete("previews", data["token"])
        return True, {"count": len(grouped)}
    if action == "query":
        filters = data.get("filters", {})
        if (
            not isinstance(filters, dict)
            or not isinstance(filters.get("query"), str)
            or not 0 < len(filters["query"].strip()) <= 1000
        ):
            raise ValueError("请输入不超过 1000 字的问题")
        if not app.config.analysis.get("enabled"):
            raise ValueError("请先准备本地文本模型；关键字搜索仍可使用")
        request = app.store.enqueue(
            "knowledge",
            {"filters": filters, "answer": bool(data.get("answer"))},
            "本地资料检索与问答",
        )
        return True, {"id": request["id"]}
    if action == "task":
        return True, k.task(data["task"])
    if action == "merge_tasks":
        ids = data.get("ids", [])
        if len(ids) < 2 or len(set(ids)) != len(ids):
            raise ValueError("请选择至少两条不同待办")
        tasks = [k.get("tasks", i) for i in ids]
        if not all(tasks):
            raise ValueError("待办不存在")
        target = tasks[0]
        target["text"] = data.get("text") or target["text"]
        target["sources"] = [
            dict(s, recording=s.get("recording", t.get("recording")))
            for t in tasks
            for s in t.get("sources", [])
        ]
        k.task(target)
        for t in tasks[1:]:
            k.task({**t, "status": "ignored"})
        return True, target
    if action == "reminder":
        t = k.get("tasks", data["task_id"])
        if not t:
            raise ValueError("待办不存在")
        note = "\n".join(
            f"{s.get('recording', t.get('recording', ''))} · {s.get('start', 0)} 秒：{s['text']}"
            for s in t.get("sources", [])
        )
        script = """on run argv
 tell application "Reminders"
 make new reminder with properties {name:item 1 of argv, body:item 2 of argv}
 end tell
end run"""
        subprocess.run(
            ["/usr/bin/osascript", "-", t["text"], note],
            input=script,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return True, {"ok": True}
    if action == "delete_person":
        person = data["person_id"]
        for m in k.all("members"):
            if m.get("person") == person:
                try:
                    entry = job(app.config, m["recording"])
                except FileNotFoundError:
                    k.delete("members", m["id"])
                    continue
                from .files import atomic_json

                directory = Path(entry["output"])
                # Remove learned source vectors as well as the named profile. ASR is untouched.
                for file in [
                    directory / "speakers.json",
                    *directory.glob("speaker-processing/**/turns.json"),
                    *directory.glob("speaker-processing/**/previous-speakers.json"),
                ]:
                    voice_data = read_json(file)
                    if m["speaker"] in voice_data.get("embeddings", {}):
                        del voice_data["embeddings"][m["speaker"]]
                        atomic_json(file, voice_data)
                app.store.rename_speaker(entry["audio_hash"], m["speaker"], "")
                k.delete("members", m["id"])
        k.delete("people", person)
        return True, {"ok": True}
    d = detail(app.config, app.store, data.get("id", ""))
    key = d["id"]
    if action == "revise":
        view = data.get("view", "original")
        if view not in ("original", "body"):
            raise ValueError("修订视图无效")
        return True, k.revise(
            key,
            d["smart"]["body"] if view == "body" else d["segments"],
            data.get("old"),
            data.get("new"),
            data.get("scope"),
            data.get("segment"),
            data.get("offset"),
            view=view,
        )
    if action == "undo_revision":
        return True, k.undo(key, data["revision_id"])
    if action == "review_segment":
        s = next((s for s in d["segments"] if s["id"] == data.get("segment")), None)
        if not s or data.get("signature") != s["review_signature"]:
            raise ValueError("片段已变化，请重新核对")
        confirmed = k.get("review", key).get("confirmed", {})
        confirmed[str(s["id"])] = s["review_signature"]
        return True, k.put("review", key, {"confirmed": confirmed})
    if action == "scene":
        scene = data.get("scene")
        if scene not in SCENES:
            raise ValueError("场景无效")
        if not app.config.analysis.get("enabled"):
            raise ValueError("请先启用本地文本模型")
        k.put("scene", key, {"scene": scene})
        return True, app.store.enqueue(
            "enrich", {"job_id": key, "force": True}, "按场景重新生成智能稿"
        )
    if action == "remember_voice":
        speaker, name = data.get("speaker"), data.get("name", "").strip()
        voices = read_json(Path(d["output"]) / "speakers.json").get("embeddings", {})
        vector = voices.get(speaker)
        if not vector or not all(isinstance(v, (int, float)) for v in vector):
            raise ValueError("这条录音尚无声纹，请先补做说话人区分")
        if not name:
            raise ValueError("请填写人物姓名")
        person_id = data.get("person_id") or hashlib.sha256(name.encode()).hexdigest()[:24]
        person = k.get("people", person_id, {"id": person_id, "voices": []})
        app.store.rename_speaker(d["audio_hash"], speaker, name)
        # One contribution per recording/speaker; correcting a mistaken identity removes it.
        member_id = key + ":" + speaker
        old = k.get("members", member_id)
        if old.get("person") and old["person"] != person_id:
            previous = k.get("people", old["person"])
            if previous:
                previous["voices"] = [v for v in previous["voices"] if v["source"] != member_id]
                k.put("people", old["person"], previous)
        person["voices"] = [v for v in person["voices"] if v["source"] != member_id] + [
            {"source": member_id, "vector": vector}
        ]
        person.update(name=name, me=bool(data.get("me")))
        if person["me"]:
            for p in k.all("people"):
                if p.get("me") and p["id"] != person_id:
                    k.put("people", p["id"], {**p, "me": False})
        k.put("people", person_id, person)
        k.put(
            "members",
            member_id,
            {
                "id": member_id,
                "recording": key,
                "speaker": speaker,
                "person": person_id,
                "confirmed": True,
            },
        )
        return True, {"ok": True}
    raise ValueError("未知操作")


def match_voices(config, recording, audio_hash, embeddings):
    k = Knowledge(config.state)
    for speaker, vector in embeddings.items():
        member_id = recording + ":" + speaker
        if k.get("members", member_id).get("confirmed"):
            continue
        matches = sorted(
            (
                (max((cosine(vector, v["vector"]) for v in p["voices"]), default=0), p)
                for p in k.all("people")
            ),
            key=lambda x: x[0],
            reverse=True,
        )
        if not matches or matches[0][0] < 0.65:
            continue
        score, person = matches[0]
        high = score >= 0.85 and (len(matches) == 1 or score - matches[1][0] >= 0.08)
        k.put(
            "members",
            member_id,
            {
                "id": member_id,
                "recording": recording,
                "speaker": speaker,
                "person": person["id"],
                "confirmed": high,
                "score": score,
            },
        )
        # The suggestion prefix must not push a valid 40-character name over the limit.
        k.store.rename_speaker(
            audio_hash, speaker, person["name"] if high else f"可能是 {person['name'][:35]}？"
        )


def forget_voice_assignment(config, recording, speaker, new_name):
    k = Knowledge(config.state)
    key = recording + ":" + str(speaker)
    member = k.get("members", key)
    person = k.get("people", member.get("person", ""))
    if not person or person["name"] == new_name:
        return
    person["voices"] = [v for v in person["voices"] if v["source"] != key]
    k.put("people", person["id"], person)
    k.delete("members", key)
