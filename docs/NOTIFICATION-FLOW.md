# Notification Flow

这份文档只讲一件事：**ClawCamKeeper 的主动通知怎么走，联调时应该先查什么。**

## 结论先说

- 危险告警主路径不是 ACP
- QQBot 场景优先走 **直连 HTTP 发送**
- OpenClaw CLI 消息发送是 **兜底路径**
- ACP 更适合复杂会话工作继续流转，不适合低延迟危险告警主链路

## 参与组件

- `cli/main.py`：本地 CLI 与 `openclaw-context` 注册入口
- `cli/openclaw_bridge.py`：机器可读 OpenClaw bridge
- `webui/app.py`：`/api/openclaw/notification-context` 与 `/api/notifications`
- `core/engine.py`：通知队列、路由解析、主动发送

## 通知链步骤

1. 本地服务启动：`python .\main.py run`
2. 当前 OpenClaw 会话/渠道通过 `openclaw-context` 注册上下文
3. 引擎保存最近一次：
   - `session_key`
   - `session_label`
   - `channel`
   - `target`
   - `account`
4. 关键事件进入 `_queue_notification()`
5. `_dispatch_notification()` 选择发送路径：
   - **QQBot**：优先 `_qqbot_direct_send()`
   - **若 QQ 直连失败**：退回 `openclaw message send`
   - **其他渠道**：默认走 `openclaw message send`
6. 分发结果写入 `delivery.dispatch` 与最近一次 dispatch 状态

## 静态配置 vs 运行时上下文

这里必须分清楚：

### 静态配置（适合写进 `settings.yaml`）
- 默认 `routes`
- `fallback`
- 是否启用通知
- 默认账号

> 分发到 GitHub 的仓库时，`routes` / `fallback` 应保持为**通用默认值或空占位**，不要把自己的私聊目标、群目标、open_id、account 等本机路由直接提交进去。

### 运行时上下文（不建议写死进配置文件）
- `session_key`
- `session_label`
- 当前聊天临时绑定的 `channel / target / account`

也就是说：
- `settings.yaml` 负责“默认发到哪儿”
- `openclaw-context` 负责“这一次当前聊天该回到哪儿”

不要把 `session_key / session_label` 当作安装后必须手填的长期配置项。

## 哪些事件会主动推送

当前主动推送事件主要包括：

- `action_success`
- `danger_lock`
- `action_failure`
- `camera_failure`

而像 `arm` / `disarm` / `recover` 这类更偏状态变化的事件，默认更适合查询，不作为主动危险告警主消息。

## 已验证联调结果（2026-04-14）

在 Windows 11 本机环境下，以下链路已经完成真人联调与重复压测：

- 当前 QQ 私聊上下文注册成功
- 真人摄像头触发成功
- 状态推进 `pre_alert -> full_alert -> danger_locked`
- 动作链执行成功（安全窗口切换 / 风险程序最小化）
- QQBot 直连 HTTP 主动回推成功
- 多轮重复压测后仍能成功落锁

因此，当前更准确的结论不是“理论上可用”，而是：

> **危险触发 -> 动作执行 -> 危险锁定 -> QQ 主动回推**
>
> **已经完成真人实测，并具备重复成功能力。**

## 联调 SOP

### 场景：我要验证 QQ 私聊能不能收到危险告警

按这个顺序：

1. 确保运行的是新代码
   ```powershell
   python .\main.py service-restart --json
   ```
   重点确认：
   - `start.pid`
   - `start.listening_pids`
   - `status.runtime_validation.pid_match=true`

2. 注册上下文
   ```powershell
   python .\main.py openclaw-context --channel qqbot --target qqbot:c2c:YOUR_TARGET --account default
   ```
3. 检查上下文
   ```powershell
   python .\main.py openclaw-context-show
   ```
4. 先做主动通知链路烟雾测试
   ```powershell
   python .\main.py notification-test --message "smoke test"
   ```
5. 武装
   ```powershell
   python .\main.py openclaw arm
   ```
6. 人工触发危险事件
7. 观察 QQ 是否收到主动提醒
8. 再检查：
   ```powershell
   python .\main.py openclaw notifications --since-id 0 --limit 10
   python .\main.py openclaw events --limit 10
   python .\main.py openclaw status
   ```
9. 联调结束后，如无需继续监控，执行：
   ```powershell
   python .\main.py service-stop
   ```

## 常见故障

### 1. 明明触发了，但消息没发回来
优先排查：
- 上下文是否没注册
- `channel / target / account` 是否为空
- 当前事件是否属于主动推送事件（如 `action_success` / `danger_lock`）
- QQ 直连是否失败
- CLI 兜底是否也失败

### 2. 你以为在测新代码，实际上跑的是旧实例
症状：
- 8765 端口已有旧进程占用
- 新进程 bind 失败
- 日志看起来启动过，但 WebUI / API 实际还是旧版本

处理：
- 先找并结束占用 8765 的旧进程
- 再重启服务

### 3. 为什么不建议默认走 ACP
因为危险告警需要的是：
- 更短路径
- 更低延迟
- 更少依赖
- 更明确的渠道投递

ACP 更适合“把后续 agent 工作送回某会话继续做”，而不是承担危险告警主链路。
