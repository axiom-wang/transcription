#!/bin/bash
# 已停用：听记不再在后台自动扫描语音备忘录。
TASK_ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
echo '听记已改为手动扫描：打开「听记」，点击「立即扫描」即可导入并转写新录音。'
"$TASK_ROOT/run.sh" service uninstall
read -r -p '按回车关闭窗口…' _
