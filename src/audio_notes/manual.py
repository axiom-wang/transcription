"""Selected-file transcription for the Finder droplet and command line."""

import logging
import signal
import subprocess
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .files import AlreadyRunning, run_lock
from .model import check_model
from .notifications import notify
from .pipeline import Pipeline, set_lock_fd

log = logging.getLogger("audio_notes")


def selected_files(config, paths):
    extensions = {"." + ext.lower().lstrip(".") for ext in config.processing["extensions"]}
    sources = []
    for value in paths:
        source = Path(value).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError(f"请选择录音文件，不能选择文件夹：{source.name}")
        if source.suffix.lower() not in extensions:
            raise ValueError(f"不支持此文件格式：{source.name}")
        if source.stat().st_size == 0:
            raise ValueError(f"录音为空，请先下载完整文件：{source.name}")
        if source not in sources:
            sources.append(source)
    if not sources:
        raise ValueError("请至少选择一个录音文件")
    return sources


@contextmanager
def wait_for_turn(config):
    # Share the same lock and cache as automatic runs, without interrupting them.
    with ExitStack() as stack:
        announced = False
        while True:
            try:
                handle = stack.enter_context(run_lock(config.state))
                break
            except AlreadyRunning:
                if not announced:
                    log.info("手动转写已排队，等待当前转写结束；无需重新提交")
                    notify(
                        config, "手动转写已排队", "正在处理其他录音，结束后会自动处理你选择的文件。"
                    )
                    announced = True
                time.sleep(1)
        set_lock_fd(handle.fileno())
        try:
            yield
        finally:
            set_lock_fd(None)


def transcribe(config, paths, reveal=False):
    sources = selected_files(config, paths)
    check_model(config.model_dir)
    profile = config.profile()
    previous = signal.getsignal(signal.SIGTERM)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        with wait_for_turn(config):
            pipeline = Pipeline(config, profile)
            try:
                failed = False
                while True:
                    counts = pipeline.scan(sources=sources)
                    failed = failed or bool(counts.failed)
                    if not counts.waiting:
                        break
                    log.info("等待所选录音保存或下载完成后继续")
                    time.sleep(config.processing["poll_seconds"])
                if failed:
                    raise RuntimeError("部分所选录音转写失败，请查看 state/logs/workflow.log。")
                directories = []
                for source in sources:
                    row = pipeline.store.db.execute(
                        """SELECT jobs.output FROM jobs JOIN sources ON jobs.id=sources.job_id
                        WHERE sources.path=? AND jobs.profile=? AND jobs.status='completed'
                        ORDER BY jobs.updated DESC LIMIT 1""",
                        (str(source), profile),
                    ).fetchone()
                    if row and Path(row["output"]) not in directories:
                        directories.append(Path(row["output"]))
                for directory in directories:
                    name = (
                        "polished.md"
                        if (directory / "enrichment.complete.json").is_file()
                        else "notes.md"
                    )
                    print(f"转写结果：{directory / name}")
                if reveal and directories:
                    name = (
                        "polished.md"
                        if (directories[0] / "enrichment.complete.json").is_file()
                        else "notes.md"
                    )
                    target = (
                        directories[0] / name
                        if len(directories) == 1
                        else (config.output / "index.md")
                    )
                    try:
                        subprocess.run(["/usr/bin/open", "-R", str(target)], check=True, timeout=10)
                    except (OSError, subprocess.SubprocessError) as exc:
                        log.warning("转写已保存，但未能自动打开结果：%s", exc)
                return directories
            finally:
                pipeline.close()
    finally:
        signal.signal(signal.SIGTERM, previous)


def show_error(message):
    # Fixed script with argument data: quotes in paths/errors cannot become code.
    script = """on run argv
        display alert "手动转写未完成" message (item 1 of argv) as warning
    end run
    """
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-", str(message)],
            input=script,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        log.warning("无法显示手动转写错误提示：%s", message)
