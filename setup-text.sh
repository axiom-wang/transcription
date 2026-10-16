#!/bin/bash
set -euo pipefail
TASK_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$TASK_ROOT"
uv venv --python .venv/bin/python --allow-existing .venv-text
uv pip sync --python .venv-text/bin/python requirements-text.lock
./run.sh download-text-model
printf '%s\n' '本地文本模型已安装。配置 analysis.enabled = true 后自动生成交付稿与分析稿。'
