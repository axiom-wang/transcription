"""Build the local Finder file-picker and drag-and-drop entry point."""

import plistlib
import subprocess
from pathlib import Path


def build(root):
    root = Path(root)
    app = root / "developer/手动转写.app"
    subprocess.run([
        "/usr/bin/osacompile", "-o", str(app),
        str(root / "scripts/manual_transcription.applescript"),
    ], check=True)
    plist_path = app / "Contents/Info.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    plist.update({
        "CFBundleIdentifier": "local.audio-notes.manual",
        "CFBundleName": "手动转写",
        "CFBundleDisplayName": "手动转写",
        "CFBundleShortVersionString": "1.0",
        "CFBundleDocumentTypes": [{
            "CFBundleTypeName": "录音文件",
            "CFBundleTypeRole": "Viewer",
            "LSItemContentTypes": ["public.audio", "public.movie", "local.audio-notes.qta"],
            "LSHandlerRank": "Alternate",
        }],
        "UTImportedTypeDeclarations": [{
            "UTTypeIdentifier": "local.audio-notes.qta",
            "UTTypeDescription": "QuickTime 音频",
            "UTTypeConformsTo": ["public.audio", "com.apple.quicktime-movie"],
            "UTTypeTagSpecification": {"public.filename-extension": ["qta"]},
        }],
        "NSHighResolutionCapable": True,
    })
    plist_path.write_bytes(plistlib.dumps(plist))
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)], check=True)
    print(app)
    return app


if __name__ == "__main__":
    build(Path(__file__).resolve().parents[1])
