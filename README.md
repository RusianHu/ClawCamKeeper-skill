# ClawCamKeeper-skill

一个运行在本地、由 OpenClaw 参与控制与通知的轻量 **工位摸鱼防护预警技能**。

当有 **人形生物** 进入可见摄像头的 **危险空间** 时，把当前屏幕环境切到安全态，避免暴露你的小隐私状态。

## 功能概览

- 本地常驻监控与双阶段预警
- 危险成立后自动切换到安全窗口并最小化风险程序
- 危险动作成功后进入危险锁定，避免反复触发
- WebUI 状态面板、配置编辑、热加载
- CLI 控制与调试
- OpenClaw / bridge 机器可读适配层
- 通知队列、最近动作结果、时间线、远程动作矩阵

## 一键部署（推荐）

直接和你的龙虾 openclaw 说：

```text
为你自己安装这个 skill ： https://github.com/RusianHu/ClawCamKeeper-skill 
```

剩下的就不用管了，交给龙虾，剩下的是调试时看的，龙虾自己会看 [SKILL.md](SKILL.md) 。

## 配置建议

适合长期写入 `config/settings.yaml` 的包括：

- 主/备安全窗口：`safe_window.primary` / `safe_window.backup`
- 风险程序列表：`risk_apps`
- 检测阈值与帧数：`detection.*`
- WebUI 端口：`webui.port`
- WebUI 主机与局域网开关：`webui.host` / `webui.allow_lan`（默认仅本地回环，开启后可显式允许局域网访问）
- OpenClaw 默认通知路由：`openclaw.notifications.*`

其中：
- `routes / fallback` 负责长期默认发到哪儿
- `session_key / session_label` 不属于静态配置，不应手写进配置文件
- 当前聊天绑定应通过 `python .\main.py openclaw-context ...` 运行时注册

## 运行环境

当前项目以 **Windows 11 + Python 3.10+** 为主要目标环境。

建议前置条件：

- 已安装 Python，并可直接使用 `python`
- 已安装 OpenClaw CLI，并已完成基础配置
- 具备可用摄像头
- 具备 Windows 桌面窗口控制能力

## 安装依赖

在部署本 skill 的根目录执行：

```powershell
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

## 手动启动本地服务

在仓库根目录执行：

```powershell
python .\main.py run
```

默认会：

- 启动监控核心
- 启动 WebUI
- 使用 [`config/settings.yaml`](config/settings.yaml) 作为默认配置

WebUI 默认地址：

- <http://127.0.0.1:8765>

## 常用本地 CLI

在仓库根目录执行：

```powershell
python .\main.py status --json
python .\main.py doctor --json
python .\main.py arm --json
python .\main.py disarm --json
python .\main.py recover --json
python .\main.py events --limit 10 --json
python .\main.py notifications --since-id 0 --limit 10 --json
python .\main.py action-test --json
```

## 手动同步到 OpenClaw skill

如果你希望 OpenClaw 在自己的 workspace 中直接拥有这个项目的**完整代码副本**，请在**当前仓库根目录**执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_openclaw_skill.ps1
```

这个脚本会把**整个仓库**同步到：

- `C:\Users\你的用户名\.openclaw\workspace\skills\clawcamkeeper-openclaw`

也就是说，OpenClaw skill 工作区里的这个目录本身，就是完整可运行的项目副本，而不是只复制一个轻量 skill 壳。

## OpenClaw 远程能力面

当前已对接的能力：

- `status`
- `doctor`
- `arm`
- `disarm`
- `recover`
- `config-show`
- `set-safe-window`
- `events`
- `notifications`
- `notification-context`
- `action-test`

OpenClaw 适配命令入口见 [`cli/openclaw_bridge.py`](cli/openclaw_bridge.py)。

## 主动通知回推

### 设计结论

当前版本同时支持：

- 轮询 `/api/notifications`
- 关键事件发生后的主动回推

但要注意：

- **Feishu 已实测可走直连 HTTP 后备发送**
- **QQBot 场景优先走直连 HTTP 发送**
- **OpenClaw CLI 消息发送是兜底路径**
- **最终业务状态要以 `status` / `state_snapshot` 为准，不以单条通知文案为准**

### 当前回推链路

1. 运行中的本地服务保存最近一次上下文：`session_key / session_label / channel / target / account`
2. 通过 `openclaw-context` 或 bridge 自动注册上下文
3. 危险事件进入通知队列后：
   - QQBot：优先直连 QQ API
   - Feishu：优先走 Feishu 直连发送能力
   - 若渠道直发失败：退回 OpenClaw CLI 消息发送
   - 其他渠道：默认通过 OpenClaw CLI 消息发送

