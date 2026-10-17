"""Install the local Tingji Native Messaging connector (macOS, safe to rerun)."""

import argparse
import base64
import hashlib
import json
import shlex
import sys
from pathlib import Path

HOST = "local.audio_notes.tingji"
BROWSERS = (
    "Google/Chrome",
    "Google/Chrome Beta",
    "Google/Chrome Canary",
    "Google/ChromeForTesting",
    "Chromium",
    "Microsoft Edge",
    "Microsoft Edge Beta",
    "Microsoft Edge Canary",
    "BraveSoftware/Brave-Browser",
    "Arc/User Data",
)


def extension_id(manifest):
    public_key = base64.b64decode(manifest["key"], validate=True)
    digest = hashlib.sha256(public_key).hexdigest()[:32]
    return "".join(chr(ord("a") + int(char, 16)) for char in digest)


def install(root, home=None):
    root = Path(root).resolve()
    home = Path(home) if home is not None else Path.home()
    if not (root / ".venv/bin/python").exists():
        raise ValueError("请先完成听记安装，准备项目的 .venv/bin/python")
    manifest = json.loads((root / "browser-extension/manifest.json").read_text())
    identifier = extension_id(manifest)
    wrapper = root / "developer/native-host.sh"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(
        "#!/bin/sh\nset -eu\n"
        f"ROOT={shlex.quote(str(root))}\n"
        'export PYTHONPATH="$ROOT/src"\n'
        'exec "$ROOT/.venv/bin/python" -m audio_notes.cli --config "$ROOT/config.toml" native-host\n'
    )
    wrapper.chmod(0o755)
    host = {
        "name": HOST,
        "description": "听记 · 标签页录音本机连接器",
        "path": str(wrapper),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{identifier}/"],
    }
    support = home / "Library/Application Support"
    written = []
    for browser in BROWSERS:
        directory = support / browser
        if browser != "Google/Chrome" and not directory.is_dir():
            continue
        destination = directory / "NativeMessagingHosts" / f"{HOST}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(host, ensure_ascii=False, indent=2) + "\n")
        written.append(destination)
    return identifier, written


def main():
    parser = argparse.ArgumentParser(description="安装听记浏览器连接器（可重复运行）")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("目前的连接器安装脚本仅支持 macOS")
    try:
        identifier, written = install(args.root)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"安装失败：{exc}\n")
    print(f"听记连接器已安装；拓展 ID：{identifier}")
    for path in written:
        print(f"  {path}")
    print("在 chrome://extensions 开启「开发者模式」，点击「加载已解压的扩展程序」并选择：")
    print(args.root.resolve() / "browser-extension")
    print("打开拓展面板，确认连接器已安装后即可使用。以后移动听记目录请重新运行此脚本。")


if __name__ == "__main__":
    main()
