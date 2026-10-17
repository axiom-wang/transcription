"""Legacy launchd agent cleanup. Scanning now runs only when requested in the workbench."""

import hashlib
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("audio_notes")
MANUAL_HELP = "自动处理已并入听记：打开听记，在设置中选择处理时机，也可点击「立即扫描」。"


def identity(config):
    suffix = hashlib.sha256(str(config.file).encode()).hexdigest()[:12]
    return f"local.audio-notes.voice-memos.{suffix}"


def agent_path(config):
    return Path.home() / "Library/LaunchAgents" / f"{identity(config)}.plist"


def launchctl(*args, required=True):
    result = subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=15)
    if required and result.returncode:
        raise RuntimeError(f"launchctl {' '.join(args)} 失败：{result.stderr.strip()}")
    return result


def registered(config):
    if sys.platform != "darwin":
        return False
    try:
        target = f"gui/{os.getuid()}/{identity(config)}"
        return launchctl("print", target, required=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def cleanup_legacy(config):
    """Remove the old periodic agent; idempotent. Returns True when something was removed.

    The plist goes first: when the old agent itself runs this, bootout ends the process.
    """
    path = agent_path(config)
    found = path.is_file()
    path.unlink(missing_ok=True)
    if registered(config):
        found = True
        log.info("已停用旧的自动扫描服务：%s", identity(config))
        launchctl("bootout", f"gui/{os.getuid()}/{identity(config)}", required=False)
    elif found:
        log.info("已删除旧的自动扫描服务配置：%s", path)
    return found


def manage(config, action):
    if action == "status":
        print(f"服务配置：{agent_path(config)}\nPython：{Path(sys.executable).resolve()}")
        if registered(config) or agent_path(config).is_file():
            print("检测到旧的自动扫描服务；运行 ./developer/run.sh service uninstall 可清理。")
        else:
            print("没有旧版 launchd 自动扫描服务；听记的自动处理由应用内调度。")
        print(MANUAL_HELP)
        return
    if action in ("stop", "uninstall"):
        removed = cleanup_legacy(config)
        print(
            ("旧的自动扫描服务已停用并卸载" if removed else "没有需要清理的后台服务")
            + "；录音和文档保留"
        )
        return
    raise ValueError(MANUAL_HELP)
