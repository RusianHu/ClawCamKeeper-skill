# ClawCamKeeper-skill

一个运行在本地、由 OpenClaw 参与控制与通知的轻量 **工位摸鱼防护预警技能**。

项目目标不是识别“谁来了”，而是判断 **是否有人进入可见屏幕的危险空间**，并在必要时把当前环境切到安全态。

详细规划见 [`PLAN-CORE.md`](PLAN-CORE.md)。

## 功能概览

- 本地常驻监控与双阶段预警
- 危险成立后自动切换到安全窗口并最小化风险程序
- WebUI 状态面板、配置编辑、热加载
- CLI 控制与调试
- OpenClaw / bridge 机器可读适配层
- 通知队列、最近动作结果、时间线、远程动作矩阵

## 仓库结构

- [`main.py`](main.py) —— 项目入口
- [`cli/main.py`](cli/main.py) —— CLI 与本地服务入口
- [`cli/openclaw_bridge.py`](cli/openclaw_bridge.py) —— OpenClaw 机器可读桥接层
- [`core/`](core/__init__.py) —— 状态机、检测、动作链、配置热加载
- [`webui/app.py`](webui/app.py) —— FastAPI WebUI
- [`SKILL.md`](SKILL.md) —— OpenClaw skill 入口
- [`AGENTS.md`](AGENTS.md) —— 仓库内部约束与工作准则
- [`docs/NOTIFICATION-FLOW.md`](docs/NOTIFICATION-FLOW.md) —— 主动通知回推链路说明
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) —— 常见故障排查

## 运行环境

当前项目以 **Windows 11 + Python 3.10+** 为主要目标环境。

建议前置条件：

- 已安装 Python，并可直接使用 `python`
- 已安装 OpenClaw CLI，并已完成基础配置
- 具备可用摄像头
- 具备 Windows 桌面窗口控制能力

## 安装依赖

在仓库根目录执行：

```powershell
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

## 启动本地服务

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

## 安装 OpenClaw skill

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

- **QQBot 场景优先走直连 HTTP 发送**
- **OpenClaw CLI 消息发送是兜底路径**
- **ACP 不是危险告警主链路**，它更适合复杂会话工作继续流转

### 当前回推链路

1. 运行中的本地服务保存最近一次上下文：`session_key / session_label / channel / target / account`
2. 通过 `openclaw-context` 或 bridge 自动注册上下文
3. 危险事件进入通知队列后：
   - QQBot：优先直连 QQ API
   - 若直连失败：退回 OpenClaw CLI 消息发送
   - 其他渠道：默认通过 OpenClaw CLI 消息发送

### 联调建议顺序

1. 启动本地服务：`python .\main.py run`
2. 注册当前回推上下文：`python .\main.py openclaw-context --channel <channel> --target <target> --account <account>`
3. 检查上下文：`python .\main.py openclaw notification-context`
4. 武装：`python .\main.py openclaw arm`
5. 人工触发一次危险事件
6. 观察目标渠道是否收到主动提醒
7. 再查看 `notifications` / `events` / `status.notification_channel`

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
- WebUI 监听地址与端口
- OpenClaw 主动通知回推配置

## 首次安装后的配置指南

把 skill 分发到新环境后，通常需要先做一轮**静态配置**，但不要把**当前会话的 session 绑定信息**写死进配置文件。

### 适合写进 `settings.yaml` 的静态配置

这些属于长期稳定配置：

- `safe_window.primary` / `safe_window.backup`
- `risk_apps`
- `webui.host` / `webui.port`
- `openclaw.notifications.enabled`
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

OpenClaw 通知配置示例：

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
        account: ""
    fallback:
      channel: "qqbot"
      target: "qqbot:c2c:YOUR_TARGET"
      account: "default"
```

说明：

- `enabled=true` 后才会真正主动回推
- `routes` 用于静态补全渠道路由
- `fallback` 用于当前没有活动上下文时的兜底路由
- `session_key / session_label` 不建议写入配置文件，应由运行时 `openclaw-context` 注册
- 最近一次上下文与分发结果可从 `notification-context` / `status.notification_channel` 查看

## 常见问题

如果遇到以下情况，优先看排障文档：

- `service_unavailable` / `WinError 10061`
- 8765 端口被旧实例占用
- 通知上下文为空，导致发不回当前聊天
- `Weixin.exe` 不可用，看起来像动作链坏了

详见 [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)。

## 当前限制

- 项目当前主要面向 Windows
- OpenClaw `agent --local` / 某些 chat 直通能力仍受 provider 凭据与 token 状态影响
- 通知链虽然支持会话上下文，但并不等于 ACP 原生事件流
- 如果当前调用链没有提供 `channel / target`，则需要依赖配置中的 `routes` 或 `fallback`
- 如果使用同步脚本安装 skill，更新时应重新同步整个仓库，而不是只覆盖 `SKILL.md`
