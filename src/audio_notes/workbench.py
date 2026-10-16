"""Offline, loopback-only workbench with authenticated APIs and durable operations."""

import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .control import lock_busy, paused, read_json, set_paused
from .files import AlreadyRunning, atomic_json, run_lock
from .workbench_library import (
    artifact,
    audio_path,
    backlog,
    compatible_audio,
    detail,
    historical_artifact,
    job,
    library,
    rows,
)
from .workbench_store import Conflict, WorkbenchStore
from .workbench_tools import AGENTS, agent_context, delete_recording, find_agent, launch_agent

log = logging.getLogger("audio_notes")
STATIC = Path(__file__).with_name("web")
MAX_UPLOAD = 2 * 1024**3


TEXT_ARTIFACTS = (".md", ".txt", ".srt")


def scan_summary(config, since):
    """One line for the queue/toast; empty when this round wrote no report."""
    report = read_json(config.state / "last-scan.json")
    if report.get("finished_at", 0) < since:
        return None
    parts = [
        f"发现 {report.get('copied', 0)} 条新录音",
        f"已转写 {report.get('completed', 0)} 条",
        f"失败 {report.get('failed', 0)} 条",
    ]
    if report.get("waiting"):
        parts.append(f"{report['waiting']} 条录音仍在同步，稍后再扫描")
    return " · ".join(parts)


def safe_filename(value):
    name = re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", value.replace(":", "：")).strip(". ")[:160]
    return name or "录音"


def renamed_text(config, store, key, file):
    """Generated documents read with the user's speaker names; files on disk stay untouched."""
    from .diarization import rename_speakers

    names = store.metadata().get(job(config, key)["audio_hash"], {}).get("speakers", {})
    return rename_speakers(file.read_text(encoding="utf-8"), names)


def execute_request(config, key):
    """Runs outside the web server so closing the UI/server cannot lose a request."""
    store = WorkbenchStore(config.state)
    with run_lock(config.state / "workbench-execution", blocking=True):
        if paused(config) or not store.claim(key):
            return 0
        request = next(row for row in store.requests() if row["id"] == key)
        payload = json.loads(request["payload"])
        os.environ["AUDIO_NOTES_WORKBENCH_TASK"] = "1"
        from .cli import main

        args = ["--config", str(config.file)]
        started = time.time()
        try:
            if request["kind"] == "transcribe":
                args += ["transcribe", "--", *payload["paths"]]
            elif request["kind"] == "enrich":
                args += [
                    "enrich",
                    *(["--force"] if payload.get("force") else []),
                    payload["job_id"],
                ]
            elif request["kind"] == "diarize":
                args += ["diarize", payload["job_id"]]
            elif request["kind"] == "title":
                args += ["enrich", "--title", payload["job_id"]]
            elif request["kind"] == "scan":
                wait = config.processing["settle_seconds"] + 30
                args += ["run", "--wait-limit", str(wait)]
            elif request["kind"] == "notify":
                args += ["notify-test"]
            elif request["kind"] == "service":
                store.finish(key, "cancelled", "已改为手动扫描，无需启用自动接收。")
                return 0
            else:
                raise ValueError("未知操作")
            code = main(args)
            error = None
            if not code and request["kind"] == "diarize":
                marker = read_json(config.output / payload["job_id"] / "speakers.status.json")
                if marker.get("status") != "completed":
                    code, error = 1, marker.get("error") or "说话人区分尚未完成，原稿仍可阅读。"
                elif config.analysis.get("enabled"):
                    code = main(["--config", str(config.file), "enrich", payload["job_id"]])
                    if code:
                        error = "说话人区分已完成，整理与分析稿仍待重试。"
            if code:
                error = "本次操作未完成，请展开运行记录查看原因。已完成的文稿保留。"
            if not code and request["kind"] == "enrich":
                marker = read_json(config.output / payload["job_id"] / "enrichment.status.json")
                if marker.get("status") != "completed":
                    code = 1
                    error = (
                        marker.get("error") or "文本模型尚未就绪或后续任务已暂停，原稿仍可阅读。"
                    )
            if not code and request["kind"] == "transcribe":
                selected = set(payload["paths"])
                for entry in rows(config):
                    if entry["source"] in selected:
                        marker = read_json(Path(entry["output"]) / "enrichment.status.json")
                        if marker.get("status") == "failed":
                            code, error = (
                                1,
                                "原稿已完成，文稿分析失败。请打开录音，仅重试文稿阶段。",
                            )
            summary = scan_summary(config, started) if request["kind"] == "scan" else None
            if code == 3 and request["kind"] == "scan":
                # Files still syncing are expected; the round itself finished.
                store.finish(key, "completed", None, summary)
                return 0
            if code == 3:
                store.finish(key, "failed", "部分文件仍在保存或同步。请等待文件稳定后重试。")
            else:
                store.finish(key, "completed" if code == 0 else "failed", error, summary)
            return code
        except BaseException as exc:
            store.finish(
                key, "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", str(exc)
            )
            raise


