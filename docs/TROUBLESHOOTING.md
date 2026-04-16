# Troubleshooting

这份文档聚焦 **首次接手最容易踩的坑**。

## 1. `service_unavailable` / `WinError 10061`

### 症状
- `python .\main.py openclaw status` 失败
- `doctor` 失败
- 日志里出现 `由于目标计算机积极拒绝，无法连接`

### 原因
- 本地服务没启动
- WebUI / API 没监听到配置端口

### 处理
```powershell
python .\main.py run
python .\main.py openclaw status
python .\main.py openclaw doctor
```

不要在服务没起来时连续重复写操作。

---

## 2. 8765 端口被旧实例占用 / 看起来重启了但其实还是旧代码

### 症状
- 新服务日志里出现 bind 失败
- 看起来启动成功过，但实际响应的是旧版本
- 修改代码后效果不生效
- `service-restart` 前后 API 表现不一致

### 正确处理
现在**优先使用内置控制命令**，不要先手工 `netstat/taskkill`：

```powershell
python .\main.py service-stop
python .\main.py service-restart --json
```

重点检查 `service-restart --json` 返回中的：
- `start.pid`
- `start.listening_pids`
- 新 PID 是否真正接管监听

### 兜底手工排查（仅在内置命令失效时）
```powershell
netstat -ano | findstr :8765
taskkill /PID <PID> /F
python .\main.py run
```

如果不先处理旧实例，你很可能在拿旧代码做测试。

---

## 3. 告警进了本地通知队列，但没发回当前聊天

### 典型原因
- `openclaw-context` 没注册
- 当前上下文缺少 `channel` / `target`
- 当前事件不属于主动推送事件

### 处理
```powershell
python .\main.py openclaw-context-show
python .\main.py openclaw-context --channel qqbot --target qqbot:c2c:YOUR_TARGET --account default
python .\main.py notification-test --message "smoke test" --json
```

如果你当前主要在 QQ 私聊联调，再补一层稳妥配置：
- `routes.qqbot.target` 指向同一个 `qqbot:c2c:...`
- `fallback.channel/target` 也指向同一个 QQ 目标
- 如果真人测试经常隔很久，再适当调大 `context_ttl_seconds`

然后再做告警联调。

---

## 4. `Weixin.exe` 不可用，看起来像动作链坏了

### 现象
- 动作测试失败或回退
- 主安全窗口不可用
- 但备选窗口其实可用

### 处理
先区分两件事：
- **主安全窗口不可用**
- **整个动作链不可用**

不要把它们混为一谈。

建议顺序：
```powershell
python .\main.py openclaw doctor
python .\main.py openclaw action-test --full-check
```

确认是主窗口缺失、备选回退生效，还是动作链本身失效。

---

## 5. 为什么明明武装成功，却没看到“正在保护”

`arm` 成功只代表状态机进入武装，不等于：
- 摄像头可用
- 安全窗口可用
- 动作链可用

仍要回读：
```powershell
python .\main.py openclaw status
```

重点看：
- `arm_state`
- `camera_available`
- `safe_window_available`
- `action_chain_available`
- `is_protecting`

---

## 6. 通知链到底该查哪儿

最短路径：

1. `notification-context` / `openclaw-context-show`：看上下文是否已注册
2. `notification-test`：先做烟雾测试
3. `notifications`：看事件是否进入通知队列
4. `events`：看完整时间线
5. `status`：看当前状态和通知通道摘要

如果是 Feishu：默认应优先怀疑
- 当前上下文是否真的是 `channel=feishu`
- `target` 是否是 `user:ou_xxx` 或正确聊天目标
- `last_dispatch` 是否成功
- 收到的消息是不是只是中途事件，不是最终状态

---

## 7. 触发后反复切窗口

### 正确行为
- 完全报警成功后，应当**切一次安全态就进入危险锁定**
- 锁定后保持静默，不应继续反复切窗
- 只有用户显式 `recover` 或 `disarm` 后，系统才允许下一次新的报警动作

