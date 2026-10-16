import argparse
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from contextlib import closing
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import load_config
from .files import AlreadyRunning, atomic_json, run_lock
from .model import check_model, download
from .pipeline import Pipeline, set_lock_fd
from .voice_memos import VoiceMemosImporter, inspect_sources


def setup_logging(state):
    logger = logging.getLogger("audio_notes")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    if not os.environ.get("AUDIO_NOTES_BACKGROUND"):
        logger.addHandler(console)
    (state / "logs").mkdir(parents=True, exist_ok=True)
    file = RotatingFileHandler(
        state / "logs" / "workflow.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file.setFormatter(formatter)
    logger.addHandler(file)


def status(config):
    import_report = config.state / "voice-memos-status.json"
    if import_report.is_file():
        report = json.loads(import_report.read_text(encoding="utf-8"))
        print(
            f"语音备忘录：发现 {report['found']}，最近复制 {report['copied']}，读取失败 {report['failed']}"
        )
        for error in report["errors"]:
            print(f"  {error['path']}：{error['error']}")
    path = config.state / "jobs.sqlite3"
    if not path.exists():
        print("尚无转写任务。运行 ./run.sh 自动导入已同步的语音备忘录")
        return
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
        for state, count in rows:
            print(f"{state}: {count}")
        for source, state, attempts, error in db.execute(
            "SELECT source,status,attempts,error FROM jobs WHERE status!='completed' ORDER BY updated"
        ):
            print(f"\n{source}\n  状态={state} 尝试={attempts}\n  {error or ''}")
    print(f"\n文档：{config.output}\n日志：{config.state / 'logs'}")
    if config.analysis.get("enabled"):
        from collections import Counter

        stages = Counter()
        for marker in config.output.glob("*/complete.json"):
            try:
                entry = json.loads((marker.parent / "enrichment.status.json").read_text())
                stages[entry["status"]] += 1
            except (OSError, ValueError, KeyError):
                stages["pending"] += 1
        print("文稿优化与分析：" + "，".join(f"{k}={v}" for k, v in stages.items()))


def doctor(config):
    from importlib.metadata import version

    import av
    import ctranslate2

    print(f"Python: {sys.version.split()[0]}")
    check_model(config.model_dir)
    if config.whisper["model"] == "qwen3-asr-1.7b":
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        from .qwen import doctor as qwen_doctor

        qwen_doctor(config.model_dir)
        print("本地环境检查通过；run/watch 强制离线，不上传音频。")
        return
    print(f"faster-whisper: {version('faster-whisper')}")
    print(f"PyAV: {av.__version__}")
    print(f"CPU 可用精度: {sorted(ctranslate2.get_supported_compute_types('cpu'))}")
    precision = config.whisper["compute_type"]
    if precision != "auto" and precision not in ctranslate2.get_supported_compute_types("cpu"):
        raise ValueError(f"此 Mac 不支持 {precision}；请改为 float32 或 auto")
    from faster_whisper import WhisperModel

    model = WhisperModel(
        str(config.model_dir),
        device="cpu",
        compute_type=precision,
        local_files_only=True,
        cpu_threads=config.whisper["cpu_threads"],
    )
    print(f"模型加载成功：{config.model_dir}（多语言={model.model.is_multilingual}）")
    for name in ("inbox", "output", "state", "models"):
        print(f"{name}: {getattr(config, name)}")
    print("本地环境检查通过；run/watch 强制离线，不上传音频。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="本地语音识别 → 原始转写和 Markdown 阅读文档")
    parser.add_argument("--config", type=Path, default=Path("config.toml"), help="配置文件路径")
    commands = parser.add_subparsers(dest="command", required=True)
    run_command = commands.add_parser("run", help="批量处理；等待同步稳定；重试之前失败的任务")
    run_command.add_argument(
        "--no-wait", action="store_true", help="不等待新文件稳定，存在待同步文件时退出 3"
    )
    run_command.add_argument(
        "--wait-limit",
        type=float,
        metavar="SECONDS",
        help="最多等待同步中的文件这么多秒，超时后结束本轮并退出 3",
    )
    manual_command = commands.add_parser("transcribe", help="仅转写所选文件，不扫描语音备忘录")
    manual_command.add_argument("files", nargs="+", type=Path, help="一个或多个录音文件")
    manual_command.add_argument("--reveal", action="store_true", help="完成后在 Finder 显示结果")
    manual_command.add_argument("--gui", action="store_true", help=argparse.SUPPRESS)
    commands.add_parser("watch", help="持续扫描指定目录，Ctrl+C 停止")
    commands.add_parser("status", help="显示完成、失败和中断任务")
    commands.add_parser("doctor", help="检查依赖、模型完整性与本地加载")
    commands.add_parser("notify-test", help="发送一条 macOS 测试通知，不读取或转写录音")
    discover_command = commands.add_parser("discover", help="只读列出已同步的语音备忘录和访问错误")
    discover_command.add_argument("--report", type=Path, help="保存发现结果 JSON")
    service_command = commands.add_parser("service", help="查看或清理旧的后台自动扫描服务")
    service_command.add_argument(
        "action", choices=["install", "start", "stop", "status", "uninstall"]
    )
    commands.add_parser("import", help="自动复制所有新旧语音备忘录，不调用 Whisper")
    command = commands.add_parser("download-model", help="首次联网下载模型权重")
    command.add_argument("--refresh", action="store_true", help="重新向模型仓库检查并修复本地模型")
    text_download = commands.add_parser("download-text-model", help="下载本地文稿优化与分析模型")
    text_download.add_argument("--refresh", action="store_true", help="修复本地文本模型")
    commands.add_parser("check-text-model", help="校验文本模型全部文件")
    speaker_download = commands.add_parser("download-speaker-model", help="下载本地说话人区分模型")
    speaker_download.add_argument("--refresh", action="store_true", help="修复说话人模型")
    speaker_download.add_argument(
        "--authorize", action="store_true", help="临时授权下载公开受限模型，不保存账号令牌"
    )
    commands.add_parser("check-speaker-model", help="校验说话人模型全部文件")
    speaker_command = commands.add_parser(
        "diarize", help="为已有转写补做说话人区分，不重新识别文字"
    )
    speaker_command.add_argument("jobs", nargs="*", help="任务标识；省略时处理全部已完成录音")
    speaker_command.add_argument("--force", action="store_true", help="保留旧结果并重新区分")
    enrich_command = commands.add_parser("enrich", help="优化与分析已完成转写，不重新识别音频")
    enrich_command.add_argument("jobs", nargs="*", help="任务指纹；省略时处理全部已完成录音")
    enrich_command.add_argument("--force", action="store_true", help="保留旧缓存并重新生成")
    enrich_command.add_argument(
        "--title", action="store_true", help="只重新生成录音名称（一句话简介 + 时间）"
    )
    workbench_command = commands.add_parser("workbench", help="本地录音工作台")
    workbench_mode = workbench_command.add_mutually_exclusive_group()
    workbench_mode.add_argument("--open", action="store_true", help="后台启动并在浏览器打开工作台")
    workbench_mode.add_argument("--url", action="store_true", help="后台启动并返回本机应用连接信息")
    workbench_command.add_argument("--port", type=int, default=8765)
    task_command = commands.add_parser("workbench-task", help=argparse.SUPPRESS)
    task_command.add_argument("request_id")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        config.ensure_dirs()
        setup_logging(config.state)
        if os.environ.get("AUDIO_NOTES_BACKGROUND"):
            # Only the retired launchd agent sets this; remove it instead of scanning.
            from .service import cleanup_legacy

            cleanup_legacy(config)
            return 0
        if args.command == "workbench":
            from .workbench import ensure_workbench, open_workbench, serve

            if not 0 <= args.port <= 65535:
                raise ValueError("端口必须在 0 至 65535 之间")
            if args.url:
                print(json.dumps(ensure_workbench(config, args.port)))
            else:
                (open_workbench if args.open else serve)(config, args.port)
            return 0
        if args.command == "workbench-task":
            from .workbench import execute_request

            return execute_request(config, args.request_id)
        if args.command == "watch":
            # Release the run lock between scans so queued/manual work can proceed.
            while True:
                previous_watch = os.environ.get("AUDIO_NOTES_WATCH")
                os.environ["AUDIO_NOTES_WATCH"] = "1"
                try:
                    main(["--config", str(config.file), "run", "--no-wait"])
                finally:
                    if previous_watch is None:
                        os.environ.pop("AUDIO_NOTES_WATCH", None)
                    else:
                        os.environ["AUDIO_NOTES_WATCH"] = previous_watch
                time.sleep(config.processing["poll_seconds"])
        if args.command == "transcribe":
            from .manual import transcribe

            transcribe(config, args.files, reveal=args.reveal)
            return 0
        if args.command == "notify-test":
            from .notifications import notify

            if not notify(config, "音频转写通知已开启", "开始转写、完成和失败时会在这里提醒你。"):
                print("通知未发送：请检查 notifications.enabled、系统平台和项目日志。")
                return 1
            print("已请求系统发送测试通知；若未显示，请检查系统通知设置及专注模式。")
            return 0
        if args.command == "status":
            status(config)
            return 0
        if args.command == "doctor":
            doctor(config)
            return 0
        if args.command == "service":
            from .service import manage

            manage(config, args.action)
            return 0
        if args.command == "discover":
            files, roots, errors = inspect_sources(config)
            for root in roots:
                print(f"{root['path']}：{root['recordings']} 条录音")
            for source in files:
                print(source)
            for error in errors:
                print(f"读取失败：{error['path']}：{error['error']}", file=sys.stderr)
            if not files and not errors:
                print("尚未发现本地录音；请确认语音备忘录启用了 iCloud 并已同步到 Mac。")
            if args.report:
                atomic_json(args.report, {"roots": roots, "errors": errors, "found": len(files)})
            return 2 if errors else 0
        if args.command in ("download-speaker-model", "check-speaker-model", "diarize"):
            from .diarization import check_speaker_model, diarize_completed, download_speaker_model

            if args.command == "download-speaker-model":
                download_speaker_model(config, args.refresh, args.authorize)
                return 0
            check_speaker_model(config, verify=True)
            if args.command == "check-speaker-model":
                print("说话人模型文件校验通过；推理完全在本机运行。")
                return 0
            if not config.speakers.get("enabled"):
                raise ValueError("请先设置 speakers.enabled = true")
            from .pipeline import write_index
            from .store import Store

            with run_lock(config.state) as handle:
                set_lock_fd(handle.fileno())
                with closing(Store(config.state)) as store:
                    jobs = store.completed()
                    if args.jobs:
                        wanted = set(args.jobs)
                        if wanted - {job["id"] for job in jobs}:
                            raise ValueError("任务不存在或尚未完成转写")
                        jobs = [job for job in jobs if job["id"] in wanted]
                    count = diarize_completed(config, jobs, force=args.force, retry_now=True)
                    write_index(config, store.completed())
                print(f"本次完成 {count} 份录音的说话人区分。")
            from .control import read_json

            return int(
                any(
                    read_json(Path(j["output"]) / "speakers.status.json").get("status")
                    in ("failed", "blocked")
                    for j in jobs
                )
            )
        if args.command in ("download-text-model", "check-text-model", "enrich"):
            from .enrichment import check_text_model, download_text_model, enrich_completed

            if args.command == "download-text-model":
                download_text_model(config, args.refresh)
                return 0
            check_text_model(config, verify=True)
            if args.command == "check-text-model":
                print("文本模型文件校验通过；推理使用本地独立环境。")
                return 0
            if not config.analysis["enabled"]:
                raise ValueError("请先设置 analysis.enabled = true")
            from .pipeline import write_index
            from .store import Store

            with closing(Store(config.state)) as store:
                jobs = store.completed()
                if args.jobs:
                    requested = set(args.jobs)
                    if requested - {job["id"] for job in jobs}:
                        raise ValueError("任务不存在或尚未完成转写")
                    jobs = [job for job in jobs if job["id"] in requested]
                if args.title:
                    from .enrichment import name_completed

                    named = name_completed(config, jobs)
                    print(f"本次生成 {named} 个录音名称。")
                    return 0 if named == len(jobs) else 1
                count = enrich_completed(config, jobs, force=args.force, retry_now=True)
                write_index(config, store.completed())
                print(f"本次生成 {count} 份转写交付稿和分析稿。")
                failed = False
                for job in jobs:
                    marker = Path(job["output"]) / "enrichment.status.json"
                    if (
                        marker.is_file()
                        and json.loads(marker.read_text()).get("status") == "failed"
                    ):
                        failed = True
                return 1 if failed else 0
        with run_lock(config.state) as handle:
            set_lock_fd(handle.fileno())
            if args.command == "download-model":
                download(config, args.refresh)
                return 0
            with closing(VoiceMemosImporter(config)) as importer:
                if args.command == "import":
                    imported = importer.scan()
                    return 1 if imported.failed else (3 if imported.waiting else 0)
                check_model(config.model_dir)
                pipeline = Pipeline(config, config.profile())
                previous = signal.getsignal(signal.SIGTERM)

                def interrupted(signum, frame):
                    raise KeyboardInterrupt

                signal.signal(signal.SIGTERM, interrupted)
                try:
                    if args.command == "run":
                        failed = False
                        watching = bool(os.environ.get("AUDIO_NOTES_WATCH"))
                        deadline = (
                            None
                            if args.wait_limit is None
                            else time.monotonic() + max(args.wait_limit, 0)
                        )
                        summary = {"started_at": time.time(), "copied": 0, "completed": 0}
                        while True:
                            imported = importer.scan()
                            # A manual round finishes follow-up stages for every new recording;
                            # the optional watch loop keeps the per-pass limits.
                            counts = pipeline.scan(watch=watching, followup_limit=watching)
                            failed = failed or bool(counts.failed or imported.failed)
                            waiting = counts.waiting + imported.waiting
                            summary["copied"] += imported.copied
                            summary["completed"] += counts.completed
                            summary.update(
                                found=imported.found,
                                failed=counts.failed + imported.failed,
                                waiting=waiting,
                                finished_at=time.time(),
                            )
                            atomic_json(config.state / "last-scan.json", summary)
                            expired = deadline is not None and time.monotonic() >= deadline
                            if not waiting or args.no_wait or expired:
                                return 1 if failed else (3 if waiting and not watching else 0)
                            logging.getLogger("audio_notes").info(
                                "等待同步中的文件稳定后继续；Ctrl+C 可停止"
                            )
                            time.sleep(config.processing["poll_seconds"])
                    logging.getLogger("audio_notes").info("监控中：%s；Ctrl+C 停止", config.inbox)
                    while True:
                        importer.scan()
                        pipeline.scan(watch=True)
                        time.sleep(config.processing["poll_seconds"])
                finally:
                    signal.signal(signal.SIGTERM, previous)
                    pipeline.close()
                    set_lock_fd(None)
    except AlreadyRunning as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已停止。已完成结果保留；重新运行会继续未完成任务。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        if args.command == "transcribe" and args.gui:
            from .manual import show_error

            show_error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
