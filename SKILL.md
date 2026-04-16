---
name: clawcamkeeper-openclaw
description: 当用户提到 ClawCamKeeper 仓库、这个 skill 本身、OpenClaw 接入、工位防护、危险锁定、安全窗口、告警回推、QQ/Feishu 通知、动作链测试、继续昨天的 ClawCamKeeper 调试、或者要求安装/启动/排障/修改这个项目时，必须优先使用本技能。适用于安装 skill、启动本地服务、查看状态、健康检查、武装/解除武装/恢复、修改主备安全窗口、查看事件与通知、验证消息回推链路、排查端口占用/旧进程/上下文未注册等问题。
---

# ClawCamKeeper OpenClaw Skill

这个 skill 用来操作和排查 **ClawCamKeeper 自包含仓库**。当前包含本文件的目录就是项目根目录，不要再假定外面还有一个“真正源码目录”。

## 先做什么

第一次接手时，不要一上来讨论 WebUI 交互或 Core 细节。先按这个顺序：

1. **确认项目根目录**
   - 当前目录应包含 `SKILL.md`、`main.py`、`requirements.txt`、`cli/`、`core/`、`webui/`
2. **确认本地服务是否在线**
   - 优先跑 `python .\main.py status --json`
   - 若报 `service_unavailable` / `WinError 10061` / `无法连接到服务`，说明服务未启动或端口不可达
3. **必要时启动服务**
   - `python .\main.py run`
4. **再做诊断**
   - `python .\main.py doctor --json`
   - `python .\main.py action-test --full-check --json`（仅在需要动作链实测时）

先把服务、状态、动作链搞清楚，再进入更细的通知链或配置问题。

## 服务生命周期命令（必须记清）

这是这个 skill 最容易混淆、也最该写清楚的部分：

### 直接本地服务命令

- **启动服务**
  - `python .\main.py run`
- **停止服务**
  - `python .\main.py service-stop --json`
- **完全重启服务并等待新代码生效**
  - `python .\main.py service-restart --json`

### 什么时候用哪个

- 只是让服务跑起来：`run`
- 明确要求停服务：`service-stop --json`
- 改了代码/配置后要重新加载并确认新实例接管：`service-restart --json`

### 重要区别

- `run` 是前台常驻服务命令；适合直接启动或配合后台执行
- `service-stop` / `service-restart` 是**受管生命周期命令**，会处理实例文件、旧进程、监听接管与回滚
- **改代码后优先用 `service-restart --json` 验证新代码是否真的生效**，不要只补一个新的 `run`

### OpenClaw 适配入口

当需要从 OpenClaw 上下文调用时，对应桥接命令是：

- `python .\main.py openclaw status`
- `python .\main.py openclaw arm`
- `python .\main.py openclaw recover`
- `python .\main.py openclaw service-stop`
- `python .\main.py openclaw service-restart`

但在**本仓库本机排障**时，优先还是直接用顶层命令：
- `python .\main.py status`
- `python .\main.py doctor`
- `python .\main.py run`
- `python .\main.py service-stop --json`
- `python .\main.py service-restart --json`

## 稳定边界

- **唯一稳定自动化边界 = CLI JSON 输出**
- 本地仓库排障/验证时，优先使用：`python .\main.py ... --json`
- OpenClaw bridge 场景，再使用：`python .\main.py openclaw ...`
- 不要直接操作 `core/` 内部对象
- 不要把 WebUI 页面点击当成自动化主路径
- 读取结果时优先看：`ok`、`message`、`data`、`timings`、`state_snapshot`、`error_type`、`debug`

如果有包装脚本 `scripts/invoke-clawcamkeeper-openclaw.ps1`，可以优先用它；但确认在仓库根目录时，直接 `python .\main.py ...` 也可以。

## 首次分发后的最小配置

把 skill 分发到新环境后，通常**需要改一部分静态配置**，但**不要把 session 绑定信息写死进配置文件**。

### 适合写进 `config/settings.yaml` 的静态配置

这些属于长期稳定配置：

- `safe_window.primary` / `safe_window.backup`
- `risk_apps`
- `webui.port`
- `webui.host` / `webui.allow_lan`（默认仅允许 `127.0.0.1` / `localhost`；只有显式开启 `allow_lan` 后，才允许监听局域网地址）
- `openclaw.notifications.enabled`
- `openclaw.notifications.command`
- `openclaw.notifications.timeout_seconds`
- `openclaw.notifications.context_ttl_seconds`
- `openclaw.notifications.message_prefix`
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
- 重点关注 `action_success / danger_lock / action_failure`
- **最终是否真的锁定，以 `status` 为准**；不要只凭某一条通知下结论

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

