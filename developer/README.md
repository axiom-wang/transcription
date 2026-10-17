# 命令行用法（高级）

日常使用请打开「听记」。此目录保留兼容脚本和旧手动转写入口。

从项目根目录运行：

```bash
./developer/run.sh workbench --open
./developer/run.sh run
./developer/run.sh transcribe -- "/路径/录音.m4a"
./developer/run.sh status
```

`持续监控.command`、`启用自动转写.command` 仅负责清理旧 launchd 服务并提示使用听记设置。不要同时启用旧周期服务；新自动处理由听记的轻量监听器调度。
