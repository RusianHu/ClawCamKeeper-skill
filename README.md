# ClawCamKeeper-skill

一个运行在本地、由 OpenClaw 参与控制与通知的轻量 **工位摸鱼防护预警技能**。

项目目标不是识别“谁来了”，而是判断 **是否有人进入可见屏幕的危险空间**，并在必要时把当前环境切到安全态。

详细规划见 [`PLAN-CORE.md`](PLAN-CORE.md)。

## 功能概览

- 本地常驻监控与双阶段预警
- 危险成立后自动切换到安全窗口并最小化风险程序
- WebUI 状态面板、配置编辑、热加载
- CLI 控制与调试
- OpenClaw / ACP 薄适配层
- 通知队列、最近动作结果、时间线、远程动作矩阵

## 仓库结构

- [`main.py`](main.py) —— 项目入口
- [`cli/main.py`](cli/main.py) —— CLI 与本地服务入口
- [`cli/openclaw_bridge.py`](cli/openclaw_bridge.py) —— OpenClaw / ACP 机器可读桥接层
- [`core/`](core/__init__.py) —— 状态机、检测、动作链、配置热加载
- [`webui/app.py`](webui/app.py) —— FastAPI WebUI
- [`SKILL.md`](SKILL.md) —— 仓库根 skill 入口，供 OpenClaw / agent 直接识别整个仓库
- [`skills/clawcamkeeper-openclaw/SKILL.md`](skills/clawcamkeeper-openclaw/SKILL.md) —— 兼容保留的子目录技能说明
- [`scripts/install_openclaw_skill.ps1`](scripts/install_openclaw_skill.ps1) —— 把整个仓库同步到 OpenClaw workspace skill 目录
- [`scripts/test_openclaw_bridge.ps1`](scripts/test_openclaw_bridge.ps1) —— bridge 回归脚本

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

