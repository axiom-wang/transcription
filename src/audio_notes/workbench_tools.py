"""Workbench operations that change files: deleting recordings and handing context to agents."""

import glob
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .control import read_json
from .diarization import rename_speakers
from .files import atomic_text
from .titles import recording_time
from .workbench_library import audio_digest, detail, job, rows

AGENTS = {
    "claude": {
        "name": "Claude Code",
        "command": "claude",
        "install": "https://claude.com/claude-code",
    },
    "codex": {"name": "Codex", "command": "codex", "install": "npm install -g @openai/codex"},
}
CONTEXT_FILE = "录音上下文.md"
AGENT_PROMPT = (
    "请阅读当前目录下的「{file}」。它是一段录音（{title}）的本地转写资料，"
    "作为我们接下来对话的上下文。读完后用三五句话概括要点，然后等待我的问题；不要修改任何文件。"
)
EXTRA_PATHS = (
    "~/.local/bin",
    "~/.claude/local",
    "~/.npm-global/bin",
    "~/.bun/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    # The ChatGPT desktop app bundles the Codex CLI without putting it on PATH.
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin",
    "~/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin",
)


def move_to_trash(path):
    """Recoverable deletion: the macOS Trash, never an unlink of someone's audio."""
    if Path("/usr/bin/trash").is_file():
        result = subprocess.run(
            ["/usr/bin/trash", str(path)], capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0 and not path.exists():
            return
    trash = Path.home() / ".Trash"
    target = trash / path.name
    if target.exists():
        target = trash / f"{path.stem} {time.strftime('%H.%M.%S')}{path.suffix}"
    shutil.move(str(path), str(target))


def _protected(config, path):
    """Apple owns Voice Memos storage; deleting there would desynchronise its library."""
    roots = [
        Path(os.path.expanduser(d)).resolve() for d in config.voice_memos.get("directories", [])
    ]
    resolved = path.resolve()
    return any(resolved.is_relative_to(root) for root in roots)


def delete_recording(config, store, key, delete_audio=False):
    entry = job(config, key)
    audio_hash = entry["audio_hash"]
    group = [r for r in rows(config) if r["audio_hash"] == audio_hash]
    ids = [r["id"] for r in group]
    for version in ids:
        item = detail(config, store, version)
        if "running" in (item["asr_status"], item["text_status"], item["speaker_status"]):
            raise ValueError("这条录音正在处理，请等待当前阶段完成后再删除")
    for request in store.requests():
        if request["status"] in ("queued", "running") and any(
            value in request["payload"] for value in (*ids, audio_hash)
        ):
            raise ValueError("这条录音还有等待中的操作请求，请先在处理队列中取消")
    database = config.state / "jobs.sqlite3"
    with sqlite3.connect(database, timeout=15) as db:
        paths = [r["source"] for r in group] + [
            row[0]
            for row in db.execute(
                f"SELECT path FROM sources WHERE job_id IN ({','.join('?' * len(ids))})", ids
            )
        ]
        paths = list(dict.fromkeys(paths))
        # The tombstone keeps automatic scans and Voice Memos imports from bringing it back.
        db.execute(
            "CREATE TABLE IF NOT EXISTS deleted (audio_hash TEXT PRIMARY KEY, paths TEXT NOT NULL, deleted REAL NOT NULL)"
        )
        db.execute(
            "INSERT OR REPLACE INTO deleted VALUES (?,?,?)",
            (audio_hash, json.dumps(paths, ensure_ascii=False), time.time()),
        )
        marks = ",".join("?" * len(ids))
        db.execute(f"DELETE FROM sources WHERE job_id IN ({marks})", ids)
        db.execute(f"DELETE FROM jobs WHERE id IN ({marks})", ids)
    output = config.output.resolve()
    for version in ids:
        directory = (config.output / version).resolve()
        if directory.parent == output and directory.is_dir() and not directory.is_symlink():
            shutil.rmtree(directory)
        for stale in config.txt_output.glob(f"*（{version[:12]}）.txt"):
            if stale.is_file() and not stale.is_symlink():
                stale.unlink()
        for name in (version, f"text-{version}", f"speakers-{version}"):
            (config.state / "activity" / f"{name}.json").unlink(missing_ok=True)
        shutil.rmtree(config.state / "agent-context" / version, ignore_errors=True)
    shutil.rmtree(config.state / "workbench-preview" / audio_hash, ignore_errors=True)
    store.forget(audio_hash)
    from .knowledge import Knowledge
    knowledge = Knowledge(config.state)
    for kind in ('revisions', 'members', 'tasks'):
        for item in knowledge.all(kind):
            if item.get('recording') in ids:
                knowledge.delete(kind, item['id'])
    for version in ids:
        for kind in ('scene', 'review'):
            knowledge.delete(kind, version)
    for person in knowledge.all('people'):
        person['voices'] = [v for v in person['voices'] if v['source'].split(':')[0] not in ids]
        knowledge.put('people', person['id'], person)
    trashed, kept, apple = 0, [], 0
    if delete_audio:
        for value in paths:
            path = Path(value)
            origin_path = path.with_suffix(path.suffix + ".origin.json")
            origin = read_json(origin_path)
            if origin.get("kind") == "apple-voice-memos":
                apple += 1
            if not path.is_file() or path.is_symlink():
                continue
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
            if _protected(config, path) or audio_digest(str(path), signature) != audio_hash:
                kept.append(str(path))
                continue
            move_to_trash(path)
            trashed += 1
            if origin_path.is_file():
                origin_path.unlink()
            parent = path.parent
            for managed in (config.inbox / "voice-memos", config.state / "workbench-audio"):
                if parent.parent.resolve() == managed.resolve() and not any(parent.iterdir()):
                    parent.rmdir()
    from .pipeline import refresh_index

    refresh_index(config)
    return {"ok": True, "deleted": len(ids), "trashed": trashed, "kept": kept, "apple": apple}


def _plain_links(text):
    text = re.sub(r"^---\r?\n[\s\S]*?\r?\n---\r?\n", "", text)
    text = re.sub(r"^(<a id=.*|\[音频副本\].*)\n?", "", text, flags=re.M)
    return re.sub(r"\[([^\]]+)\]\((?:transcript[^)]*|[a-z.]+\.md)\)", r"[\1]", text).strip()


def agent_context(config, store, key, parts=("transcript",)):
    item = detail(config, store, key)
    names = store.metadata().get(item["audio_hash"], {}).get("speakers", {})
    speakers = list(dict.fromkeys(s["speaker_label"] for s in item["segments"] if s["speaker"]))
    lines = [
        f"# 录音资料：{item['title']}",
        "",
        f"- 录制时间：{recording_time(origin={'recorded_at': item['recorded_at']})}"
        if item["recorded_at"]
        else f"- 加入时间：{datetime.fromtimestamp(item['created']).strftime('%Y-%m-%d %H:%M')}",
        f"- 时长：{round(item['duration'] or 0)} 秒",
        f"- 来源：{item['source_kind']}",
    ]
    if speakers:
        lines.append(f"- 说话人：{'、'.join(speakers)}")
    lines += [
        "",
        "> 以下内容由本机语音识别和文本模型生成，可能存在识别错误；时间戳为估计值。"
        "说话人编号或名称只在本录音内有效。",
        "",
    ]
    directory, documents = Path(item["output"]), 0
    for part, name, heading in (
        ("analysis", "analysis.md", "分析稿（模型生成）"),
        ("polished", "polished.md", "整理稿（模型润色，原意保留）"),
    ):
        if part in parts and (directory / name).is_file():
            body = rename_speakers(
                _plain_links((directory / name).read_text(encoding="utf-8")), names
            )
            body = re.sub(r"^(#{1,4}) ", r"#\1 ", body, flags=re.M)
            lines += [f"## {heading}", "", body, ""]
            documents += 1
    if "transcript" in parts or not documents:
        from .markdown import timestamp

        lines += ["## 完整转写（带时间戳）", ""]
        for s in item["segments"]:
            label = f"{s['speaker_label']}：" if s["speaker_label"] else ""
            lines.append(f"[{timestamp(s['start']).split('.')[0]}] {label}{s['text'].strip()}")
    return item, "\n".join(lines).rstrip() + "\n"


def find_agent(command):
    extra = [os.path.expanduser(p) for p in EXTRA_PATHS]
    extra += sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin")), reverse=True)
    return shutil.which(command, path=os.pathsep.join([os.environ.get("PATH", ""), *extra]))


def launch_agent(config, store, key, agent, parts):
    """Write the context beside a .command launcher and open it in Terminal (login shell PATH)."""
    if agent not in AGENTS:
        raise ValueError("不支持的智能体")
    if sys.platform != "darwin":
        raise ValueError("此操作仅支持 Mac")
    info = AGENTS[agent]
    item, text = agent_context(config, store, key, parts)
    folder = config.state / "agent-context" / item["id"]
    folder.mkdir(parents=True, exist_ok=True)
    atomic_text(folder / CONTEXT_FILE, text)
    prompt = AGENT_PROMPT.format(file=CONTEXT_FILE, title=item["title"])
    extra = " ".join(f'"{p.replace("~", "$HOME", 1)}"' for p in EXTRA_PATHS)
    script = folder / f"open-{agent}.command"
    atomic_text(
        script,
        f"""#!/bin/zsh
# 听记生成：在录音上下文目录中启动 {info["name"]}
cd {shlex.quote(str(folder))} || exit 1
for extra in {extra} "$HOME"/.nvm/versions/node/*/bin(N); do
  [[ -d "$extra" ]] && PATH="$PATH:$extra"
done
if ! command -v {info["command"]} >/dev/null 2>&1; then
  echo "未找到 {info["command"]} 命令。请先安装 {info["name"]}：{info["install"]}"
  echo "录音上下文已保存在：$PWD/{CONTEXT_FILE}"
  read -k1 "?按任意键关闭"
  exit 1
fi
clear
exec {info["command"]} {shlex.quote(prompt)}
""",
    )
    script.chmod(0o700)
    subprocess.run(["/usr/bin/open", "-a", "Terminal", str(script)], check=True, timeout=15)
    return {
        "ok": True,
        "path": str(folder / CONTEXT_FILE),
        "found": bool(find_agent(info["command"])),
    }
