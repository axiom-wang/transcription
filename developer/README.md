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

## 浏览器拓展与 Native Messaging

运行 `.venv/bin/python scripts/install_browser_extension.py` 安装本机连接器，再从 Chrome 的 `chrome://extensions` 加载仓库内 `browser-extension/`。原始 `~/浏览器拓展/tab-audio-recorder` 不参与安装，也未修改。脚本幂等，生成的 `developer/native-host.sh` 包含本机绝对路径，不纳入版本控制；移动仓库后重新安装。

宿主名为 `local.audio_notes.tingji`，清单默认写入 `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/`。已有的 Chrome Beta/Canary、Chrome for Testing、Chromium、Edge/Beta/Canary、Brave、Arc 配置目录也会写入。`allowed_origins` 仅包含 `browser-extension/manifest.json` 公钥确定的拓展 ID：对 DER 公钥做 SHA-256，取前 16 字节的 32 个十六进制字符，将 0–f 映射为 a–p。私钥未入库。包装脚本固定项目 `.venv`、配置路径及 `PYTHONPATH`，不依赖 Chrome 的工作目录或系统 PATH。

`python -m audio_notes.cli --config ... native-host` 从 stdin 读取一条消息，写一条回复后退出。消息帧为 **4 字节小端无符号长度 + UTF-8 JSON**；长度按字节计，连接器将请求/回复限制为 1 MiB，音频本身不在消息中。所有状态文本和异常日志进入 `state/logs/native-host.log`，stdout 仅输出协议帧。

| 请求 | 行为与回复 |
| --- | --- |
| `{"type":"ping"}` | `{ok:true, version, api_version}`，不启动工作台、不加载模型 |
| `{"type":"import","path":"/绝对路径/录音.mkv"}` | 复用 `manual.selected_files()` 校验格式、存在性和非空；调用 `ensure_workbench()`，流式上传至 `/api/upload`；返回 `{ok:true, reused:false, request, name}` 或 `{ok:true, reused:true, id, name}` |
| `{"type":"open"}` | 优先打开 `/Applications/听记.app` 或 `~/Applications/听记.app`，否则复用 `open_workbench()`；返回 `{ok:true}` |

错误统一回复 `{ok:false,error:"中文提示"}`。上传使用 `Authorization: Bearer` 和与前端 `encodeURIComponent` 兼容的 `X-Filename`，设置 `Content-Length`，不带 Origin，不把 token 返回拓展。服务端仅监听本机，现有 guard 无改动。导入成功代表任务已入队；转写、文稿优化和说话人处理仍由听记原流水线按当前设置执行。相同音频已完成时由工作台按内容复用；尚未完成时复用现有上传排队机制。

拓展在下载开始时记录 download ID，完成后通过 `downloads.search` 查询浏览器确定的绝对路径（含自动重名后缀）。自动开关存于 `chrome.storage.local.autoTingji`，默认关闭；手动发送和恢复保存复用同一下载发送路径。后台同步注册下载监听器，下载记录镜像到 session storage，供 worker 再次唤醒时继续处理；发送状态也写回该镜像供面板刷新。

验证：

```bash
.venv/bin/python -m pytest tests/test_native_host.py tests/test_workbench.py
(cd browser-extension && npm test)
```

测试使用临时合成文件和真实本机 HTTP Server，覆盖帧边界/中文、MKV/WebM 入队与完成后复用、无效路径、ping 不启动工作台、安装幂等及包装脚本协议。浏览器测试覆盖手动/自动开关、下载完成时序、缺少宿主提示、并发去重、重试与恢复保存。

端到端验收需在自己的 Chrome 加载扩展：录制一段约 10 秒的语音，停止后发送，确认工作台队列出现该 MKV 并完成转写，再次发送确认「复用」；另外开启自动发送再录一次，确认无需点击即可入队。内置浏览器无法加载该拓展。

协议依据：[Chrome Native Messaging 官方说明](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging)。
