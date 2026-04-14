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

## 哪些事件会主动推送

当前主动推送事件主要包括：

- `action_success`
- `danger_lock`
- `action_failure`
- `camera_failure`

而像 `arm` / `disarm` / `recover` 这类更偏状态变化的事件，默认更适合查询，不一定作为主动危险告警主消息。

## 联调 SOP

### 场景：我要验证 QQ 私聊能不能收到危险告警

按这个顺序：

1. 启动服务
   ```powershell
   python .\main.py run
   ```
2. 注册上下文
   ```powershell
   python .\main.py openclaw-context --channel qqbot --target qqbot:c2c:YOUR_TARGET --account default
   ```
3. 检查上下文
   ```powershell
   python .\main.py openclaw notification-context
   ```
4. 武装
   ```powershell
   python .\main.py openclaw arm
   ```
5. 人工触发危险事件
6. 观察 QQ 是否收到主动提醒
7. 再检查：
   ```powershell
   python .\main.py openclaw notifications --since-id 0 --limit 10
   python .\main.py openclaw events --limit 10
   python .\main.py openclaw status
   ```

## 常见故障

### 1. 明明触发了，但消息没发回来
优先排查：
- 上下文是否没注册
- `channel / target / account` 是否为空
- 当前事件是否属于主动推送事件
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
