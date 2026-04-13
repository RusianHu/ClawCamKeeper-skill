---
name: clawcamkeeper-openclaw
description: 当用户想安装、启动、查询、调试或远程控制 ClawCamKeeper 时使用。适用于“帮我安装这个 GitHub skill、启动本地防护服务、查看当前保护状态、健康检查、远程武装、解除武装、手动恢复、查看配置、修改主备安全窗口、查看事件、查看通知、测试动作链”等请求。只要任务涉及这个仓库本身或其 OpenClaw 接入，就应优先使用本技能。
---

# ClawCamKeeper OpenClaw 自包含技能仓库

这个仓库本身就是一个 **self-contained skill repository**。

如果它被 OpenClaw 或其他 agent 工具直接克隆到 skill 工作区，那么**当前包含本文件的目录就是项目根目录**，而不是某个外部源码目录的代理壳。

## 仓库根定位

优先用以下事实确认项目根：

1. 当前目录包含 [`SKILL.md`](SKILL.md)
2. 当前目录同时包含 [`main.py`](main.py)、[`requirements.txt`](requirements.txt)、[`cli/`](cli/__init__.py)、[`core/`](core/__init__.py)、[`webui/`](webui/app.py)
3. 如果存在 [`scripts/invoke-clawcamkeeper-openclaw.ps1`](scripts/invoke-clawcamkeeper-openclaw.ps1)，优先通过它调用，因为它会稳定切回仓库根目录执行命令

不要再假定需要跳回某个“原始开发仓库路径”；对于分发场景，这个 skill 仓库自身就应该是运行载体。

## 核心原则

1. 始终通过项目提供的 CLI / bridge 调用，不直接操作 [`core/`](core/__init__.py) 内部对象。
2. 优先使用 [`scripts/invoke-clawcamkeeper-openclaw.ps1`](scripts/invoke-clawcamkeeper-openclaw.ps1)；若确认当前就在仓库根目录，也可以直接用 [`python .\main.py openclaw ...`](main.py:14)。
3. 始终依赖 JSON 输出，不要解析人类可读文本。
4. 优先把返回结果中的 [`ok`](cli/openclaw_bridge.py:139)、[`message`](cli/openclaw_bridge.py:170)、[`data`](cli/openclaw_bridge.py:143)、[`timings`](cli/openclaw_bridge.py:147)、[`state_snapshot`](cli/openclaw_bridge.py:288) 作为主依据。
5. 如果调用失败，优先查看 [`error_type`](cli/openclaw_bridge.py:153) 与 [`debug`](cli/openclaw_bridge.py:249)。
6. 危险锁定状态下，不要擅自假定可以通过配置修改解除锁定；恢复只能显式调用 [`recover()`](cli/openclaw_bridge.py:343)。
7. [`session_policy`](core/engine.py:202) 表明 `session-label` 只用于会话隔离，不承载业务状态；任何武装、锁定、恢复判断都必须回到 [`status()`](cli/openclaw_bridge.py:280) 或返回内的 `state_snapshot` 确认。
8. 如果用户要看最近关键动作或锁定提示，优先使用 [`notifications()`](cli/openclaw_bridge.py:400)；不要把通知队列误当成完整历史，完整回放仍以 [`events()`](cli/openclaw_bridge.py:376) 为准。

## 首次安装 / 启动检查

如果仓库刚被克隆到 skill 工作区，先检查：

1. Python 是否可用
2. 依赖是否已按 [`requirements.txt`](requirements.txt) 安装
3. 默认配置 [`config/settings.yaml`](config/settings.yaml) 是否存在
4. 本地服务是否已通过 [`run()`](cli/main.py:489) 启动

典型准备步骤：

- 安装依赖：`python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`
- 启动服务：`python .\main.py run`

## 调用入口选择

- **优先入口**：`powershell -ExecutionPolicy Bypass -File .\scripts\invoke-clawcamkeeper-openclaw.ps1 ...`
- **确认位于仓库根目录时**：可直接使用 [`python .\main.py openclaw ...`](main.py:14)
- 如果当前目录不含 [`main.py`](main.py) 或 [`requirements.txt`](requirements.txt)，不要盲调命令，先定位到 skill 仓库根目录

## 可用命令

### 只读查询

- 状态：`python .\main.py openclaw status`
- 健康检查：`python .\main.py openclaw doctor`
- 配置查看：`python .\main.py openclaw config-show`
- 事件查看：`python .\main.py openclaw events --limit 10`
- 通知查看：`python .\main.py openclaw notifications --since-id 0 --limit 10`

### 写操作

- 武装：`python .\main.py openclaw arm`
- 解除武装：`python .\main.py openclaw disarm`
- 恢复：`python .\main.py openclaw recover`
- 修改安全窗口：`python .\main.py openclaw set-safe-window --primary Weixin.exe --backup calc.exe`

### 联调辅助

- 快速动作测试：`python .\main.py openclaw action-test`
- 完整动作测试：`python .\main.py openclaw action-test --full-check`

## 推荐工作流

### 1. 用户要查看当前保护状态

按顺序：

1. 运行 `python .\main.py openclaw status`
2. 提炼 `arm_state`、`is_locked`、`camera_available`、`safe_window_available`、`action_chain_available`
3. 简洁汇报当前是否具备真实保护能力

### 2. 用户要远程控制开关

- 武装前如有疑问，先执行 `python .\main.py openclaw doctor`
- 执行 `arm` / `disarm`
- 必须读取返回中的 `state_snapshot` 来确认结果是否真正生效

### 3. 用户要修改安全窗口

- 使用 `python .\main.py openclaw set-safe-window --primary <主> --backup <备>`
- 重点检查返回中的：
  - `data.config_set`
  - `data.config_reload`
  - `state_snapshot.primary_safe_app`
  - `state_snapshot.backup_safe_app`
- 如果 `config_set` 成功但 `config_reload` 失败，应明确说明“配置已落盘，但运行中的本地服务未热加载成功”

### 4. 用户要诊断为什么不能救场

优先顺序：

1. `python .\main.py openclaw doctor`
2. `python .\main.py openclaw action-test`
3. 必要时 `python .\main.py openclaw action-test --full-check`
4. 结合 [`timings`](cli/openclaw_bridge.py:147)、[`cli_perf`](cli/main.py:42)、[`meta.perf`](webui/app.py:33) 判断是请求层、可用性探测还是动作链本身的问题

### 5. 用户要看最近关键动作 / 锁定提醒 / 恢复提示

按顺序：

1. 先运行 `python .\main.py openclaw notifications --since-id 0 --limit 10`
2. 重点提炼 `data.notifications[*].event_type`、`message`、`state_summary`、`timings`
3. 如果需要完整时间线，再补 `python .\main.py openclaw events --limit 10`
4. 若通知里出现“需人工恢复”或 `danger_lock`，必须再读一次 `python .\main.py openclaw status`，确认当前是否仍为锁定态

## 失败处理规则

- `error_type=service_unavailable`：通常表示本地 [`run()`](cli/main.py:489) 服务未运行或 Web API 不可达，应先恢复本地服务。
- `error_type=timeout`：说明调用超时，先保留现场，不要重复连发写操作。
- `error_type=invalid_arguments`：说明参数不完整或格式错误，先修正参数。
- 若写操作失败且没有明确状态快照，默认视为“未确认生效”，不要向用户声称已经切换成功。

## 输出要求

向用户汇报时：

1. 先给结果结论。
2. 再给关键状态字段。
3. 最后给必要的耗时或错误原因。
4. 不要把整个 JSON 原样倾倒给用户，除非对方明确要求原始结果。
