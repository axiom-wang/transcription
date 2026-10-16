import json
import os
from pathlib import Path

from .files import atomic_json, sha256_file

REQUIRED = ("model.bin", "config.json", "tokenizer.json")
QWEN_MODEL = "qwen3-asr-1.7b"
QWEN_REPOS = (
    ("Qwen/Qwen3-ASR-1.7B", "7278e1e70fe206f11671096ffdd38061171dd6e5", ""),
    ("Qwen/Qwen3-ForcedAligner-0.6B", "c7cbfc2048c462b0d63a45797104fc9db3ad62b7", "aligner"),
)
QWEN_FILES = (
    "config.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
    "model.safetensors.index.json",
    "model-00001-of-00002.safetensors",
    "model-00002-of-00002.safetensors",
    "aligner/config.json",
    "aligner/tokenizer_config.json",
    "aligner/vocab.json",
    "aligner/merges.txt",
    "aligner/model.safetensors",
)


def check_model(directory: Path, verify=True):
    if directory.name == QWEN_MODEL:
        return check_qwen_model(directory, verify)
    for name in (*REQUIRED, "local-model.json"):
        if not (directory / name).is_file():
            raise ValueError(f"缺少模型文件 {directory / name}；运行 ./run.sh download-model")
    manifest = json.loads((directory / "local-model.json").read_text(encoding="utf-8"))
    if not all(name in manifest.get("files", {}) for name in REQUIRED):
        raise ValueError("模型清单无效；运行 download-model --refresh 修复")
    if verify:
        for name, expected in manifest["files"].items():
            if name not in (
                *REQUIRED,
                "preprocessor_config.json",
                "vocabulary.json",
                "vocabulary.txt",
            ):
                raise ValueError("模型清单包含未知文件")
            if sha256_file(directory / name) != expected:
                raise ValueError(f"模型校验失败：{name}；运行 download-model --refresh 修复")
    return manifest


def check_qwen_model(directory, verify=True):
    for name in (*QWEN_FILES, "local-model.json"):
        if not (directory / name).is_file():
            raise ValueError(f"缺少模型文件 {directory / name}；运行 ./run.sh download-model")
    manifest = json.loads((directory / "local-model.json").read_text(encoding="utf-8"))
    if (
        manifest.get("model") != QWEN_MODEL
        or manifest.get("engine") != "mlx-qwen3-asr"
        or set(manifest.get("files", {})) != set(QWEN_FILES)
    ):
        raise ValueError("Qwen 模型清单无效；运行 download-model --refresh 修复")
    if verify:
        for name, expected in manifest["files"].items():
            if sha256_file(directory / name) != expected:
                raise ValueError(f"模型校验失败：{name}；运行 download-model --refresh 修复")
    return manifest


def download(config, refresh=False):
    if not refresh:
        try:
            check_model(config.model_dir)
            print(f"本地模型完整，无需下载：{config.model_dir}")
            return
        except (ValueError, OSError, json.JSONDecodeError):
            pass
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    from huggingface_hub import snapshot_download

    if config.whisper["model"] == QWEN_MODEL:
        print("下载 Qwen3-ASR-1.7B 完整精度权重与时间戳对齐模型（约 6.5 GB）。", flush=True)
        for repo, revision, subdir in QWEN_REPOS:
            snapshot_download(
                repo,
                revision=revision,
                local_dir=str(config.model_dir / subdir),
                force_download=refresh,
                max_workers=2,
                allow_patterns=[
                    name.removeprefix("aligner/")
                    for name in QWEN_FILES
                    if name.startswith("aligner/") == bool(subdir)
                ],
            )
        hashes = {name: sha256_file(config.model_dir / name) for name in QWEN_FILES}
        import hashlib

        identity = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        atomic_json(
            config.model_dir / "local-model.json",
            {
                "model": QWEN_MODEL,
                "engine": "mlx-qwen3-asr",
                "precision": "float16",
                "repositories": [{"repo": r, "revision": v} for r, v, _ in QWEN_REPOS],
                "model_sha256": identity,
                "files": hashes,
            },
        )
        check_qwen_model(config.model_dir)
        print(f"模型就绪：{config.model_dir}", flush=True)
        return
    print(f"首次准备模型 {config.whisper['model']}；仅下载权重，不上传音频。", flush=True)
    name = config.whisper["model"]
    repo = (
        "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
        if name == "turbo"
        else f"Systran/faster-whisper-{name}"
    )
    snapshot_download(
        repo,
        local_dir=str(config.model_dir),
        force_download=refresh,
        allow_patterns=[
            "config.json",
            "preprocessor_config.json",
            "model.bin",
            "tokenizer.json",
            "vocabulary.*",
        ],
    )
    hashes = {
        name: sha256_file(config.model_dir / name)
        for name in (*REQUIRED, "preprocessor_config.json", "vocabulary.json", "vocabulary.txt")
        if (config.model_dir / name).is_file()
    }
    if not all(name in hashes for name in REQUIRED):
        raise ValueError("下载不完整，请重试 download-model")
    atomic_json(
        config.model_dir / "local-model.json",
        {"model": config.whisper["model"], "model_sha256": hashes["model.bin"], "files": hashes},
    )
    print(f"模型就绪：{config.model_dir}")