## Feishu / QQ 通知链规则

这是这轮最容易绕弯的地方，直接记结论：

- **危险告警主路径是渠道直发，不是复杂会话编排**
- **Feishu 已实测可走直连 HTTP 后备发送**
- **QQBot 场景优先走直连 HTTP 发送**
- OpenClaw CLI 消息发送是兜底路径，不是首选低延迟主路径
- **最终业务状态以 `status` / `state_snapshot` 为准，不以单条消息为准**

### 当前通知链的正确理解

1. 运行中的本地服务会保存最近一次通知上下文：`session_key / session_label / channel / target / account`
2. 通过 `python .\main.py openclaw-context ...` 或 bridge 自动注册上下文
3. 危险事件进入通知队列后：
   - **QQBot**：优先直连 QQ HTTP API 发送
   - **Feishu**：优先走 Feishu 直连发送能力
   - 若渠道直发失败：退回 OpenClaw CLI 消息发送兜底
   - 其他渠道：默认走 OpenClaw CLI 发送

### Feishu 接入最佳实践（2026-04-16 实测后收束）

1. 先启动服务：`python .\main.py run`
2. 先看健康状态：`python .\main.py doctor --json`
3. 再看会话绑定：`python .\main.py openclaw-context-show`
4. 确认当前上下文至少包含：
   - `channel=feishu`
   - `target=user:ou_xxx`（私聊）或真实群目标
   - `account=default`
5. 做一次烟雾测试：`python .\main.py notification-test --message "smoke test" --json`
6. 回读 `status.notification_channel.last_dispatch`：
   - 看 `ok`
   - 看 `status`
   - 看 `effective_path` / `primary_path`
   - Feishu 当前实测成功路径可见 `sent_via_direct_http` / `feishu_direct_http`
7. 再执行武装与真人触发测试
8. 触发后不要只看“有没有消息”，还要回读：
   - `python .\main.py status --json`
   - `python .\main.py events --limit 10 --json`
   - `python .\main.py notifications --since-id 0 --limit 10`

### 这轮 Feishu 联调踩过的坑

- **不要在摄像头未恢复时，把“armed”当成“已可实测”**
  - 真正要看的是 `camera_available=true` 且 `is_protecting=true`
- **不要把中途状态当最终结论**
  - `action_success` 消息到了，不等于最终业务状态已经稳定
  - 是否已进入危险锁定，要以 `status.arm_state=danger_locked`、`is_locked=true` 为准
- **Feishu 链路是否通，不要靠猜**
  - 先看 `openclaw-context-show`
  - 再看 `status.notification_channel.last_dispatch`
- **不要一怀疑旧进程就先手工乱杀**
  - 优先 `service-stop` / `service-restart --json`
  - 只在内置控制失效时，再 `netstat/taskkill`
- **联调时仍要区分“主动消息”与“最终状态”**
  - 当前实现已调整为优先把最终锁定态表达为 `danger_lock`
  - 但联调时仍应回读 `status`，不要只凭消息文案做最终判断

### 做回推联调时的顺序

1. 启动服务：`python .\main.py run`
2. 注册上下文：`python .\main.py openclaw-context --channel <channel> --target <target> --account <account>`
3. 确认上下文：`python .\main.py openclaw-context-show`
4. 做烟雾测试：`python .\main.py notification-test --message "smoke test" --json`
5. 武装：`python .\main.py openclaw arm`
6. 人工触发告警
7. 检查渠道是否收到主动提醒
8. 必要时再查 `notifications` / `events` / `status.notification_channel`
9. 最后回读 `status`，确认最终业务状态

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
- 优先 `python .\main.py service-stop --json`
- 再 `python .\main.py service-restart --json`
- 只在内置生命周期命令失效时，再手工查 PID
- 否则你以为在测新代码，实际测的是旧版本

### 通知上下文为空
症状：
- 告警能进本地通知队列，但发不回当前聊天

处理：
- 先注册 `openclaw-context`
- 再做烟雾测试与告警联调

### WebUI 暴露到局域网/外网的边界
症状：
- 想把 `webui.host` 改成 `0.0.0.0`、局域网 IP 或其他非回环地址
- 需要从局域网其他设备访问控制面

处理：
- 默认情况下，项目会强制校验 `webui.host`，仅允许 `127.0.0.1`（`localhost` 会被归一化为 `127.0.0.1`）
- **只有显式开启 `webui.allow_lan=true` 后**，才允许把 host 设为 `0.0.0.0` 或指定局域网 IP
- 即便开启局域网访问，也应自行确保防火墙、网段边界与代理暴露策略正确，不要把 WebUI 直接裸奔到公网

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
