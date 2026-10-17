#!/bin/bash
set -euo pipefail
TASK_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TASK_ROOT"
if [[ ! -x "$TASK_ROOT/.venv/bin/audio-notes" ]]; then
  echo '尚未安装。请先运行 ./setup.sh' >&2
  exit 2
fi
# Installation/download is explicit. Daily runs never invoke uv/pip or update dependencies.
if [[ $# -eq 0 ]]; then
  set -- run
fi
exec "$TASK_ROOT/.venv/bin/audio-notes" "$@"
