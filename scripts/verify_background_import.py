"""Verify launchd-driven import and real Whisper using synthetic audio only."""

import json
import shutil
import sqlite3
import time
from pathlib import Path

from audio_notes.config import load_config
from audio_notes.files import sha256_file
from audio_notes.service import manage

ROOT = Path(__file__).resolve().parents[1]


def main():
    directory = ROOT / "verification" / f"background-import-{time.time_ns()}"
    source_root = directory / "apple" / "Recordings"
    source_root.mkdir(parents=True)
    source = source_root / "background-test.wav"
    shutil.copyfile(ROOT / "verification" / "inbox" / "中文与English测试.wav", source)
    original_hash = sha256_file(source)
    original_mtime = source.stat().st_mtime_ns
    with sqlite3.connect(source_root / "CloudRecordings.db") as db:
        db.execute(
            "CREATE TABLE ZCLOUDRECORDING (ZPATH TEXT,ZCUSTOMLABEL TEXT,ZDATE REAL,ZDURATION REAL,ZUNIQUEID TEXT)"
        )
        db.execute(
            "INSERT INTO ZCLOUDRECORDING VALUES (?,?,?,?,?)",
            (source.name, "后台自动接收验证", 0, 12.48, "background-test"),
        )
    config_text = (ROOT / "config.toml").read_text()
    start = config_text.index("directories = [")
    end = config_text.index("]", start) + 1
    config_text = config_text[:start] + 'directories = ["apple/Recordings"]' + config_text[end:]
    config_text = config_text.replace('models = "models"', 'models = "../../models"')
    config_text = config_text.replace("settle_seconds = 30", "settle_seconds = 0")
    config_path = directory / "config.toml"
    config_path.write_text(config_text)
    config = load_config(config_path)
    config.ensure_dirs()
    (config.state / "logs").mkdir(exist_ok=True)
    installed = False
    try:
        manage(config, "install")
        installed = True
        deadline = time.monotonic() + 45
        raw_path = None
        while time.monotonic() < deadline:
            for complete in config.output.glob("*/complete.json"):
                raw_path = complete.parent / "raw.json"
            if raw_path:
                break
            time.sleep(0.25)
        if not raw_path:
            raise AssertionError("后台转写未在测试期限内完成；检查测试目录 state/logs")
        raw = json.loads(raw_path.read_text())
        assert raw["import_origin"]["recording_title"] == "后台自动接收验证"
        assert raw["import_origin"]["original_path"] == str(source)
        assert raw["segments"] and raw["segments"][0]["words"]
        assert "Markdown" in raw["text"]
        assert sha256_file(source) == original_hash
        assert source.stat().st_mtime_ns == original_mtime
        assert (raw_path.parent / "notes.md").exists()
        assert (config.output / "index.md").exists()
        report = {
            "launchd_background_import_and_real_whisper": True,
            "original_audio_unchanged": True,
            "recording_metadata_preserved": True,
            "output": str(raw_path.parent),
        }
        (ROOT / "verification/background-import-report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        if installed:
            manage(config, "uninstall")


if __name__ == "__main__":
    main()
