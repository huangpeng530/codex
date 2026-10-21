# agnes_proxy — 桌面版 Codex × Agnes Hub 兼容代理

桌面版 Codex（MSIX 包，进程 ChatGPT.exe）的内置 codex 运行时会把动态工具（multi_agent_v1.* /
codex_app.* 等）序列化成 Responses 请求里的 `type:"namespace"` 工具，Agnes hub
（`apihub.agnes-ai.com`）不接受该变体，整请求被 400 拒绝。

`proxy.py` 是 127.0.0.1:8787 的本地转发代理，解决方式：

- 把 `type:"namespace"` 工具展平为点分名的 function 工具
- 携带图片的改写走 /v1/chat/completions 再包回 Responses 格式（纯文本直通透过）
- 上游瞬时错误 "Generation failed, try later" 自动重试
- 仅标准库（Pillow 可选，用于压缩大图）

看门狗计划任务每 5 分钟检查一次 8787 端口，掉线自动拉起，因此代理可长期常驻。

## 文件

| 文件 | 作用 |
| --- | --- |
| `proxy.py` | 代理本体（端口 8787，仅标准库） |
| `launch.ps1` | 隐藏启动器；自动解析 python 路径，防重复启动 |
| `watchdog.ps1` | 端口探测 + 自动重启 + 滚动日志（200 行） |
| `install.ps1` | 幂等注册/注销两个计划任务（路径无关，可重跑） |
| `codex-config.example.toml` | `~/.codex/config.toml` 示例（base_url 指向代理） |
| `sync_doc.py` | 把机制文档同步到 Obsidian / ima（工具脚本） |

## 安装 / 重装恢复（三步）

1. 克隆仓库到任意路径：
   `git clone git@github.com:huangpeng530/codex.git`
2. 注册计划任务（需管理员；脚本会自动 UAC 提权重跑）：
   `powershell -NoProfile -ExecutionPolicy Bypass -File <仓库>\agnes_proxy\install.ps1`
   生成 `AgnesCodexProxy`（登录自启）与 `AgnesCodexProxyWatchdog`（每 5 分钟检查）；
   任务参数指向克隆目录本身，换路径无需改任何脚本。
3. 恢复 Codex 配置与密钥：
   - 复制 `codex-config.example.toml` → `%USERPROFILE%\.codex\config.toml`
   - 设置机器级环境变量（重启后仍在）：`setx /M AGNES_API_KEY <你的密钥>`
   - 可选：指定 python：`setx /M AGNES_PROXY_PYTHON C:\Python314\python.exe`

之后正常登录 Windows 即自动生效；需要停用时：
`powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Unregister`

## Python 解析顺序（launch.ps1）

1. `AGNES_PROXY_PYTHON` 环境变量
2. PATH 里的 `python`
3. `C:\Python3*\python.exe`（取版本号最高的）
4. `%LOCALAPPDATA%\Programs\Python\Python3*\python.exe`

## 日志

- `proxy.log`：代理自身日志（5MB 滚动）
- `watchdog.log`：看门狗动作记录（200 行滚动）
- `requests\`：原始请求捕获（运行时产物，不入库）
