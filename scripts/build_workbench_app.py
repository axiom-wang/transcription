"""Install a signed native macOS workbench app without copying models or personal recordings."""

import argparse
import math
import os
import plistlib
import shutil
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

IDENTIFIER = "local.audio-notes.workbench"


def icon_png(size):
    """Rasterize the app's simple vector mark; no third-party imaging dependencies."""

    def rounded(x, y, left, top, width, height, radius):
        dx = max(left + radius - x, 0, x - (left + width - radius))
        dy = max(top + radius - y, 0, y - (top + height - radius))
        return math.hypot(dx, dy) - radius

    rows = bytearray()
    bars = [(30, 39, 7, 22), (41, 25, 7, 50), (52, 32, 7, 36), (63, 42, 7, 16)]
    for py in range(size):
        rows.append(0)
        for px in range(size):
            x, y = (px + 0.5) * 100 / size, (py + 0.5) * 100 / size
            edge = rounded(x, y, 5, 5, 90, 90, 21)
            alpha = max(0, min(1, 0.5 - edge * size / 100))
            mark = min(rounded(x, y, *bar, 3.5) for bar in bars)
            mix = max(0, min(1, 0.5 - mark * size / 100))
            shade = y / 100
            base = (58 - 13 * shade, 99 - 20 * shade, 78 - 14 * shade)
            color = [round(c * (1 - mix) + light * mix) for c, light in zip(base, (243, 241, 218))]
            rows.extend((*color, round(alpha * 255)))

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


def write_icon(path):
    chunks = []
    for kind, size in (
        (b"icp4", 16),
        (b"icp5", 32),
        (b"icp6", 64),
        (b"ic07", 128),
        (b"ic08", 256),
        (b"ic09", 512),
        (b"ic10", 1024),
    ):
        png = icon_png(size)
        chunks.append(kind + struct.pack(">I", len(png) + 8) + png)
    data = b"".join(chunks)
    path.write_bytes(b"icns" + struct.pack(">I", len(data) + 8) + data)


def build(root, destination=None):
    root = Path(root).resolve()
    if not (root / "run.sh").is_file() or not (root / ".venv/bin/python").exists():
        raise ValueError("请先在此项目完成安装，准备 run.sh 与 .venv")
    default = (
        Path("/Applications")
        if os.access("/Applications", os.W_OK)
        else Path.home() / "Applications"
    )
    destination = Path(destination).expanduser().resolve() if destination else default / "听记.app"
    if destination.suffix != ".app":
        raise ValueError("安装目标必须以 .app 结尾")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        existing = plistlib.loads((destination / "Contents/Info.plist").read_bytes())
        if existing.get("CFBundleIdentifier") != IDENTIFIER:
            raise ValueError("目标位置已有其他应用，未覆盖；请指定其他 --destination")
    with tempfile.TemporaryDirectory(prefix=".tingji-build-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "听记.app"
        executable = staged / "Contents/MacOS/Tingji"
        executable.parent.mkdir(parents=True)
        resources = staged / "Contents/Resources"
        resources.mkdir()
        compiler = Path(
            subprocess.check_output(["/usr/bin/xcrun", "--find", "swiftc"], text=True).strip()
        )
        # CLT's canonical SDK matches its compiler; xcrun can otherwise select a newer beta SDK.
        sdk = compiler.parents[2] / "SDKs/MacOSX.sdk"
        if not sdk.exists():
            sdk = Path(
                subprocess.check_output(["/usr/bin/xcrun", "--show-sdk-path"], text=True).strip()
            )
        subprocess.run(
            [
                str(compiler),
                "-sdk",
                str(sdk),
                "-O",
                "-swift-version",
                "5",
                "-target",
                f"{os.uname().machine}-apple-macosx13.0",
                "-framework",
                "Cocoa",
                "-framework",
                "WebKit",
                str(root / "native/Tingji.swift"),
                "-o",
                str(executable),
            ],
            check=True,
        )
        plist_path = staged / "Contents/Info.plist"
        info = {
            "CFBundleIdentifier": IDENTIFIER,
            "CFBundleName": "听记",
            "CFBundleDisplayName": "听记",
            "CFBundleExecutable": "Tingji",
            "CFBundlePackageType": "APPL",
            "CFBundleInfoDictionaryVersion": "6.0",
            "CFBundleShortVersionString": "2.0.0",
            "CFBundleVersion": "2",
            "CFBundleIconFile": "Workbench.icns",
            "LSMinimumSystemVersion": "13.0",
            "NSHighResolutionCapable": True,
            "NSPrincipalClass": "NSApplication",
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
            "LSApplicationCategoryType": "public.app-category.productivity",
            "NSHumanReadableCopyright": "本地录音工作台 · 所有资料留在本机",
        }
        plist_path.write_bytes(plistlib.dumps(info))
        resources = staged / "Contents/Resources"
        (resources / "Project.plist").write_bytes(plistlib.dumps({"ProjectPath": str(root)}))
        write_icon(resources / "Workbench.icns")
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(staged)], check=True)
        subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(staged)], check=True
        )
        backup = Path(temporary) / "previous.app"
        if destination.exists():
            destination.rename(backup)
        try:
            shutil.move(str(staged), destination)
        except BaseException:
            if backup.exists():
                backup.rename(destination)
            raise
    register = Path(
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    )
    if register.exists():
        subprocess.run([str(register), "-f", str(destination)], check=True)
    print(destination)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="安装听记 macOS 应用入口")
    parser.add_argument("--destination", type=Path, help="默认安装到应用程序目录")
    args = parser.parse_args()
    build(Path(__file__).resolve().parents[1], args.destination)
