#!/bin/bash
# 兼容入口：旧 launchd 监控已停用，自动处理由听记应用管理。
TASK_ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
echo '自动处理已并入听记；请在听记设置中选择处理时机。'
"$TASK_ROOT/developer/run.sh" service uninstall
read -r -p '按回车关闭窗口…' _
