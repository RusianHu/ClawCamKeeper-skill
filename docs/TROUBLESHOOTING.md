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

## 2. 8765 端口被旧实例占用

### 症状
- 新服务日志里出现 bind 失败
- 看起来启动成功过，但实际响应的是旧版本
- 修改代码后效果不生效

### 处理思路
先找旧进程，再清掉，再重启：

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
python .\main.py openclaw-context --channel qqbot --target qqbot:c2c:YOUR_TARGET --account default
python .\main.py openclaw notification-context
```

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

1. `notification-context`：看上下文是否已注册
2. `notifications`：看事件是否进入通知队列
3. `events`：看完整时间线
4. `status`：看当前状态和通知通道摘要

如果是 QQBot：默认应优先怀疑
- 直连 HTTP 发送是否失败
- 失败后 CLI 兜底是否成功

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

## 8. 什么时候该谈 ACP，什么时候不该

### 适合 ACP
- 要把复杂 agent 工作送回某个会话继续做
- 要有持续对话或继续执行语义

### 不适合 ACP
- 危险告警主链路
- 要求低延迟、低依赖、明确渠道直达的提醒

危险告警优先考虑 **直连渠道发送**，不是默认走 ACP。