class Workbench:
    def __init__(self, config):
        self.config = config
        self.store = WorkbenchStore(config.state)
        self.token = secrets.token_urlsafe(32)
        self.stop = threading.Event()
        self.child = None
        self.legacy_cache = (float("-inf"), {})
        self.legacy_removed = False

    def scheduler(self):
        if not lock_busy(self.config.state / "workbench-execution" / "run.lock"):
            self.store.recover()
        while not self.stop.wait(1):
            try:
                if self.child and self.child.poll() is None:
                    continue
                if any(
                    lock_busy(path)
                    for path in (
                        self.config.state / "run.lock",
                        self.config.state / "text-processing" / "run.lock",
                        self.config.state / "workbench-execution" / "run.lock",
                        self.config.models / ".inference.lock",
                    )
                ):
                    continue
                self.store.recover()
                if paused(self.config):
                    continue
                pending = self.store.pending()
                if not pending:
                    continue
                path = self.config.state / "logs" / f"request-{pending['id']}.log"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("ab") as output:
                    self.child = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "audio_notes.cli",
                            "--config",
                            str(self.config.file),
                            "workbench-task",
                            pending["id"],
                        ],
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                    )
                self.store.set_process(pending["id"], self.child.pid, path)
            except Exception:
                log.exception("工作台队列调度失败，将在下一次检查时重试")

    def legacy_service(self):
        """Retire the old periodic agent whenever it reappears; checked at most every 30 s."""
        if time.monotonic() - self.legacy_cache[0] < 30:
            return self.legacy_cache[1]
        from .service import cleanup_legacy

        try:
            self.legacy_removed = cleanup_legacy(self.config) or self.legacy_removed
        except (OSError, subprocess.SubprocessError, RuntimeError):
            log.exception("旧的自动扫描服务未能清理，将在稍后重试")
        result = {"removed": self.legacy_removed}
        self.legacy_cache = (time.monotonic(), result)
        return result

    def republish(self, audio_hash):
        """Keep 转写TXT and the index in step with workbench names; failures stay non-fatal."""
        from .pipeline import refresh_index
        from .txt_exports import publish_txt

        for entry in rows(self.config):
            directory = Path(entry["output"])
            if entry["audio_hash"] != audio_hash or not (directory / "complete.json").is_file():
                continue
            try:
                publish_txt(
                    self.config, json.loads((directory / "raw.json").read_text()), directory
                )
            except (OSError, ValueError, KeyError):
                log.exception("转写TXT 未能同步更新：%s", entry["id"][:12])
        refresh_index(self.config)

    def snapshot(self):
        items = library(self.config, self.store)
        requests = self.store.requests()
        for request in requests:
            request.pop("payload", None)
            request.pop("log", None)
        return {
            "items": items,
            "backlog": backlog(self.config),
            "requests": requests,
            "paused": paused(self.config),
            "busy": lock_busy(self.config.models / ".inference.lock"),
            "pipeline_busy": lock_busy(self.config.state / "run.lock"),
            "scan": read_json(self.config.state / "voice-memos-status.json"),
            "legacy_service": self.legacy_service(),
            "now": time.time(),
            "settings": {
                "model": self.config.whisper["model"],
                "language": self.config.whisper["language"],
                "analysis_enabled": self.config.analysis.get("enabled", False),
                "speakers_enabled": self.config.speakers.get("enabled", False),
                "voice_memos_enabled": self.config.voice_memos.get("enabled", False),
                "notifications_enabled": self.config.notifications.get("enabled", False),
                "output": str(self.config.output),
                "config": str(self.config.file),
                "extensions": self.config.processing["extensions"],
                "agents": {k: bool(find_agent(v["command"])) for k, v in AGENTS.items()},
            },
        }

    def action(self, data):
        action = data.get("action")
        if action == "pause":
            if not isinstance(data.get("paused"), bool):
                raise ValueError("暂停值无效")
            set_paused(self.config, data["paused"])
            return {"ok": True}
        if action in ("cancel", "prioritize", "retry_request"):
            self.store.request_action(data["id"], "retry" if action == "retry_request" else action)
            return {"ok": True}
        if action in ("scan", "notify"):
            return self.store.enqueue(
                action,
                {},
                {"scan": "扫描语音备忘录并处理新录音", "notify": "测试系统通知"}[action],
            )
        entry = job(self.config, data.get("id", ""))
        if action == "metadata":
            result = self.store.update(entry["audio_hash"], data["changes"])
            if "title" in data["changes"]:
                self.republish(entry["audio_hash"])
            return result
        if action == "rename_speaker":
            names = self.store.rename_speaker(
                entry["audio_hash"], data.get("speaker"), data.get("name", "")
            )
            self.republish(entry["audio_hash"])
            return {"ok": True, "speakers": names}
        if action == "delete":
            if not isinstance(data.get("delete_audio"), bool):
                raise ValueError("请确认是否同时删除录音源文件")
            return delete_recording(self.config, self.store, entry["id"], data["delete_audio"])
        if action == "agent":
            parts = [
                p for p in data.get("parts", []) if p in ("transcript", "polished", "analysis")
            ]
            return launch_agent(self.config, self.store, entry["id"], data.get("agent"), parts)
        if action == "title":
            if entry["status"] != "completed":
                raise ValueError("请先完成音频转写")
            if not self.config.analysis.get("enabled"):
                raise ValueError("尚未启用文本模型，请在 config.toml 中开启 analysis.enabled")
            return self.store.enqueue("title", {"job_id": entry["id"]}, "重新生成录音名称")
        if action == "diarize":
            if entry["status"] != "completed":
                raise ValueError("请先完成音频转写")
            if not self.config.speakers.get("enabled"):
                raise ValueError("尚未启用说话人区分")
            return self.store.enqueue("diarize", {"job_id": entry["id"]}, "补做 / 重试说话人区分")
        if action in ("enrich", "regenerate"):
            if entry["status"] != "completed" or not (Path(entry["output"]) / "raw.json").is_file():
                raise ValueError("请先完成音频转写")
            if not self.config.analysis.get("enabled"):
                raise ValueError("尚未启用文稿分析，请在 config.toml 中开启 analysis.enabled")
            return self.store.enqueue(
                "enrich",
                {"job_id": entry["id"], "force": action == "regenerate"},
                "重新生成文稿" if action == "regenerate" else "补齐 / 重试文稿",
            )
        if action == "retry_asr":
            source = audio_path(self.config, entry["id"])
            return self.store.enqueue(
                "transcribe", {"paths": [str(source)]}, f"重试转写 · {source.stem}"
            )
        if action == "reveal":
            if sys.platform != "darwin":
                raise ValueError("此操作仅支持 Mac")
            subprocess.run(["/usr/bin/open", str(Path(entry["output"]))], check=True, timeout=10)
            return {"ok": True}
        raise ValueError("未知操作")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, Handler)
        self.authority = f"127.0.0.1:{self.server_port}"
        self.origin = f"http://{self.authority}"
        self.cookie_name = f"audio_workbench_{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalAudioWorkbench/1.0"

    def log_message(self, format, *args):
        # URLs can include private filenames. Routine polling need not be logged.
        pass

    def guard(self, authenticated=True):
        if self.headers.get("Host") != self.server.authority:
            raise PermissionError("仅允许通过本机工作台地址访问")
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            raise PermissionError("不允许其他网站访问本机资料")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise PermissionError("不允许跨站访问")
        if authenticated:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie.get(self.server.cookie_name)
            bearer = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if not (
                (value and secrets.compare_digest(value.value, self.server.app.token))
                or secrets.compare_digest(bearer, self.server.app.token)
            ):
                raise PermissionError("请通过「打开转写工作台.command」重新打开工作台")

    def send(self, body, code=200, mime="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.common_headers()
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def common_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)

    def dispatch(self, post):
        try:
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            params = parse_qs(parsed.query)
            app, config = self.server.app, self.server.app.config
            static = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
            self.guard(authenticated=path not in static and path != "/api/session")
            if post:
                if path == "/api/session":
                    if not secrets.compare_digest(
                        self.headers.get("Authorization", ""), "Bearer " + app.token
                    ):
                        raise PermissionError("工作台访问凭据已失效，请重新打开")
                    self.send(
                        {"ok": True},
                        headers={
                            "Set-Cookie": f"{self.server.cookie_name}={app.token}; HttpOnly; SameSite=Strict; Path=/"
                        },
                    )
                    return
                if path == "/api/upload":
                    self.upload()
                    return
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("请使用 JSON 请求")
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 2_000_000:
                    raise ValueError("请求大小无效")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("请求格式无效")
                if path == "/api/action":
                    self.send(app.action(data))
                    return
                raise FileNotFoundError("接口不存在")
            if path in static:
                file = STATIC / static[path]
                mime = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}[
                    file.suffix
                ]
                self.send(file.read_bytes(), mime=mime + "; charset=utf-8")
            elif path == "/api/library":
                self.send(app.snapshot())
            elif path == "/api/detail":
                self.send(detail(config, app.store, params.get("id", [""])[0]))
            elif path == "/api/document":
                file = (
                    historical_artifact(
                        config, params["id"][0], params["name"][0], params["version"][0]
                    )
                    if params.get("version")
                    else artifact(config, params["id"][0], params["name"][0])
                )
                self.send(
                    {
                        "text": renamed_text(config, app.store, params["id"][0], file),
                        "name": file.name,
                    }
                )
            elif path == "/api/audio":
                file = (
                    compatible_audio(config, params["id"][0])
                    if params.get("compatible")
                    else audio_path(config, params["id"][0])
                )
                self.stream(file, audio=True)
            elif path == "/api/download":
                file = (
                    historical_artifact(
                        config, params["id"][0], params["name"][0], params["version"][0]
                    )
                    if params.get("version")
                    else artifact(config, params["id"][0], params["name"][0])
                )
                title = detail(config, app.store, params["id"][0])["title"]
                filename = f"{safe_filename(title)}-{file.name}"
                if file.suffix in TEXT_ARTIFACTS:
                    self.send(
                        renamed_text(config, app.store, params["id"][0], file),
                        mime=(mimetypes.guess_type(file.name)[0] or "text/plain")
                        + "; charset=utf-8",
                        headers={
                            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)
                        },
                    )
                else:
                    self.stream(file, filename=filename)
            elif path == "/api/agent-context":
                parts = params.get("part", ["transcript"])
                item, text = agent_context(config, app.store, params["id"][0], parts)
                if params.get("download"):
                    self.send(
                        text,
                        mime="text/markdown; charset=utf-8",
                        headers={
                            "Content-Disposition": "attachment; filename*=UTF-8''"
                            + quote(f"{safe_filename(item['title'])}-录音上下文.md")
                        },
                    )
                else:
                    self.send({"text": text, "title": item["title"]})
            elif path == "/api/history":
                directory = Path(job(config, params["id"][0])["output"]) / "history"
                versions = []
                if directory.is_dir():
                    for folder in sorted(directory.iterdir(), reverse=True):
                        if folder.is_symlink() or not re.fullmatch(r"[0-9]{16,24}", folder.name):
                            continue
                        receipt = read_json(folder / "enrichment.complete.json")
                        versions.append(
                            {
                                "id": folder.name,
                                "created_at": receipt.get("created_at"),
                                "files": [
                                    n
                                    for n in ("polished.md", "polished.txt", "analysis.md")
                                    if (folder / n).is_file()
                                ],
                            }
                        )
                self.send({"versions": versions})
            elif path == "/api/export":
                keys = params.get("id", [])
                name = params.get("name", ["transcript.simplified.txt"])[0]
                if not keys or len(keys) > 100:
                    raise ValueError("每次请选择 1 至 100 条录音")
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                    for key in dict.fromkeys(keys):
                        file = artifact(config, key, name)
                        title = detail(config, app.store, key)["title"]
                        archive.writestr(
                            f"{safe_filename(title)}（{key[:8]}）/{name}",
                            renamed_text(config, app.store, key, file)
                            if file.suffix in TEXT_ARTIFACTS
                            else file.read_bytes(),
                        )
                self.send(
                    buffer.getvalue(),
                    mime="application/zip",
                    headers={"Content-Disposition": "attachment; filename=recordings.zip"},
                )
            elif path == "/api/log":
                key = params.get("request", [None])[0]
                if key:
                    if not re.fullmatch(r"[a-f0-9]{32}", key):
                        raise ValueError("请求标识无效")
                    file = config.state / "logs" / f"request-{key}.log"
                else:
                    key = params["id"][0]
                    job(config, key)
                    file = (
                        config.state / "logs" / f"{'text-' if params.get('text') else ''}{key}.log"
                    )
                if not file.is_file():
                    self.send({"text": "暂时没有运行记录。"})
                else:
                    with file.open("rb") as handle:
                        handle.seek(max(0, file.stat().st_size - 16000))
                        self.send({"text": handle.read().decode("utf-8", errors="replace")})
            else:
                raise FileNotFoundError("页面不存在")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            code = (
                409
                if isinstance(exc, Conflict)
                else 403
                if isinstance(exc, PermissionError)
                else 404
                if isinstance(exc, FileNotFoundError)
                else 400
                if isinstance(exc, (ValueError, KeyError, TypeError))
                else 500
            )
            if code == 500:
                log.exception("工作台请求失败")
            self.send(
                {"error": str(exc) if code != 500 else "操作未完成，请稍后重试或检查工作台日志。"},
                code,
            )

    def stream(self, path, filename=None, audio=False):
        size = path.stat().st_size
        start, end, code = 0, size - 1, 200
        requested = self.headers.get("Range") if audio else None
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            if not match or not any(match.groups()) or size == 0:
                self.send({}, 416, headers={"Content-Range": f"bytes */{size}"})
                return
            left, right = match.groups()
            if left:
                start, end = int(left), min(int(right) if right else size - 1, size - 1)
            else:
                start = max(0, size - int(right))
            if start > end or start >= size:
                self.send({}, 416, headers={"Content-Range": f"bytes */{size}"})
                return
            code = 206
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if audio and path.suffix.lower() in (".qta", ".m4a"):
            mime = "audio/mp4"
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        self.common_headers()
        if audio:
            self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if filename:
            self.send_header(
                "Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename)
            )
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def upload(self):
        config, app = self.server.app.config, self.server.app
        name = safe_filename(unquote(self.headers.get("X-Filename", "")))
        if Path(name).suffix.lower().lstrip(".") not in config.processing["extensions"]:
            raise ValueError("不支持此音频格式")
        length = int(self.headers.get("Content-Length", 0))
        if not 0 < length <= MAX_UPLOAD:
            raise ValueError("请选择非空录音，单个文件最多 2 GB")
        staging = config.state / "workbench-audio" / ".upload"
        staging.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=staging)
        temporary = Path(temp)
        try:
            digest = hashlib.sha256()
            with os.fdopen(fd, "wb") as out:
                remaining = length
                self.connection.settimeout(120)
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("文件接收中断，请重新导入")
                    digest.update(chunk)
                    out.write(chunk)
                    remaining -= len(chunk)
                out.flush()
                os.fsync(out.fileno())
            audio_hash = digest.hexdigest()
            existing = next(
                (
                    r
                    for r in rows(config)
                    if r["audio_hash"] == audio_hash and r["status"] == "completed"
                ),
                None,
            )
            target = config.state / "workbench-audio" / audio_hash / name
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, target)
            if existing:
                with sqlite3.connect(config.state / "jobs.sqlite3") as db:
                    db.execute(
                        "INSERT OR REPLACE INTO sources VALUES (?,?,?)",
                        (existing["id"], str(target), time.time()),
                    )
                self.send({"reused": True, "id": existing["id"], "name": name})
                return
            request = app.store.enqueue("transcribe", {"paths": [str(target)]}, name)
            self.send({"reused": False, "request": request["id"], "name": name}, 201)
        finally:
            temporary.unlink(missing_ok=True)


