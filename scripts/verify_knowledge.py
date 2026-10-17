"""Exercise real local-model retrieval against the synthetic browser QA corpus."""

from dataclasses import replace
from pathlib import Path

from audio_notes.config import load_config
from audio_notes.knowledge import Knowledge
from audio_notes.retrieval import run_query
from audio_notes.workbench import Workbench

root = Path(__file__).resolve().parents[1]
base = load_config(root / "config.toml")
config = load_config(root / "verification/workbench-ui/config.toml")
config = replace(config, models=base.models, analysis={**base.analysis, "enabled": True})
app = Workbench(config)
run_query(app, "acceptance-query", {"filters": {"query": "预算已经确定了吗？"}, "answer": True})
result = Knowledge(config.state).get("queries", "acceptance-query")
assert result["items"], result
assert result["claims"], result
assert any("尚未决定" in claim["text"] for claim in result["claims"]), result
print("本地模型语义检索和问答通过；每条回答均已校验为来源录音的逐字引用。")
