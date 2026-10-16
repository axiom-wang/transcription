#!/bin/bash
TASK_ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
"$TASK_ROOT/run.sh" run
TASK_EXIT=$?
echo
if [[ $TASK_EXIT -eq 3 ]]; then
  echo '有文件仍在等待同步；稍后再次运行，或在听记中点击「立即扫描」。'
elif [[ $TASK_EXIT -ne 0 ]]; then
  echo "本轮退出码：$TASK_EXIT；详见上方错误和 state/logs。"
fi
read -r -p '按回车关闭窗口…' _
exit "$TASK_EXIT"
