# -*- coding: utf-8 -*-
"""Sync the codex-desktop + agnes-hub proxy mechanism doc to Obsidian + ima."""
import configparser
import json
import sys
import urllib.request
from pathlib import Path

MD = """# 桌面版 Codex × Agnes Hub：namespace 工具代理机制

> 2026-10-19 · 排障结论 + 长期运行机制文档 · 状态：生效中（代理常驻，登录自启）

## 1. 症状

桌面版 Codex（MSIX `OpenAI.Codex_26.1013.4894.0`，进程名 ChatGPT.exe）无法对话：输入任何信息都报

`***.BadRequestError: OpenAIException - tools[4].type: unknown variant 'namespace', expected one of function, web_search_preview, code_interpreter, mcp`

## 2. 根因链

1. 应用的模型 provider 指向 Agnes hub（`~/.codex/config.toml`：model=agnes-3.0-flash，`[model_providers.agnes]` base_url=https://apihub.agnes-ai.com/v1，wire_api=responses）
2. 应用内置 codex 运行时 **0.163.0-alpha.10.2**（MSIX `resources\\codex.exe`），每次启动都会 relocate：先删后拷到 `AppData\\Local\\OpenAI\\Codex\\bin\\896a0166b774037a\\`（rg.exe 在兄弟目录 ce4759538b7f2f1a）
   - relocate 步骤必须成功，失败则应用无法启动 → 不能预先锁住/替换 bin 目录里的运行时（ACL 锁实测触发 `bundled_executable_relocation_failed EPERM`）
3. 0.163 运行时按应用 UI 注册的动态工具（multi_agent_v1.* / codex_app.* 等）把 `type="namespace"` 的工具序列化进 Responses 请求 → Agnes hub 不认 namespace 变体 → 整请求被拒
4. 0.164.0 源码已有修复（`core/src/tools/spec_plan.rs` 的 `expand_namespace_spec_for_gateway`，对非 OpenAI 网关把 namespace 工具展平为点分函数名），但换 0.164.0 做不到：
   - MSIX 包文件受 VFS 保护，提权也写不进 `resources\\codex.exe`
   - 应用每次启动都会把 0.163 恢复回 bin 目录
5. → 唯一可行：**请求层适配（本地代理）**

## 3. 方案：本地展平代理

```
桌面应用 (Electron) → codex 运行时 0.163（不动）
    → http://127.0.0.1:8787/v1 （本地代理）
    → https://apihub.agnes-ai.com/v1 （Agnes hub）
```

代理行为（Python 3.14 纯标准库，单文件约 150 行）：

- POST /v1/responses JSON 请求：把 `tools[]` 里 `type="namespace"` 的条目展开为顶层 `function` 工具，嵌套工具名变成 `namespace.tool`（如 `multi_agent_v1.spawn_agent`），其余工具原样透传
- `tools` 为空时剥离 `tool_choice` 参数（hub 在无 tools 时拒绝 tool_choice，报 400）
- SSE/流式响应边收边发（64KB 块 + flush），不整包缓冲
- 非 2xx 响应记录前 4KB body（日志 RESP_HTTP_xxx）
- 展平过的请求转储到 `requests/<时间戳>.json` 便于 wire 排查

## 4. 组件清单

| 项 | 路径 / 位置 |
|---|---|
| 代理脚本 | `E:\\back-ai\\codex\\agnes_proxy\\proxy.py` |
| 日志 | `E:\\back-ai\\codex\\agnes_proxy\\proxy.log`（5MB 轮转 → .log.old） |
| 展平请求转储 | `E:\\back-ai\\codex\\agnes_proxy\\requests\\` |
| config 改动 | `C:\\Users\\HP530\\.codex\\config.toml` → `[model_providers.agnes] base_url = "http://127.0.0.1:8787/v1"` |
| config 原备份 | `E:\\back-ai\\codex\\agnes_proxy\\config.toml.bak` |
| 自启任务 | `AgnesCodexProxy`（AtLogOn，`C:\\Python314\\python.exe proxy.py`，StartWhenAvailable + 崩溃重启 3 次） |
| 任务重注册脚本 | `E:\\back-ai\\codex\\agnes_proxy\\reg_task.ps1` |
| 0.163 运行时备份 | `E:\\back-ai\\codex\\backup\\2026-10-19-0.163-alpha\\` |
| 应用 JS 提取 | `E:\\back-ai\\codex\\asar_extract\\`（app.asar 1.6 万文件，查 wire 格式用） |

## 5. 维护

- 代理挂了（现象：桌面版/终端 CLI 连不上模型）→ `C:\\Python314\\python.exe E:\\back-ai\\codex\\agnes_proxy\\proxy.py`；注销/重启后登录任务会自动拉起
- 坑：Windows SO_REUSEADDR 允许两个实例同时 bind 8787，用 `Get-CimInstance Win32_Process -Filter "Name='python.exe'"` 确认只有一个 proxy 实例
- 回滚到原始状态：把 `config.toml` 的 base_url 改回 `https://apihub.agnes-ai.com/v1`（原文件备份在 config.toml.bak）→ 停代理 → 删除 AgnesCodexProxy 任务（namespace 报错会回来）

## 6. 验证记录（2026-10-19）

- 09:40:15 FLATTENED(multi_agent_v1.*) → 上游 200，160KB
- 09:45:32 FLATTENED → 200，286KB；其后无 4xx/5xx，应用日志干净
- 09:35 段的 503 为 hub 侧瞬态；09:40:35 的 400（tool_choice）已由剥离逻辑解决

## 7. 后续演进

- OpenAI 若发新版桌面应用（运行时 ≥0.164，含展平修复）：代理自动退化为纯透传，可保留不动
- 更干净的长期修复：Agnes hub 的 Responses API 增加 namespace 工具变体，则客户端完全不用改
"""

out_md = Path(r"E:\back-ai\Hermes相关\Codex\Codex桌面版-Agnes代理机制-2026-10-19.md")
out_md.parent.mkdir(parents=True, exist_ok=True)
out_md.write_bytes(MD.encode("utf-8"))
print("[obsidian] wrote", out_md, out_md.stat().st_size, "bytes")

cfg = configparser.ConfigParser()
cfg.read(r"E:\BACK-AI\Hermes-win\ima_credentials.ini", encoding="utf-8")
client_id = cfg["ima"]["client_id"].strip()
api_key = cfg["ima"]["api_key"].strip()

body = json.dumps({"title": "Codex桌面版-Agnes代理机制-2026-10-19", "content": MD,
                   "content_format": "1"}, ensure_ascii=False).encode("utf-8")
req = urllib.request.Request(
    "https://ima.qq.com/openapi/note/v1/import_doc",
    data=body,
    headers={
        "ima-openapi-clientid": client_id,
        "ima-openapi-apikey": api_key,
        "Content-Type": "application/json",
    },
    method="POST",
)
with urllib.request.urlopen(req, timeout=60) as resp:
    result = json.loads(resp.read().decode("utf-8"))
print("[ima] import_doc ->", json.dumps(result, ensure_ascii=False)[:800])
if result.get("code") != 0:
    sys.exit(1)
