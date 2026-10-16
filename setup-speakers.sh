#!/bin/bash
set -euo pipefail
TASK_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$TASK_ROOT"
uv venv --python .venv/bin/python --allow-existing .venv-speakers
uv pip sync --python .venv-speakers/bin/python requirements-speakers.lock
./run.sh download-speaker-model "$@"
printf '%s\n' '本地说话人模型已准备好。speakers.enabled = true 时自动处理后续转写。'
