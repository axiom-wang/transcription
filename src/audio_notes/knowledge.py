"""Local knowledge and reversible revisions. Never modifies inference artifacts."""

import hashlib
import json
import math
import re
import time
import uuid
from difflib import SequenceMatcher
from pathlib import Path

from .control import read_json
from .workbench_store import Conflict, WorkbenchStore

SCENES = {
    "auto": "自动",
    "meeting": "会议纪要",
    "interview": "访谈",
    "class": "课堂 / 学习",
    "thinking": "个人思考",
    "podcast": "讲座 / 播客",
}
SCENE_PROMPTS = {
    "auto": "",
    "meeting": "侧重议题、提议、共识、决定、行动项，严格区分讨论与决定。",
    "interview": "按问答脉络组织主题，提炼关键观点和可以直接引用的原话金句。",
    "class": "按知识点、论据与例子、适用条件和原文疑问组织主题。",
    "thinking": "按想法脉络、卡点和下一步组织主题。",
    "podcast": "按章节组织主题与要点，引用每章开头的原文以生成时间目录。",
}
DEFAULTS = {"automatic": True, "timing": "balanced", "notification": "stages", "scene": "auto"}


class Knowledge:
    def __init__(self, state):
        self.store = WorkbenchStore(state)
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS knowledge (
                kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                updated REAL NOT NULL, PRIMARY KEY(kind,id))""")

    def all(self, kind):
        with self.store.connect() as db:
            return [
                json.loads(r["data"])
                for r in db.execute(
                    "SELECT data FROM knowledge WHERE kind=? ORDER BY updated,id", (kind,)
                )
            ]

    def get(self, kind, key, default=None):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT data FROM knowledge WHERE kind=? AND id=?", (kind, key)
            ).fetchone()
        return json.loads(row["data"]) if row else ({} if default is None else default)

    def put(self, kind, key, data):
        with self.store.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO knowledge VALUES (?,?,?,?)",
                (kind, key, json.dumps(data, ensure_ascii=False), time.time()),
            )
        return data

    def delete(self, kind, key):
        with self.store.connect() as db:
            db.execute("DELETE FROM knowledge WHERE kind=? AND id=?", (kind, key))

    def preferences(self, changes=None):
        prefs = {**DEFAULTS, **self.get("settings", "preferences")}
        if changes is not None:
            if set(changes) - set(DEFAULTS):
                raise ValueError("不支持的设置")
            prefs.update(changes)
            if (
                not isinstance(prefs["automatic"], bool)
                or prefs["timing"] not in ("saving", "balanced", "instant")
                or prefs["notification"] not in ("stages", "complete")
                or prefs["scene"] not in SCENES
            ):
                raise ValueError("设置值无效")
            self.put("settings", "preferences", prefs)
        return prefs

    def scene(self, key, directory):
        selected = self.get("scene", key).get("scene")
        if selected:
            return selected
        receipt = read_json(directory / "enrichment.complete.json")
        return receipt.get("scene", "auto") if receipt else self.preferences()["scene"]

    def term(self, data):
        correct = data.get("correct", "").strip()
        wrong = data.get("wrong", [])
        if (
            not correct
            or len(correct) > 100
            or not isinstance(wrong, list)
            or len(wrong) > 50
            or any(not isinstance(w, str) or not w.strip() or len(w) > 100 for w in wrong)
        ):
            raise ValueError("请填写正确写法与有效的错误写法（每项最多 100 字）")
        key = data.get("id") or hashlib.sha256(correct.encode()).hexdigest()[:24]
        previous = self.get("terms", key)
        return self.put(
            "terms",
            key,
            {
                "id": key,
                "correct": correct,
                "wrong": list(dict.fromkeys(w.strip() for w in wrong if w.strip() != correct)),
                "source": previous.get("source") or data.get("source", ""),
            },
        )

    def glossary(self):
        return "\n".join(
            t["correct"] + ("（常见误写：" + "、".join(t["wrong"]) + "）" if t["wrong"] else "")
            for t in self.all("terms")
        )[:6000]

    def revise(self, key, segments, old, new, scope, segment=None, offset=None, view="original"):
        if not isinstance(old, str) or not old or not isinstance(new, str) or len(new) > 10000:
            raise ValueError("修订文字无效")
        if scope == "glossary" and (not new.strip() or len(new.strip()) > 100 or len(old) > 100):
            raise ValueError("词库写法最多 100 字且不能为空")
        if scope not in ("one", "recording", "glossary"):
            raise ValueError("修订范围无效")
        edits = []
        for s in segments:
            if scope == "one" and s["id"] != segment:
                continue
            before = s["text"]
            if old not in before:
                continue
            if scope == "one":
                index = before.find(old) if offset is None else int(offset)
                if index < 0 or before[index : index + len(old)] != old:
                    raise Conflict("文字已变化，请重新选择")
                after = before[:index] + new + before[index + len(old) :]
            else:
                after = before.replace(old, new)
            edits.append({"segment": s["id"], "before": before, "after": after})
        if not edits:
            raise Conflict("没有匹配的文字，请刷新后重试")
        revision = {
            "id": uuid.uuid4().hex,
            "recording": key,
            "edits": edits,
            "created": time.time(),
            "active": True,
            "view": view,
        }
        self.put("revisions", revision["id"], revision)
        if scope == "glossary":
            existing = next((t for t in self.all("terms") if t["correct"] == new), {})
            self.term(
                {
                    **existing,
                    "correct": new,
                    "wrong": [*existing.get("wrong", []), old],
                    "source": key,
                }
            )
        return revision

    def overlay(self, key, segments, view="original"):
        segments = [dict(s, original_text=s["text"]) for s in segments]
        by_id = {s["id"]: s for s in segments}
        for r in self.all("revisions"):
            if r["recording"] != key or not r["active"] or r.get("view", "original") != view:
                continue
            for edit in r["edits"]:
                s = by_id.get(edit["segment"])
                if s and s["text"] == edit["before"]:
                    s["text"] = edit["after"]
                    s["revised"] = True
        return segments

    def undo(self, key, revision):
        item = self.get("revisions", revision)
        if item.get("recording") != key:
            raise ValueError("修订不存在")
        # Undo in reverse order so dependent edits cannot silently disappear.
        active = [r for r in self.all("revisions") if r["recording"] == key and r["active"]]
        if active and active[-1]["id"] != revision:
            raise Conflict("请先撤销较新的修订")
        item["active"] = False
        return self.put("revisions", revision, item)

    def sync_task(self, key, data):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT data FROM knowledge WHERE kind='tasks' AND id=?", (key,)
            ).fetchone()
            previous = json.loads(row["data"]) if row else {}
            if previous.get("edited"):
                return
            data["created"] = previous.get("created", data["created"])
            db.execute(
                "INSERT OR REPLACE INTO knowledge VALUES (?,?,?,?)",
                ("tasks", key, json.dumps(data, ensure_ascii=False), time.time()),
            )

    def prune_tasks(self, recording, generated):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT id,data FROM knowledge WHERE kind='tasks'").fetchall():
                task = json.loads(row["data"])
                if (
                    task.get("recording") == recording
                    and not task.get("edited")
                    and row["id"] not in generated
                ):
                    db.execute("DELETE FROM knowledge WHERE kind='tasks' AND id=?", (row["id"],))

    def task(self, data):
        key = data.get("id") or uuid.uuid4().hex
        previous = self.get("tasks", key)
        item = {
            **previous,
            **{
                k: v
                for k, v in data.items()
                if k in ("text", "status", "owner", "deadline", "recording", "sources")
            },
            "id": key,
        }
        item.setdefault("status", "todo")
        item.setdefault("sources", [])
        if (
            item["status"] not in ("todo", "done", "ignored")
            or not isinstance(item.get("text"), str)
            or not item["text"].strip()
        ):
            raise ValueError("待办内容或状态无效")
        if len(item["text"]) > 10000:
            raise ValueError("待办内容过长")
        item["edited"] = True
        item.setdefault("created", time.time())
        return self.put("tasks", key, item)


def review_context(config, key, directory):
    k = Knowledge(config.state)
    signatures = []
    for name in ("raw.json", "speakers.json", "enrichment.complete.json"):
        path = directory / name
        signatures.append((name, path.stat().st_mtime_ns if path.exists() else 0))
    signatures += [(r["id"], r["active"]) for r in k.all("revisions") if r["recording"] == key]
    return hashlib.sha256(json.dumps(signatures).encode()).hexdigest()


def generation(directory):
    receipt = read_json(directory / "enrichment.complete.json")
    fingerprint = receipt.get("fingerprint", "")
    if not re.fullmatch("[a-f0-9]{64}", fingerprint):
        return {}
    return read_json(directory / "enrichment" / fingerprint / "result.json")


def revised_paragraph(paragraph, units, segments):
    """Project source corrections only onto unchanged, attributable paragraph text."""
    sources, selected, basis = {}, [], ""
    for unit in units:
        sid = unit["segment"]
        source = sources.get(sid, "")
        content = unit.get("text", "")
        if unit["id"] in paragraph["unit_ids"]:
            selected.append((sid, len(source), len(source) + len(content), len(basis)))
            basis += content
        sources[sid] = source + content
    blocks = SequenceMatcher(None, basis, paragraph["text"], autojunk=False).get_matching_blocks()
    replacements = []
    for sid, source in sources.items():
        if not source or sid not in segments or not segments[sid].get("revised"):
            continue
        for tag, start, end, left, right in SequenceMatcher(
            None, source, segments[sid]["text"], autojunk=False
        ).get_opcodes():
            if tag == "equal":
                continue
            span = next((p - a for s, a, b, p in selected
                         if s == sid and a <= start and end <= b), None)
            if span is None:
                continue
            start, end = start + span, end + span
            for block in blocks:
                # Boundary insertions cannot be assigned to one paragraph reliably.
                if block.size and block.a <= start and end <= block.a + block.size and (
                    start != end or block.a < start < block.a + block.size
                ):
                    offset = block.b - block.a
                    replacements.append((start + offset, end + offset, segments[sid]["text"][left:right]))
                    break
    text = paragraph["text"]
    boundary = len(text) + 1
    for start, end, replacement in sorted(set(replacements), reverse=True):
        if end > boundary:
            continue
        text = text[:start] + replacement + text[end:]
        boundary = start
    return text


def decorate(config, item):
    k = Knowledge(config.state)
    key = item["id"]
    item["segments"] = k.overlay(key, item["segments"])
    for speaker in item.get("speakers", []):
        member = k.get("members", key + ":" + speaker["id"])
        person = k.get("people", member.get("person", ""))
        speaker.update(
            person=person.get("id"),
            person_name=person.get("name"),
            suggested=bool(person and not member.get("confirmed")),
            me=person.get("me", False),
        )
    segments = {s["id"]: s for s in item["segments"]}
    result = generation(Path(item["output"]))
    analysis = result.get("analysis") or {}
    units = {u["id"]: u["segment"] for u in result.get("units", [])}

    def refs(ids):
        return [
            {
                "segment": sid,
                "start": segments[sid]["start"],
                "text": segments[sid]["original_text"],
                "speaker": segments[sid].get("speaker_label", ""),
            }
            for sid in dict.fromkeys(ids)
            if sid in segments
        ]

    body = []
    for chunk in result.get("polished", []):
        for p in chunk.get("paragraphs", []):
            sources = refs([units[u] for u in p["unit_ids"] if u in units])
            if sources:
                body.append(
                    {
                        "text": revised_paragraph(p, result.get("units", []), segments),
                        "sources": sources,
                    }
                )
    actions = []
    generated_tasks = set()
    for kind in ("decisions", "actions", "questions"):
        for a in analysis.get(kind, []):
            sources = refs(a.get("source_ids", []))
            if not sources:
                continue
            # Only retain named people/dates that are literally in the source.
            original = "".join(s["text"] for s in sources)
            item_action = {**a, "sources": sources, "kind": kind}
            for field in ("owner", "deadline"):
                value = a.get(field, "")
                item_action[field] = (
                    value if value and value != "未明确" and value in original else ""
                )
            if kind == "questions":
                item_action["text"] = "待确认（模型提示）：" + item_action["text"]
            actions.append(item_action)
            if kind == "decisions":
                continue
            identity = hashlib.sha256(
                json.dumps(
                    [key, kind, sorted(a["source_ids"]), a["text"]], ensure_ascii=False
                ).encode()
            ).hexdigest()
            generated_tasks.add(identity)
            previous = k.get("tasks", identity)
            if not previous.get("edited"):
                k.sync_task(
                    identity,
                    {
                        **item_action,
                        "id": identity,
                        "recording": key,
                        "title": item["title"],
                        "status": "todo",
                        "created": previous.get("created", time.time()),
                        "edited": False,
                    },
                )
    if result:
        k.prune_tasks(key, generated_tasks)
    uncertain = set()
    for chunk in result.get("polished", []):
        for u in chunk.get("uncertainties", []):
            uncertain.update(units[i] for i in u.get("unit_ids", []) if i in units)
    if result.get("review_issues"):
        uncertain.update(segments)
    reviewed = k.get("review", key).get("confirmed", {})
    for s in item["segments"]:
        reasons = []
        if s.get("confidence") is not None and s["confidence"] < 0.65:
            reasons.append("识别置信度低")
        if (
            s.get("overlap")
            or s.get("speaker_label") == "说话人待确认"
            or str(s.get("speaker_label", "")).startswith("可能是 ")
        ):
            reasons.append("说话人待确认")
        if s["id"] in uncertain or "〔待核对〕" in s["text"]:
            reasons.append("文字待核对")
        s["review_reasons"] = reasons
        signature = hashlib.sha256(
            json.dumps([s["text"], s.get("speaker"), reasons], ensure_ascii=False).encode()
        ).hexdigest()
        s["review_signature"] = signature
        s["reviewed"] = reviewed.get(str(s["id"])) == signature
    suspects = [s for s in item["segments"] if s["review_reasons"]]
    item["review"] = {
        "total": len(suspects),
        "confirmed": sum(s["reviewed"] for s in suspects),
        "complete": bool(suspects) and all(s["reviewed"] for s in suspects),
    }
    item["needs_review"] = bool(suspects) and not item["review"]["complete"]
    marker = k.get("review", key)
    context = review_context(config, key, Path(item["output"]))
    if marker.get("complete") != item["review"]["complete"] or marker.get("context") != context:
        k.put("review", key, {**marker, "complete": item["review"]["complete"], "context": context})
    body = body or [{"text": s["text"], "sources": refs([s["id"]])} for s in item["segments"]]
    body = k.overlay(key, [dict(p, id=i) for i, p in enumerate(body, 1)], view="body")
    item["smart"] = {
        "brief": analysis.get("brief", {}).get("text", ""),
        "sources": refs(analysis.get("brief", {}).get("source_ids", [])),
        "actions": actions,
        "body": body,
        "themes": analysis.get("themes", []),
        "detected_scene": analysis.get("scene", "auto"),
        "scene": k.scene(key, Path(item["output"])),
    }
    item["revisions"] = [r for r in k.all("revisions") if r["recording"] == key]
    return item


def cosine(a, b):
    if len(a) != len(b) or not a:
        return 0
    norm = math.sqrt(sum(x * x for x in a) * sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / norm if norm else 0