### 已验证联调结果

#### QQ（2026-04-16 收束）

截至 2026-04-16，围绕 QQBot 当前会话接入，又补齐并确认了这几条关键经验：

- 当前 QQ 私聊上下文注册成功
- `notification-test` 烟雾测试成功
- 真人摄像头触发联调成功
- 动作链执行成功，最终状态进入 `danger_locked`
- 即使当前活动上下文 TTL 过期，只要 `fallback` 仍指向同一个 QQ 会话，危险通知依旧能成功送达
- `status.notification_channel.last_dispatch` 可见成功状态，且 `route_source` 可能从 `active_context` 变为 `fallback`

这意味着以下链路已被实际验证：

1. 摄像头检测到人体进入
2. 状态从 `pre_alert` 推进到 `full_alert`
3. 动作链执行安全窗口切换与风险程序最小化
4. 系统最终进入 `danger_locked`
5. 当前 QQ 会话收到主动提醒
6. 当前上下文过期后，只要 fallback 配置正确，QQ 仍不会漏消息

#### Feishu（2026-04-16）

截至 2026-04-16，已完成：

- 当前 Feishu 私聊上下文自动绑定成功
- `notification-test` 烟雾测试成功
- `status.notification_channel.last_dispatch` 显示 `sent_via_direct_http`
- 真人摄像头触发联调通过
- 动作链执行成功并最终进入 `danger_locked`
- Feishu 会话收到主动提醒

这轮 Feishu 联调后的准确结论是：

1. Feishu 当前会话回推链已经打通
2. 最终状态判断必须回读 `status`
3. 当前实现已调整为优先把最终锁定态通知为 `danger_lock`
4. 但最终业务状态仍必须通过 `status` 回读确认

### QQ 接入最佳实践（2026-04-16 收束）

建议新用户第一次接入 QQBot 时，直接按这个顺序做：

1. 启动或重启到新代码：`python .\main.py service-restart --json`
2. 注册当前 QQ 会话上下文：
   - `python .\main.py openclaw-context --channel qqbot --target qqbot:c2c:YOUR_TARGET --account default`
3. 先看上下文：`python .\main.py openclaw-context-show`
4. 再做烟雾测试：`python .\main.py notification-test --message "qq smoke test" --json`
5. 回读 `status.notification_channel.last_dispatch`
6. 确认保护状态正常后再武装：`python .\main.py openclaw arm`
7. 人工触发危险事件
8. 最后回读：
   - `python .\main.py status --json`
   - `python .\main.py events --limit 10 --json`
   - `python .\main.py notifications --since-id 0 --limit 10`

重点检查：

- `channel=qqbot`
- `target=qqbot:c2c:...`
- `camera_available=true`
- `is_protecting=true`
- `last_dispatch.ok=true`
- 最终 `arm_state=danger_locked`
- 最终 `is_locked=true`

这轮补出来的关键经验有三条：

1. **当前聊天绑定要靠 `openclaw-context`，不要把 session 信息写死进配置文件**
2. **如果人工测试可能隔很久才触发，先重新注册一次上下文，或者把 `context_ttl_seconds` 配够**
3. **新用户单人单渠道部署时，建议把 `routes.qqbot` 和 `fallback` 都先指向同一个 QQ 会话**
   - 这样即使活动上下文过期，危险通知也会落到同一个聊天
   - 否则很容易出现“系统其实发了，但发去别的默认渠道”，现场会误判成没告警

### Feishu 联调最佳实践

建议按这个顺序走：

1. 启动服务：`python .\main.py run`
2. 健康检查：`python .\main.py doctor --json`
3. 查看上下文：`python .\main.py openclaw-context-show`
4. 做烟雾测试：`python .\main.py notification-test --message "smoke test" --json`
5. 回读 `status.notification_channel.last_dispatch`
6. 武装：`python .\main.py openclaw arm`
7. 人工触发危险事件
8. 回读：
   - `python .\main.py status --json`
   - `python .\main.py events --limit 10 --json`
   - `python .\main.py notifications --since-id 0 --limit 10`

重点检查：

- `camera_available=true`
- `is_protecting=true`
- `last_dispatch.ok=true`
- `last_dispatch.effective_path=feishu_direct_http`（或等效成功路径）
- 最终 `arm_state=danger_locked`
- 最终 `is_locked=true`

### 联调建议顺序

