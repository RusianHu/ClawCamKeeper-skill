# Notification Flow

这份文档只讲一件事：**ClawCamKeeper 的主动通知怎么走，联调时应该先查什么。**

## 结论先说

- 危险告警主路径是**渠道直发 + 本地状态回读**
- **Feishu 已实测可走直连 HTTP 后备发送**
- **QQBot 场景优先走直连 HTTP 发送**
- OpenClaw CLI 消息发送是 **兜底路径**
- **最终业务状态要以 `status` / `state_snapshot` 为准，不以单条通知文案为准**

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
   - **Feishu**：优先走 Feishu 渠道直发能力
   - **若渠道直发失败**：退回 `openclaw message send`
   - **其他渠道**：默认走 `openclaw message send`
6. 分发结果写入 `delivery.dispatch` 与最近一次 dispatch 状态

## 静态配置 vs 运行时上下文

这里必须分清楚：

### 静态配置（适合写进 `settings.yaml`）
- 默认 `routes`
- `fallback`
- 是否启用通知
- 默认账号

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

## 已验证联调结果

### QQ（2026-04-14）

在 Windows 11 本机环境下，以下链路已经完成真人联调与重复压测：

- 当前 QQ 私聊上下文注册成功
- 真人摄像头触发成功
- 状态推进 `pre_alert -> full_alert -> danger_locked`
- 动作链执行成功（安全窗口切换 / 风险程序最小化）
- QQBot 直连 HTTP 主动回推成功
- 多轮重复压测后仍能成功落锁

### Feishu（2026-04-16）

本轮 Feishu 真人联调已经确认：

- 当前 Feishu 私聊上下文自动绑定成功
- `notification-test` 烟雾测试成功
- `status.notification_channel.last_dispatch` 显示 `sent_via_direct_http`
- 真人触发后消息已成功发回 Feishu 会话
- 动作链执行成功，最终状态已正确进入 `danger_locked`

本轮最重要的经验不是“Feishu 理论上可用”，而是：

> **Feishu 回推链已经打通，而且当前实现应优先把最终锁定态通知为 `danger_lock`；但最终业务状态仍必须靠 `status` 回读确认。**

也就是说：

- 即使通知文案看起来已经到位，也不代表联调结束
- 还要确认：
  - `arm_state=danger_locked`
  - `is_locked=true`
  - `last_event_message=进入危险锁定状态`

## 联调 SOP

### 场景：我要验证 Feishu / QQ 当前聊天能不能收到危险告警

按这个顺序：

1. 确保运行的是新代码
   ```powershell
   python .\main.py service-restart --json
   ```
   重点确认：
   - `start.pid`
   - `start.listening_pids`
   - 新 PID 确实接管了 `8765`

2. 注册或确认上下文
   ```powershell
   python .\main.py openclaw-context-show
   ```
   如果没有活动上下文，再执行：
   ```powershell
   python .\main.py openclaw-context --channel <channel> --target <target> --account default
   ```
3. 先做主动通知链路烟雾测试
   ```powershell
   python .\main.py notification-test --message "smoke test" --json
   ```
4. 回读 `status.notification_channel.last_dispatch`
5. 健康检查
   ```powershell
   python .\main.py doctor --json
   ```
6. 武装
   ```powershell
   python .\main.py openclaw arm
   ```
7. 人工触发危险事件
8. 观察当前聊天是否收到主动提醒
9. 再检查：
   ```powershell
   python .\main.py openclaw notifications --since-id 0 --limit 10
   python .\main.py openclaw events --limit 10
   python .\main.py openclaw status
   ```
10. 联调结束后，如无需继续监控，执行：
   ```powershell
   python .\main.py service-stop
   ```

## Feishu 联调最容易踩的坑

### 1. “显示 armed” 不等于已经可以实测
必须继续看：
- `camera_available=true`
- `is_protecting=true`

如果摄像头还没恢复，先别触发真人测试。

### 2. 只看消息，不看最终状态
即使当前实现优先推送 `danger_lock`，最终锁定状态也需要再回读 `status` 才能确认。

### 3. 误把通知事件名当成业务状态名
通知事件名是对外表达，业务状态仍以状态机为准。
所以通知文案与最终状态要分开验证。

### 4. 一怀疑旧进程就手工乱杀
优先：
```powershell
python .\main.py service-stop --json
python .\main.py service-restart --json
```

只有内置生命周期命令失效时，才再 `netstat/taskkill`。

## 常见故障

### 1. 明明触发了，但消息没发回来
优先排查：
- 上下文是否没注册
- `channel / target / account` 是否为空
- 当前事件是否属于主动推送事件（如 `action_success` / `danger_lock`）
- 渠道直发是否失败
- CLI 兜底是否也失败

### 2. 你以为在测新代码，实际上跑的是旧实例
症状：
- 8765 端口已有旧进程占用
- 新进程 bind 失败
- 日志看起来启动过，但 WebUI / API 实际还是旧版本

处理：
- 优先 `service-stop --json` + `service-restart --json`
- 再确认监听 PID 是否真的切换

### 3. 收到了提醒，但不确定是否真的锁定
这是 Feishu / QQ 联调里最容易误判的一种。

处理：
- 立即回读 `status`
- 再看 `events`
- 最终以 `arm_state=danger_locked`、`is_locked=true` 为准
