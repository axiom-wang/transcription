"""One request per Chrome Native Messaging process; stdout contains only a JSON frame."""

import json
import logging
import os
import struct
import subprocess
import sys
from contextlib import redirect_stdout
from http.client import HTTPConnection
from importlib.metadata import PackageNotFoundError, version
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote, urlsplit

from .config import load_config

MAX_MESSAGE = 1024 * 1024
log = logging.getLogger("audio_notes")


def read_exact(stream, length):
    chunks = bytearray()
    while len(chunks) < length:
        chunk = stream.read(length - len(chunks))
        if not chunk:
            raise ValueError("听记连接消息不完整，请重试")
        chunks.extend(chunk)
    return bytes(chunks)


def read_message(stream):
    length = struct.unpack("<I", read_exact(stream, 4))[0]
    if not 0 < length <= MAX_MESSAGE:
        raise ValueError("听记连接消息长度无效")
    try:
        message = json.loads(read_exact(stream, length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("听记连接消息必须是 UTF-8 JSON") from None
    if not isinstance(message, dict):
        raise ValueError("听记连接消息必须是对象")
    return message


def write_message(stream, message):
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_MESSAGE:
        raise ValueError("听记连接回复过长")
    stream.write(struct.pack("<I", len(payload)) + payload)
    stream.flush()


def import_file(config, value):
    from .manual import selected_files
    from .workbench import MAX_UPLOAD, ensure_workbench

    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("请选择已下载完成的录音文件")
    if not Path(value).is_absolute():
        raise ValueError("录音路径必须是绝对路径")
    try:
        source = selected_files(config, [value])[0]
    except FileNotFoundError:
        raise ValueError("找不到录音文件，可能已被移动或删除，请重新保存") from None
    # Keep the opened file for the entire upload; send a bounded stream, never read it all.
    with source.open("rb") as audio:
        size = os.fstat(audio.fileno()).st_size
        if not 0 < size <= MAX_UPLOAD:
            raise ValueError("请选择非空录音，单个文件最多 2 GB")
        record = ensure_workbench(config)
        url = urlsplit(record["url"])
        if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port:
            raise ValueError("听记本机连接地址无效，请重新启动听记")
        connection = HTTPConnection("127.0.0.1", url.port, timeout=120, blocksize=1024 * 1024)
        try:
            connection.request(
                "POST",
                "/api/upload",
                body=audio,
                headers={
                    "Authorization": "Bearer " + record["token"],
                    "X-Filename": quote(source.name, safe="~()*!.'-"),
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(size),
                },
            )
            response = connection.getresponse()
            try:
                result = json.loads(response.read(MAX_MESSAGE))
            except (ValueError, UnicodeDecodeError):
                raise ValueError("听记返回了无效回复，请重新启动听记后重试") from None
            if not isinstance(result, dict):
                raise ValueError("听记返回了无效回复，请重新启动听记后重试")
            if response.status not in (200, 201):
                raise ValueError(result.get("error") or "听记未能接收录音，请重试")
            return {**result, "ok": True}
        finally:
            connection.close()


def handle_message(config, message):
    kind = message.get("type")
    if kind == "ping":
        from .workbench import API_VERSION

        try:
            installed = version("local-audio-notes")
        except PackageNotFoundError:
            installed = "1.0.0"
        return {"ok": True, "version": installed, "api_version": API_VERSION}
    if kind == "import":
        return import_file(config, message.get("path"))
    if kind == "open":
        from .workbench import open_workbench

        for app in (Path("/Applications/听记.app"), Path.home() / "Applications/听记.app"):
            if app.is_dir():
                subprocess.run(
                    ["/usr/bin/open", str(app)],
                    check=True,
                    timeout=15,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                break
        else:
            open_workbench(config)
        return {"ok": True}
    raise ValueError("不支持的听记连接操作")


def run(config=None, *, config_path=None, stdin=None, stdout=None):
    incoming = stdin if stdin is not None else sys.stdin.buffer
    outgoing = stdout if stdout is not None else sys.stdout.buffer
    try:
        message = read_message(incoming)
        config = config or load_config(config_path)
        directory = config.state / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            directory / "native-host.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        previous_handlers, previous_level = log.handlers[:], log.level
        log.handlers = [handler]
        log.setLevel(logging.INFO)
        try:
            # open_workbench and future dependencies may print status text.
            with (directory / "native-host.log").open("a", encoding="utf-8") as output:
                with redirect_stdout(output):
                    result = handle_message(config, message)
        except Exception:
            log.exception("听记连接操作失败")
            raise
        finally:
            log.handlers = previous_handlers
            log.setLevel(previous_level)
            handler.close()
    except (ValueError, RuntimeError) as exc:
        result = {"ok": False, "error": str(exc)}
    except PermissionError:
        result = {"ok": False, "error": "无法读取录音文件，请检查文件和目录权限"}
    except (OSError, TimeoutError):
        result = {"ok": False, "error": "无法连接听记或读取录音，请检查本机连接和文件后重试"}
    except Exception:
        result = {"ok": False, "error": "听记连接失败，请查看 state/logs/native-host.log"}
    write_message(outgoing, result)
    return 0
