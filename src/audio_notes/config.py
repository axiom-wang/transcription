import hashlib
import json
import math
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    file: Path
    inbox: Path
    output: Path
    state: Path
    models: Path
    whisper: dict
    processing: dict
    markdown: dict
    voice_memos: dict = field(default_factory=dict)
    notifications: dict = field(default_factory=lambda: {"enabled": True})
    analysis: dict = field(default_factory=lambda: {"enabled": False})
    speakers: dict = field(default_factory=lambda: {"enabled": False})

    @property
    def speaker_model_dir(self):
        return self.models / "speaker-diarization-community-1"

    @property
    def speaker_python(self):
        return Path(__file__).resolve().parents[2] / ".venv-speakers/bin/python"

    @property
    def text_model_dir(self):
        return self.models / "qwen3.5-9b-4bit"

    @property
    def text_python(self):
        return Path(__file__).resolve().parents[2] / ".venv-text/bin/python"

    @property
    def model_dir(self):
        return self.models / self.whisper["model"]

    @property
    def txt_output(self):
        return self.output.parent / "转写TXT"

    def ensure_dirs(self):
        for path in (self.inbox, self.output, self.state, self.models, self.txt_output):
            path.mkdir(parents=True, exist_ok=True)

    def profile(self):
        manifest = self.model_dir / "local-model.json"
        if not manifest.is_file():
            raise ValueError("模型未准备好。请先运行 ./developer/run.sh download-model")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if not data.get("model_sha256"):
            raise ValueError("模型清单不完整，请重新运行 download-model")
        from importlib.metadata import version

        payload = {
            "schema": 1,
            "whisper": self.whisper,
            "model": data,
            "faster_whisper": version("faster-whisper"),
            "ctranslate2": version("ctranslate2"),
            "av": version("av"),
        }
        if self.whisper["model"] == "qwen3-asr-1.7b":
            payload.update(
                {"asr_adapter": 1, "mlx": version("mlx"), "mlx_qwen3_asr": version("mlx-qwen3-asr")}
            )
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def load_config(path: Path) -> Config:
    path = path.expanduser().resolve()
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    base = path.parent
    paths = {
        k: (base / Path(data["paths"][k]).expanduser()).resolve()
        for k in ("inbox", "output", "state", "models")
    }
    values = [*paths.values(), (paths["output"].parent / "转写TXT").resolve()]
    for i, left in enumerate(values):
        for right in values[i + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                raise ValueError(f"配置目录不能重叠或互相包含：{left} / {right}")
    whisper = {
        "model": "qwen3-asr-1.7b",
        "language": "zh",
        "compute_type": "float16",
        "cpu_threads": 4,
        "beam_size": 5,
        "word_timestamps": True,
        "vad_filter": True,
        "condition_on_previous_text": False,
        "initial_prompt": "",
    }
    processing = {
        "recursive": True,
        "extensions": ["m4a", "qta", "mp3", "wav", "mov", "mp4"],
        "settle_seconds": 30,
        "poll_seconds": 10,
        "retry_seconds": 300,
        "timeout_seconds": 0,
    }
    markdown = {"paragraph_gap_seconds": 2.0, "paragraph_max_chars": 400, "section_seconds": 300}
    notifications = {"enabled": True}
    analysis = {
        "enabled": False,
        "chunk_chars": 4000,
        "max_input_tokens": 16000,
        "max_output_tokens": 6000,
        "timeout_seconds": 3600,
        "retry_seconds": 600,
        "max_jobs_per_scan": 1,
    }
    speakers = {
        "enabled": False,
        "device": "auto",
        "num_speakers": 0,
        "cpu_threads": 4,
        "timeout_seconds": 7200,
        "retry_seconds": 600,
        "max_jobs_per_scan": 1,
    }
    voice_memos = {
        "enabled": False,
        "directories": [
            "~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings",
            "~/Library/Application Support/com.apple.voicememos/Recordings",
            "~/Library/Containers/com.apple.VoiceMemos/Data/Library/Application Support/Recordings",
        ],
    }
    for name, target in (
        ("whisper", whisper),
        ("processing", processing),
        ("markdown", markdown),
        ("voice_memos", voice_memos),
        ("notifications", notifications),
        ("analysis", analysis),
        ("speakers", speakers),
    ):
        unknown = set(data.get(name, {})) - set(target)
        if unknown:
            raise ValueError(f"未知配置项 {name}: {sorted(unknown)}")
        target.update(data.get(name, {}))
    if "compute_type" not in data.get("whisper", {}) and whisper["model"] != "qwen3-asr-1.7b":
        whisper["compute_type"] = "int8"
    if whisper["model"] not in (
        "qwen3-asr-1.7b",
        "tiny",
        "base",
        "small",
        "medium",
        "large-v3",
        "turbo",
    ):
        raise ValueError("请选择多语言模型：qwen3-asr-1.7b/tiny/base/small/medium/large-v3/turbo")
    if whisper["model"] == "qwen3-asr-1.7b":
        if whisper["compute_type"] != "float16":
            raise ValueError("Qwen3-ASR 完整精度模型的 compute_type 必须为 float16")
    elif whisper["compute_type"] not in ("int8", "float32", "auto"):
        raise ValueError("Mac CPU compute_type 请选择 int8、float32 或 auto")
    if whisper["language"] not in ("zh", "en", "auto"):
        raise ValueError("language 请选择 zh、en 或 auto")
    if not isinstance(whisper["initial_prompt"], str):
        raise ValueError("initial_prompt 必须是字符串")
    for group, keys in (
        (whisper, ("word_timestamps", "vad_filter", "condition_on_previous_text")),
        (processing, ("recursive",)),
        (notifications, ("enabled",)),
    ):
        for key in keys:
            if type(group[key]) is not bool:
                raise ValueError(f"{key} 必须是 true 或 false")
    for group, keys in (
        (processing, ("settle_seconds", "retry_seconds", "timeout_seconds", "poll_seconds")),
        (markdown, ("paragraph_gap_seconds", "paragraph_max_chars", "section_seconds")),
    ):
        for key in keys:
            if type(group[key]) not in (int, float) or not math.isfinite(group[key]):
                raise ValueError(f"{key} 必须是有限数值")
    if type(markdown["paragraph_max_chars"]) is not int:
        raise ValueError("paragraph_max_chars 必须是正整数")
    for k in ("cpu_threads", "beam_size"):
        if type(whisper[k]) is not int or whisper[k] < 1:
            raise ValueError(f"whisper.{k} 必须是正整数")
    for k in ("settle_seconds", "retry_seconds", "timeout_seconds"):
        if not isinstance(processing[k], (float, int)) or processing[k] < 0:
            raise ValueError(f"processing.{k} 必须 >= 0")
    if processing["poll_seconds"] < 1:
        raise ValueError("poll_seconds 必须 >= 1")
    if markdown["paragraph_max_chars"] < 1 or markdown["section_seconds"] < 1:
        raise ValueError("Markdown 长度与章节秒数必须 > 0")
    if markdown["paragraph_gap_seconds"] < 0:
        raise ValueError("paragraph_gap_seconds 必须 >= 0")
    if not isinstance(processing["extensions"], list) or not all(
        isinstance(x, str) and x for x in processing["extensions"]
    ):
        raise ValueError("extensions 必须是扩展名字符串列表")
    if type(voice_memos["enabled"]) is not bool:
        raise ValueError("voice_memos.enabled 必须是 true 或 false")
    if type(analysis["enabled"]) is not bool:
        raise ValueError("analysis.enabled 必须是 true 或 false")
    if type(speakers["enabled"]) is not bool:
        raise ValueError("speakers.enabled 必须是 true 或 false")
    if speakers["device"] not in ("auto", "cpu", "mps"):
        raise ValueError("speakers.device 请选择 auto、cpu 或 mps")
    for key in set(speakers) - {"enabled", "device"}:
        if type(speakers[key]) is not int or speakers[key] < (0 if key == "num_speakers" else 1):
            raise ValueError(f"speakers.{key} 数值无效")
    if speakers["num_speakers"] > 50:
        raise ValueError("speakers.num_speakers 必须介于 0 和 50；0 自动判断人数")
    for key in set(analysis) - {"enabled"}:
        if type(analysis[key]) is not int or analysis[key] < 1:
            raise ValueError(f"analysis.{key} 必须是正整数")
    if not 1000 <= analysis["chunk_chars"] <= 8000:
        raise ValueError("analysis.chunk_chars 必须介于 1000 和 8000")
    if not isinstance(voice_memos["directories"], list) or not all(
        isinstance(value, str) and value for value in voice_memos["directories"]
    ):
        raise ValueError("voice_memos.directories 必须是路径字符串列表")
    # Do not resolve/probe protected paths during configuration loading.
    voice_memos["directories"] = [
        str(base / Path(value).expanduser()) for value in voice_memos["directories"]
    ]
    if voice_memos["enabled"]:
        for value in voice_memos["directories"]:
            source = Path(os.path.abspath(value))
            for destination in values:
                if (
                    source == destination
                    or source in destination.parents
                    or destination in source.parents
                ):
                    raise ValueError(
                        f"语音备忘录源目录不能与项目数据目录重叠：{source} / {destination}"
                    )
    return Config(
        path,
        **paths,
        whisper=whisper,
        processing=processing,
        markdown=markdown,
        voice_memos=voice_memos,
        notifications=notifications,
        analysis=analysis,
        speakers=speakers,
    )