- [`http://127.0.0.1:8765`](http://127.0.0.1:8765)

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

同步后的 skill 目录将包含：

- 仓库根 [`SKILL.md`](SKILL.md)
- [`main.py`](main.py)
- [`requirements.txt`](requirements.txt)
- [`cli/`](cli/__init__.py)
- [`core/`](core/__init__.py)
- [`webui/`](webui/app.py)
- [`config/`](config/settings.yaml)
- [`scripts/`](scripts/install_openclaw_skill.ps1)
- 以及安装时生成的 `skill-install.json`

也就是说，**OpenClaw skill 工作区里的这个目录本身，就是完整可运行的项目副本**，而不是依赖外部开发目录的轻量壳。

## 安装后如何稳定调用

安装完成后，可以直接把 OpenClaw skill 目录视为项目根目录。

典型调用方式：

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\你的用户名\.openclaw\workspace\skills\clawcamkeeper-openclaw\scripts\invoke-clawcamkeeper-openclaw.ps1 status
powershell -ExecutionPolicy Bypass -File C:\Users\你的用户名\.openclaw\workspace\skills\clawcamkeeper-openclaw\scripts\invoke-clawcamkeeper-openclaw.ps1 doctor
powershell -ExecutionPolicy Bypass -File C:\Users\你的用户名\.openclaw\workspace\skills\clawcamkeeper-openclaw\scripts\invoke-clawcamkeeper-openclaw.ps1 notifications --since-id 0 --limit 10
powershell -ExecutionPolicy Bypass -File C:\Users\你的用户名\.openclaw\workspace\skills\clawcamkeeper-openclaw\scripts\invoke-clawcamkeeper-openclaw.ps1 arm
```

或者直接进入该目录后执行：

```powershell
python .\main.py run
python .\main.py openclaw status
```

## OpenClaw 远程能力面

当前已对接的首批能力：

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

### 主动通知回推

当前版本不仅支持轮询 [`/api/notifications`](webui/app.py:95)，还支持在关键事件发生后主动调用 [`openclaw message send`](AGENTS.md) 进行回推。

关键点：

- 运行中的本地服务会保存最近一次 OpenClaw 会话上下文：`session_key / session_label / channel / target / account`
- OpenClaw bridge 每次远程调用时，会先尝试通过 [`openclaw-context`](cli/main.py:667) 把当前上下文注册进本地服务
- 当 [`danger_lock`](core/engine.py:699)、[`action_failure`](core/engine.py:710)、[`recover`](core/engine.py:518)、[`arm`](core/engine.py:481)、[`disarm`](core/engine.py:507)、[`camera_failure`](core/engine.py:607) 等事件进入 [`_queue_notification()`](core/engine.py:573) 后，会继续进入主动分发逻辑
- 分发命令最终落到 [`subprocess.run()`](core/engine.py:519) 调用的 `openclaw message send --channel ... --target ... --message ...`

建议联调顺序：

1. 启动本地服务：[`python .\main.py run`](cli/main.py:489)
2. 通过 OpenClaw bridge 发起一次远程查询或控制，让 bridge 自动注册当前上下文
3. 用 [`python .\main.py openclaw notification-context`](main.py:14) 检查当前上下文是否已写入
4. 触发预警 / 锁定后，检查 [`status.notification_channel.last_dispatch`](core/engine.py:1254) 或 [`python .\main.py openclaw notifications`](main.py:14) 中的 `delivery.dispatch`

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

新增的 OpenClaw 通知配置骨架：

```yaml
openclaw:
  notifications:
    enabled: false
    command: openclaw
    timeout_seconds: 8
    context_ttl_seconds: 900
    message_prefix: "[ClawCamKeeper]"
    routes:
      qqbot:
        target: ""
        account: ""
      feishu:
        target: ""
        account: ""
    fallback:
      channel: ""
      target: ""
      account: ""
```

说明：

- `enabled=true` 后才会真正主动调用 [`openclaw message send`](AGENTS.md)
- `routes.qqbot` / `routes.feishu` 用于“当前上下文只有 channel，没有 target/account”时做静态补全
- `fallback` 用于当前没有活动 OpenClaw 上下文时的兜底渠道
- 最近一次上下文注册结果和最近一次分发结果可从 [`status`](cli/openclaw_bridge.py:385) 的 `notification_channel.context / last_dispatch` 读取

## 给 GitHub 分发用户的关键说明

如果别人从 GitHub clone 了这个仓库，并希望让自己的 OpenClaw 使用它，有两种路径：

### 路径 A：直接把整个仓库作为 skill 仓库放进 OpenClaw skill 目录

适用于支持“用 GitHub 仓库直接安装 skill”的 agent / skill 管理器。

要求：

1. 安装后的目录根包含 [`SKILL.md`](SKILL.md)
2. 安装后的目录中同时包含完整项目代码（[`main.py`](main.py)、[`cli/`](cli/__init__.py)、[`core/`](core/__init__.py)、[`webui/`](webui/app.py)、[`config/`](config/settings.yaml) 等）
3. 在该目录下安装依赖并启动服务

### 路径 B：先 clone 到任意目录，再同步到 OpenClaw workspace

1. clone 仓库到本地任意目录
2. 安装 Python 依赖
3. 运行 [`scripts/install_openclaw_skill.ps1`](scripts/install_openclaw_skill.ps1) 把**整个仓库**同步进 OpenClaw workspace
4. 进入 `C:\Users\你的用户名\.openclaw\workspace\skills\clawcamkeeper-openclaw`
5. 运行 [`python .\main.py run`](cli/main.py:489) 启动本地服务
6. 之后 OpenClaw 可直接把该 skill 目录当作项目根目录使用

## 当前限制

- 项目当前主要面向 Windows
- OpenClaw `agent --local` 的 embedded 模型调用是否成功，仍依赖用户本机 provider 凭据与 token 状态
- 主动通知回推当前基于 [`openclaw message send`](AGENTS.md) 的 channel/target 路由，不是直接写入 ACP 原生事件流
- 如果 OpenClaw 当前调用链没有提供 `channel / target`，则需要依赖 [`config/settings.yaml`](config/settings.yaml) 中的 `routes` 或 `fallback` 补全
- 若使用的是“同步脚本安装”而不是“直接 Git 克隆到 skill 目录”，更新流程应重新执行 [`scripts/install_openclaw_skill.ps1`](scripts/install_openclaw_skill.ps1) 覆盖 skill 副本

## 当前验证状态

已完成一次本地模拟冒烟验证：

- 通过 monkeypatch [`core.engine.subprocess.run`](core/engine.py:519) 模拟 `openclaw message send`
- 注册 `qqbot` 上下文后，触发 [`danger_lock`](core/engine.py:699) 事件
- 验证结果确认通知已进入 [`get_notifications()`](core/engine.py:624) 队列，且 `delivery.dispatch` 中记录了主动发送参数与成功状态

## 许可证

见 [`LICENSE`](LICENSE)。