### 如果你看到反复切窗
优先怀疑：
- 成功切窗后仍保持 `armed + 持续检测`
- 或仅重置了报警阶段，没有真正进入危险锁定
- 导致人在画面里持续存在时，又重新累计预警帧并再次触发

### 当前修复思路
- 成功切窗后直接 `enter_danger_lock()`
- 立即停止检测线程
- 下一次触发必须依赖用户显式恢复或解除武装

---

## 8. 查询时看到 `full_alert`，不一定代表最终没锁上

### 现象
在真人联调或重复压测时，可能会出现这样一种观感：

- `events` / `notifications` 先显示 `pre_alert`
- 随后显示 `full_alert`
- 此时如果你立刻查询，可能暂时还没看到最终 `danger_locked`

容易误判成：
- “是不是只到完全报警，没真正锁死？”

### 正确认知
这未必是失败，也可能只是**状态推进时序**。

在多轮真人联调中，现场已经验证过：
- 中途查询确实可能先看到 `full_alert`
- 当前实现会优先把最终锁定态对外表达为 `danger_lock`
- 但稍后再次回读 `status`，最终状态才算真正确认落到 `danger_locked`

因此，判断是否真正锁定，**以最终 `status` 为准**，重点看：
- `arm_state=danger_locked`
- `is_locked=true`
- `last_event_message=进入危险锁定状态`

### 推荐处理
如果你在联调时正好撞到这个时间窗：

```powershell
python .\main.py openclaw status
python .\main.py openclaw events --limit 10
python .\main.py openclaw notifications --since-id 0 --limit 10
```

优先用 `status` 判定最终结果，不要只凭中途一拍的 `full_alert` 或某条主动消息就断言失败。

---

## 9. 联调结束后如何正确收口

如果已经完成验证，且暂时不需要继续监控，建议直接：

```powershell
python .\main.py service-stop
```

这样可以避免：
- 后台检测线程继续运行
- 残留服务进程影响下次联调
- 误把上一轮状态带到下一轮测试

---

## 10. QQ 接入的几个实战提醒

### 先看上下文，再看消息
先确认：
- `channel=qqbot`
- `target=qqbot:c2c:...`
- `account=default`

再去看消息是否成功发回。

### 单人单渠道起步时，先把 fallback 也配到同一个 QQ 会话
这是这轮补出来的一条很实用的经验。

如果你当前主要就是在一个 QQ 私聊里联调：
- `openclaw-context` 绑定当前 QQ 聊天
- `routes.qqbot.target` 指向同一个 QQ 聊天
- `fallback.channel/target` 也先指向同一个 QQ 聊天

这样即使活动上下文 TTL 过期，危险通知仍会回到同一个对话，不容易误判成“系统没发”。

### 人工测试隔得久，记得重绑上下文或调大 TTL
如果你是先配置好，然后过很久才真人触发：
- 先重新执行一次 `openclaw-context`
- 或把 `context_ttl_seconds` 调大到更适合你的联调节奏

### 先看 `last_dispatch`，再猜发送路径
最有用的位置是：
- `status.notification_channel.last_dispatch`

重点看：
- `ok`
- `status`
- `route_source`
- `effective_path`
- `primary_path`

如果 `route_source` 从 `active_context` 变成 `fallback`，不一定是故障；也可能只是活动上下文过期后，兜底路由正常接管。

### 别把“收到消息”误当“最终已锁定”
收到消息只说明**有事件被投递**。
最终是否已锁定，必须再回读 `status`。

---

## 11. Feishu 接入的几个实战提醒

### 先看健康状态，再让用户触发真人测试
如果 `camera_available=false` 或 `is_protecting=false`，就别让用户白演一轮。

### 先看上下文，再看消息
先确认：
- `channel=feishu`
- `target=user:ou_xxx` 或正确群目标
- `account=default`

再去看消息是否成功发回。

### 先看 `last_dispatch`，再猜发送路径
最有用的位置是：
- `status.notification_channel.last_dispatch`

重点看：
- `ok`
- `status`
- `effective_path`
- `primary_path`

### 别把“收到消息”误当“最终已锁定”
收到消息只说明**有事件被投递**。
最终是否已锁定，必须再回读 `status`。