def serve(config, port=8765):
    try:
        with run_lock(config.state / "workbench-server"):
            app = Workbench(config)
            app.legacy_service()
            server = Server(("127.0.0.1", port), app)
            record = config.state / "workbench-server.json"
            atomic_json(record, {"url": server.origin, "token": app.token, "pid": os.getpid()})
            record.chmod(0o600)
            threading.Thread(target=app.scheduler, daemon=True).start()
            print(f"工作台已启动：{server.origin}", flush=True)
            try:
                server.serve_forever(poll_interval=0.5)
            finally:
                app.stop.set()
                server.server_close()
                record.unlink(missing_ok=True)
    except AlreadyRunning:
        raise ValueError("工作台已启动，请使用 ./run.sh workbench --open") from None


def ensure_workbench(config, port=8765):
    def existing():
        record = read_json(config.state / "workbench-server.json")
        if not record or not re.fullmatch(r"http://127\.0\.0\.1:\d+", record.get("url", "")):
            return None
        try:
            connection = HTTPConnection(
                "127.0.0.1", int(record["url"].rsplit(":", 1)[1]), timeout=5
            )
            try:
                connection.request(
                    "GET", "/api/library", headers={"Authorization": "Bearer " + record["token"]}
                )
                response = connection.getresponse()
                response.read()
                if response.status == 200:
                    return record
            finally:
                connection.close()
        except (OSError, ValueError, KeyError):
            pass
        return None

    record = existing()
    if not record:
        log_path = config.state / "logs" / "workbench.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as output:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "audio_notes.cli",
                    "--config",
                    str(config.file),
                    "workbench",
                    "--port",
                    str(port),
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            )
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            record = existing()
            if record:
                break
            if process.poll() is not None:
                raise RuntimeError(f"工作台启动失败，请查看 {log_path}")
            time.sleep(0.25)
        if not record:
            raise RuntimeError(f"工作台启动超时，请查看 {log_path}")
    return record


def open_workbench(config, port=8765):
    record = ensure_workbench(config, port)
    webbrowser.open(record["url"] + "/#token=" + record["token"])
    print("已打开本地工作台；关闭浏览器不影响后台任务。")
