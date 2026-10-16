"""Test local daemon and CLI dispatch in isolation, without inference or a browser."""

import json
import os
import subprocess
import sys
import time
from http.client import HTTPConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def request(record, path, body=None):
    port = int(record["url"].rsplit(":", 1)[1])
    connection = HTTPConnection("127.0.0.1", port, timeout=6)
    headers = {"Authorization": "Bearer " + record["token"]}
    if body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(body)
    try:
        connection.request("POST" if body else "GET", path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}: {data[:200]!r}")
        return json.loads(data)
    finally:
        connection.close()


def ready(record_path, process):
    deadline = time.monotonic() + 20
    last_error = "尚未生成启动记录"
    while time.monotonic() < deadline:
        if record_path.exists():
            record = json.loads(record_path.read_text())
            try:
                return record, request(record, "/api/library")
            except Exception as exc:
                last_error = str(exc)
        if process.poll() is not None:
            raise RuntimeError("服务提前退出")
        time.sleep(0.2)
    raise RuntimeError("本地服务未能就绪：" + last_error)


def main():
    smoke = ROOT / "verification" / ("workbench-smoke-" + str(time.time_ns()))
    smoke.mkdir(parents=True)
    config = smoke / "config.toml"
    config.write_text(
        '[paths]\ninbox="inbox"\noutput="output"\nstate="state"\nmodels="models"\n[voice_memos]\nenabled=false\n[notifications]\nenabled=false\n[analysis]\nenabled=false\n'
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    with (smoke / "server.log").open("ab") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "audio_notes.cli",
                "--config",
                str(config),
                "workbench",
                "--port",
                "0",
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        record, initial = ready(smoke / "state/workbench-server.json", process)
        assert initial["items"] == []
        print("服务启动与本机 API 连通通过", flush=True)
        queued = request(record, "/api/action", {"action": "scan"})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state = request(record, "/api/library")
            item = next(row for row in state["requests"] if row["id"] == queued["id"])
            if item["status"] in ("failed", "completed", "interrupted"):
                break
            time.sleep(0.2)
        assert item["status"] == "failed", item
        assert request(record, "/api/log?request=" + queued["id"])["text"]
        request(record, "/api/action", {"action": "pause", "paused": True})
        waiting = request(record, "/api/action", {"action": "notify"})
        time.sleep(1.1)
        state = request(record, "/api/library")
        assert state["paused"]
        assert (
            next(row for row in state["requests"] if row["id"] == waiting["id"])["status"]
            == "queued"
        )
        request(record, "/api/action", {"action": "cancel", "id": waiting["id"]})
        state = request(record, "/api/library")
        assert (
            next(row for row in state["requests"] if row["id"] == waiting["id"])["status"]
            == "cancelled"
        )
        report = {
            "server_start": True,
            "subprocess_dispatch": True,
            "failure_and_log_visible": True,
            "pause": True,
            "cancel": True,
            "synthetic_data_only": True,
            "browser_visual_check": "permission_denied",
        }
        (smoke / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print("真实进程派发、失败回传、日志、暂停与取消全部通过", flush=True)
        print("报告：" + str(smoke / "report.json"), flush=True)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


if __name__ == "__main__":
    main()
