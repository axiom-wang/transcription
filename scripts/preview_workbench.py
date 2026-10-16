"""Isolated browser QA fixture. Contains synthetic material only; no model inference."""

import hashlib
import math
import struct
import wave
from pathlib import Path

from audio_notes.config import load_config
from audio_notes.files import atomic_json, sha256_file
from audio_notes.pipeline import write_exports
from audio_notes.store import Store
from audio_notes.workbench import Server, Workbench

root = Path(__file__).resolve().parents[1] / "verification" / "workbench-ui"
root.mkdir(parents=True, exist_ok=True)
config_path = root / "config.toml"
config_path.write_text('''[paths]
inbox="inbox"
output="output"
state="state"
models="models"
[voice_memos]
enabled=false
[notifications]
enabled=false
[analysis]
enabled=true
''')
config = load_config(config_path)
config.ensure_dirs()
store = Store(config.state)
for index, (title, text) in enumerate([
    ("产品讨论 · 让录音变成可用资料", "今天讨论录音工作台的第一版。先把资料库、处理进度和原文核对做好。预算3000元，这只是提议，尚未决定。下一次讨论之前先完成一个可操作的版本。"),
    ("晨间想法 · 关于专注与记录", "记录的意义，是让未来的自己能够重新理解现在。今天想先把最重要的一件事完成，然后再整理其他想法。"),
    ("学习笔记 · 阅读与理解", "阅读的时候要区分作者的观点、支持观点的依据和自己的推断。读完之后，用自己的语言解释一遍，再回到原文核对。"),
]):
    source = config.inbox / f"{title}.wav"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"".join(struct.pack('<h', int(1500*math.sin(2*math.pi*(220+index*80)*i/16000))) for i in range(16000*12)))
    digest = sha256_file(source)
    key = hashlib.sha256(f"{digest}:ui-fixture".encode()).hexdigest()
    directory = config.output / key
    directory.mkdir(parents=True, exist_ok=True)
    store.register(key, digest, "ui-fixture", source, directory)
    segments = [{"start": i*3., "end":(i+1)*3., "text":part+"。"} for i,part in enumerate(text.strip("。").split("。"))]
    raw = {"schema_version":1, "job_id":key, "audio_sha256":digest, "profile":"ui-fixture",
           "source":{"name":title,"path":str(source)}, "created_at":"2026-10-16T05:00:00Z",
           "settings":{"model":"synthetic-test"}, "info":{"language":"zh","duration":12.},
           "segments":segments, "text":text}
    atomic_json(directory / "raw.json", raw)
    write_exports(raw, directory, config.markdown)
    store.mark(key, "completed")
    if index == 0:
        (directory / "polished.md").write_text("# 产品讨论\n\n## 转写交付稿\n\n[00:00](transcript.simplified.md#segment-1)\n\n"+text+"\n\n## 待核对的转写\n\n预算尚未确定，请核对讨论原文。\n")
        (directory / "polished.txt").write_text(text)
        (directory / "analysis.md").write_text("# 产品讨论分析\n\n## 先看重点（原话摘录）\n\n- 先把资料库、处理进度和原文核对做好。[00:03](transcript.simplified.md#segment-2)\n\n## 进一步分析（模型推断）\n\n建议优先验证用户能否顺利找到并核对文稿。\n")
        atomic_json(directory / "enrichment.complete.json", {"fingerprint":"fixture", "quality_status":"needs_review", "created_at":"2026-10-16T05:00:00Z"})
        atomic_json(directory / "enrichment.status.json", {"status":"completed"})
    elif index == 1:
        atomic_json(directory / "enrichment.status.json", {"status":"failed", "error":"测试情景：文稿生成被中断，原稿已经保存。"})
store.close()
app = Workbench(config)
app.service = lambda: {"registered":False, "checked":True}
app.store.enqueue("transcribe", {"paths":[str(config.inbox / "新的测试录音.wav")]}, "新的测试录音 · 等待处理")
server = Server(("127.0.0.1", 8766), app)
atomic_json(root / "server.json", {"url":server.origin, "token":app.token})
print("QA 工作台已启动于 " + server.origin, flush=True)
server.serve_forever()
