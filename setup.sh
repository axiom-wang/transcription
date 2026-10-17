#!/bin/bash
set -euo pipefail
TASK_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$TASK_ROOT"
if command -v uv >/dev/null 2>&1; then
  uv sync --frozen
else
  echo '需要 uv（推荐 brew install uv），以及 Python 3.11–3.13。' >&2
  exit 2
fi
./developer/run.sh download-model
./developer/run.sh doctor
if .venv/bin/python -c 'from pathlib import Path; from audio_notes.config import load_config; raise SystemExit(0 if load_config(Path("config.toml")).analysis["enabled"] else 1)'; then
  ./setup-text.sh
fi
.venv/bin/python scripts/build_manual_app.py
.venv/bin/python scripts/build_workbench_app.py
if .venv/bin/python -c 'from pathlib import Path; from audio_notes.config import load_config; raise SystemExit(0 if load_config(Path("config.toml")).speakers["enabled"] else 1)'; then
  ./setup-speakers.sh
fi
echo '安装完成。运行 ./developer/run.sh 自动接收语音备忘录；打开听记后在设置中选择自动处理时机。'
