---
name: clawcamkeeper-openclaw
description: 当用户提到 ClawCamKeeper 仓库、这个 skill 本身、OpenClaw 接入、工位防护、危险锁定、安全窗口、告警回推、QQ/Feishu 通知、动作链测试、继续昨天的 ClawCamKeeper 调试、或者要求安装/启动/排障/修改这个项目时，必须优先使用本技能。适用于安装 skill、启动本地服务、查看状态、健康检查、武装/解除武装/恢复、修改主备安全窗口、查看事件与通知、验证消息回推链路、排查端口占用/旧进程/上下文未注册等问题。
---

# ClawCamKeeper OpenClaw Skill

这个 skill 用来操作和排查 **ClawCamKeeper 自包含仓库**。当前包含本文件的目录就是项目根目录，不要再假定外面还有一个“真正源码目录”。

## 先做什么

第一次接手时，不要一上来讨论 ACP、WebUI 交互或 Core 细节。先按这个顺序：

1. **确认项目根目录**
   - 当前目录应包含 `SKILL.md`、`main.py`、`requirements.txt`、`cli/`、`core/`、`webui/`
2. **确认本地服务是否在线**
   - 先跑 `python .\main.py openclaw status`
   - 若报 `service_unavailable` / `WinError 10061`，说明服务未启动或端口不可达
3. **必要时启动服务**
   - `python .\main.py run`
4. **再做诊断**
   - `python .\main.py openclaw doctor`
   - `python .\main.py openclaw action-test --full-check`（仅在需要动作链实测时）

先把服务、状态、动作链搞清楚，再进入更细的通知链或配置问题。

## 稳定边界

- **唯一稳定自动化边界 = CLI JSON 输出**
- 优先使用：`python .\main.py openclaw ...`
- 不要直接操作 `core/` 内部对象
- 不要把 WebUI 页面点击当成自动化主路径
- 读取结果时优先看：`ok`、`message`、`data`、`timings`、`state_snapshot`、`error_type`、`debug`

如果有包装脚本 `scripts/invoke-clawcamkeeper-openclaw.ps1`，可以优先用它；但确认在仓库根目录时，直接 `python .\main.py openclaw ...` 也可以。

## 首次分发后的最小配置

把 skill 分发到新环境后，通常**需要改一部分静态配置**，但**不要把 session 绑定信息写死进配置文件**。

### 适合写进 `config/settings.yaml` 的静态配置

这些属于长期稳定配置：

- `safe_window.primary` / `safe_window.backup`
- `risk_apps`
- `webui.host` / `webui.port`
- `openclaw.notifications.enabled`
- `openclaw.notifications.routes.<channel>.target`
- `openclaw.notifications.routes.<channel>.account`
- `openclaw.notifications.fallback.channel / target / account`

### 不建议写死进配置文件的运行时上下文

这些更适合在运行时由 `openclaw-context` 注册，而不是作为安装后手填配置项：

- `session_key`
- `session_label`
- 当前会话临时绑定的 `channel / target / account`

原因很简单：`session_*` 代表的是**某次会话**或**某次当前聊天上下文**，不是长期稳定配置。把它们写死进配置文件，容易把临时上下文和长期路由混在一起。

### 正确做法

- **长期默认路由**：写进 `routes` / `fallback`
- **当前会话绑定**：通过 `python .\main.py openclaw-context ...` 在运行时注册

也就是说：
- 新环境首次安装后，通常要改 `settings.yaml`
- 但通常**不需要**让用户手动去写 `session id`

## 30 秒分流

### 1) 用户要看当前保护状态
按顺序：
- `python .\main.py openclaw status`
- 提炼 `arm_state`、`is_locked`、`camera_available`、`safe_window_available`、`action_chain_available`
- 汇报是否具备真实保护能力

### 2) 用户要远程控制
按顺序：
- 先 `python .\main.py openclaw doctor`
- 再 `arm` / `disarm` / `recover`
- **必须**回读 `state_snapshot` 或再次 `status` 确认是否真的生效

### 3) 用户要排查“为什么不能救场”
按顺序：
- `python .\main.py openclaw doctor`
- `python .\main.py openclaw action-test`
- 必要时 `python .\main.py openclaw action-test --full-check`
- 判断是服务问题、摄像头问题、安全窗口问题，还是动作链问题

