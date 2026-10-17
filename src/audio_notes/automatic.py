"""Model-free directory watching and conservative power/idle scheduling."""

import re
import subprocess
import sys
import time
from pathlib import Path

from .knowledge import Knowledge


def power_and_idle():
    if sys.platform != "darwin":
        return False, False
    try:
        power = subprocess.run(
            ["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True, timeout=3
        )
        idle = subprocess.run(
            ["/usr/sbin/ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=3
        )
        match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', idle.stdout)
        return power.returncode == 0 and "AC Power" in power.stdout, bool(
            match and int(match[1]) >= 120 * 10**9
        )
    except (OSError, subprocess.SubprocessError):
        return False, False


def allowed(timing, ac, idle):
    return timing == "instant" or (ac and idle if timing == "saving" else ac or idle)


class Watcher:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.last_check = 0
        self.signature = None
        self.dirty = False
        self.reason = ""

    def tick(self):
        if time.monotonic() - self.last_check < 10:
            return
        self.last_check = time.monotonic()
        prefs = Knowledge(self.config.state).preferences()
        if not prefs["automatic"] or not self.config.voice_memos.get("enabled"):
            self.reason = "自动处理已关闭"
            return
        signature = []
        from .voice_memos import discover

        errors = []
        for directory in self.config.voice_memos.get("directories", []):
            root = Path(directory).expanduser()
            for path in discover(
                root,
                {"." + ext.lower().lstrip(".") for ext in self.config.processing["extensions"]},
                errors,
            ):
                try:
                    stat = path.stat()
                    signature.append((str(path), stat.st_size, stat.st_mtime_ns))
                except OSError:
                    continue
        if errors:
            self.reason = "语音备忘录目录需要访问权限，请立即扫描查看详情"
            for error in errors:
                signature.append((error["path"], 0, 0))
        signature.sort()
        if signature != self.signature:
            self.signature, self.dirty = signature, True
        # A settling file or remaining follow-up work is retried without loading any model here.
        from .control import read_json

        report = read_json(self.config.state / "last-scan.json")
        if report.get("waiting") and time.time() - report.get("finished_at", 0) < 600:
            self.dirty = True
        active_scans = [
            r
            for r in self.store.requests()
            if r["kind"] == "scan" and r["status"] in ("queued", "running")
        ]
        if any(r["status"] == "running" for r in active_scans):
            self.reason = "正在扫描；新增变化将在本轮结束后继续处理" if self.dirty else "正在扫描"
            return
        if not self.dirty and not active_scans:
            self.reason = "监听新录音"
            return
        ac, idle = power_and_idle()
        if not allowed(prefs["timing"], ac, idle):
            self.reason = (
                "等待接通电源且空闲" if prefs["timing"] == "saving" else "等待接通电源或电脑空闲"
            )
        else:
            self.reason = "已发现变化，等待处理"
        if self.dirty:
            self.store.enqueue("scan", {"automatic": True}, "自动扫描新录音")
            self.dirty = False