1. 启动本地服务：`python .\main.py run`
2. 注册当前回推上下文：`python .\main.py openclaw-context --channel <channel> --target <target> --account <account>`
3. 检查上下文：`python .\main.py openclaw-context-show`
4. 做一次烟雾测试：`python .\main.py notification-test --message "smoke test" --json`
5. 武装：`python .\main.py openclaw arm`
6. 人工触发一次危险事件
7. 观察目标渠道是否收到主动提醒
8. 再查看 `notifications` / `events` / `status.notification_channel`
9. 最后回读 `status`，确认最终业务状态
10. 联调结束后，如无需继续监控，执行 `python .\main.py service-stop` 收口

更完整说明见 [`docs/NOTIFICATION-FLOW.md`](docs/NOTIFICATION-FLOW.md)。

## 回归检查

在仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\test_openclaw_bridge.ps1
```

会依次检查：

- 状态
- 健康检查
- 事件
- 通知
- 配置读取
- 武装
- 动作测试
- 解除武装

## 配置文件

默认配置文件：[`config/settings.yaml`](config/settings.yaml)

主要配置包括：

- 摄像头参数
- 检测阈值与风险区
- 主 / 备安全窗口
- 风险程序列表
- WebUI 局域网开关、监听地址与端口（`webui.allow_lan / host / port`）
- OpenClaw 主动通知回推配置（含默认 routes / fallback / timeout / TTL / prefix）

## 首次安装后的配置指南

把 skill 分发到新环境后，通常需要先做一轮**静态配置**，但不要把**当前会话的 session 绑定信息**写死进配置文件。

### 适合写进 `settings.yaml` 的静态配置

这些属于长期稳定配置：

- `safe_window.primary` / `safe_window.backup`
- `risk_apps`
- `webui.host` / `webui.allow_lan` / `webui.port`
- `openclaw.notifications.enabled`
- `openclaw.notifications.command`
- `openclaw.notifications.timeout_seconds`
- `openclaw.notifications.context_ttl_seconds`
- `openclaw.notifications.message_prefix`
- `openclaw.notifications.routes.<channel>.target`
- `openclaw.notifications.routes.<channel>.account`
- `openclaw.notifications.fallback.channel / target / account`

### 不建议写死进 `settings.yaml` 的运行时上下文

这些更适合通过 `openclaw-context` 在运行时注册：

- `session_key`
- `session_label`
- 当前聊天临时使用的 `channel / target / account`

原因：这些值代表的是**当前会话语义**，不是长期稳定配置。如果把它们当成静态配置，会把“默认路由”和“当前会话绑定”混在一起。

### 推荐心智模型

- **默认通知去哪儿**：用 `routes` / `fallback` 配
- **当前这次聊天该回到哪儿**：用 `openclaw-context` 注册

OpenClaw 通知配置示例（以 **QQBot 单人私聊接入** 为推荐起步配置）：

```yaml
openclaw:
  notifications:
    enabled: true
    command: openclaw
    timeout_seconds: 8
    context_ttl_seconds: 900
    message_prefix: "[ClawCamKeeper]"
    routes:
      qqbot:
        target: "qqbot:c2c:YOUR_TARGET"
        account: "default"
      feishu:
        target: ""
        account: "default"
    fallback:
      channel: "qqbot"
      target: "qqbot:c2c:YOUR_TARGET"
      account: "default"
```

说明：

- `enabled=true` 后才会真正主动回推
- `routes` 用于静态补全渠道路由
- `fallback` 用于当前没有活动上下文时的兜底路由
- **新用户如果当前主要在 QQ 私聊联调，推荐先把 `fallback` 也指向同一个 QQ 会话**
- **如果人工测试常常会隔很久才做，记得重新注册 `openclaw-context`，或者适当调大 `context_ttl_seconds`**
- `session_key / session_label` 不建议写入配置文件，应由运行时 `openclaw-context` 注册
- 最近一次上下文与分发结果可从 `openclaw-context-show` / `status.notification_channel` 查看

## 常见问题

如果遇到以下情况，优先看排障文档：

- `service_unavailable` / `WinError 10061`
- 8765 端口被旧实例占用
- 通知上下文为空，导致发不回当前聊天
- `Weixin.exe` 不可用，看起来像动作链坏了
- Feishu 收到了提醒，但最终状态不确定

详见 [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)。

## 当前限制

- 项目当前主要面向 Windows
- 通知链虽然支持会话上下文，但不能只凭单条主动消息判断最终业务状态
- 如果当前调用链没有提供 `channel / target`，则需要依赖配置中的 `routes` 或 `fallback`
- 如果使用同步脚本安装 skill，更新时应重新同步整个仓库，而不是只覆盖 `SKILL.md`