### 4) 用户要看最近关键动作/锁定提醒
按顺序：
- `python .\main.py openclaw notifications --since-id 0 --limit 10`
- 如果需要完整时间线，再补 `python .\main.py openclaw events --limit 10`
- 如果出现 `danger_lock`，必须再读一次 `status`

### 5) 用户要改安全窗口
按顺序：
- `python .\main.py openclaw set-safe-window --primary <主> --backup <备>`
- 检查 `data.config_set`、`data.config_reload`
- 检查 `state_snapshot.primary_safe_app`、`state_snapshot.backup_safe_app`
- 若保存成功但热加载失败，要明确说明“配置已落盘，但运行中的服务未热加载成功”

## 配置边界

通知联调时最容易混淆的一点是：

- **静态配置** 应放在 `config/settings.yaml`
- **运行时上下文** 应通过 `openclaw-context` 注册

不要把 `session_key / session_label` 当作长期配置项持久化。它们属于当前会话语义，不属于安装后长期配置。

## 通知链规则

这是最容易绕弯的地方，直接记结论：

- **危险告警主路径不是 ACP**
- **QQBot 场景优先走直连 HTTP 发送**
- OpenClaw CLI 消息发送是兜底路径，不是 QQ 的首选低延迟路径
- ACP 更适合“把复杂 agent 工作送回某会话继续聊”，不适合危险告警主链路

### 当前通知链的正确理解

1. 运行中的本地服务会保存最近一次通知上下文：`session_key / session_label / channel / target / account`
2. 通过 `python .\main.py openclaw-context ...` 或 bridge 自动注册上下文
3. 危险事件进入通知队列后：
   - **QQBot**：优先直连 QQ HTTP API 发送
   - 若直连失败：退回 OpenClaw CLI 消息发送兜底
   - 其他渠道：默认走 OpenClaw CLI 发送

### 做回推联调时的顺序

1. 启动服务：`python .\main.py run`
2. 注册上下文：`python .\main.py openclaw-context --channel <channel> --target <target> --account <account>`
3. 确认上下文：`python .\main.py openclaw notification-context`
4. 武装：`python .\main.py openclaw arm`
5. 人工触发告警
6. 检查渠道是否收到主动提醒
7. 必要时再查 `notifications` / `events` / `status.notification_channel`

## 常见坑

### 服务没起来
症状：
- `service_unavailable`
- `WinError 10061`

处理：
- 先启动 `python .\main.py run`
- 不要在服务没起来时连续重试写操作

### 8765 端口被旧实例占用
症状：
- 新服务启动日志里出现 bind 失败
- 看起来启动了，但实际跑的是旧代码

处理：
- 先找占用 8765 的旧进程
- 清掉旧进程后再启动新实例
- 否则你以为在测新代码，实际测的是旧版本

### 通知上下文为空
症状：
- 告警能进本地通知队列，但发不回当前聊天

处理：
- 先注册 `openclaw-context`
- 再做告警联调

### 主安全窗口不可用
症状：
- 动作链看起来异常
- 其实只是 `Weixin.exe` 没打开或不可启动

处理：
- 先确认主安全窗口是否真实可用
- 必要时允许回退到备选窗口
- 不要把“主窗口不可用”和“整个动作链坏了”混为一谈

## 危险锁定语义

- 危险锁定后不要默认帮用户解除
- 恢复只能显式调用 `recover`
- `session-label` 只做会话隔离，不承载业务状态
- 武装/锁定/恢复判断必须回到 `status` 或 `state_snapshot`

## 输出要求

向用户汇报时：

1. 先给结论
2. 再给关键状态字段
3. 最后给耗时或错误原因
4. 不要整段倾倒 JSON，除非用户明确要原始结果

## 附加文档

当任务更复杂时，再读：

- `README.md`：项目总览与运行说明
- `docs/NOTIFICATION-FLOW.md`：主动通知回推设计与联调顺序
- `docs/TROUBLESHOOTING.md`：端口占用、服务不可达、上下文未注册等故障处理
- `AGENTS.md`：仓库内部约束与行为边界
