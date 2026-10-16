"""Best-effort macOS notifications; audio and transcript text stay out of banners."""

import logging
import subprocess
import sys

log = logging.getLogger("audio_notes")

# Pass user-controlled text as arguments, never as AppleScript source.
SCRIPT = """on run argv
    display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"
end run
"""


def notify(config, title, message):
    if not config.notifications["enabled"] or sys.platform != "darwin":
        return False
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-", title, message],
            input=SCRIPT,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("系统通知发送失败，转写继续：%s", exc)
        return False
    return True
