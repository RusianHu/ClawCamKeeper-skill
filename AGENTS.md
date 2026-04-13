# ClawCamKeeper Agents

## 一句话
本仓库是 **self-contained OpenClaw skill repository**：克隆到 skill 工作区后，当前目录本身就是项目根目录与运行载体。

## 目标
- 工位摸鱼防护，不泛化成安防平台。
- 报警成功 = **已切到安全态**。
- 本地优先；远程只发起控制与查询，不替代本地执行链。
- 需要时尽可能的了解openclaw ACP 的用法： https://docs.openclaw.ai/zh-CN/cli/acp 

## 边界
- 不做人脸/身份识别。
- 不做重度取证/长期图像留存。
- 不做自动恢复原窗口；恢复必须人工显式触发。
- 不让 OpenClaw / ACP 直接操作 Core 内部对象。
- 不让远程控制依赖打开 WebUI 页面链路。

## 当前 phase 结论
- Phase 0~5：已完成。
- 已完成：MVP 报警闭环、可靠性增强、OpenClaw 接入、轻量可观察性、风格化体验。
- 外部阻塞：[`openclaw agent --local`](SKILL.md) / chat 直通仍受 provider token / embedded model 鉴权状态影响，不影响本地 skill 仓库结构与 CLI/bridge 调用。

若缺这些，不要盲调命令，先定位到 skill 仓库根目录。

## 运行入口
- 服务启动：[`python .\main.py run`](cli/main.py:489)
- OpenClaw bridge：[`python .\main.py openclaw ...`](main.py:14)
- 优先包装脚本：[`scripts/invoke-clawcamkeeper-openclaw.ps1`](scripts/install_openclaw_skill.ps1:10)

## 自动化稳定边界
- **唯一稳定自动化边界 = CLI JSON**。
- WebUI 面向人工；自动化不要依赖页面点击。
- OpenClaw / ACP 默认经由 [`cli/openclaw_bridge.py`](cli/openclaw_bridge.py) → [`cli/main.py`](cli/main.py) → Core。
- 结果必须以机器可读字段为准：`ok / message / data / timings / source / state_snapshot`。

## 状态机
- 未武装
- 已武装
- 危险锁定

危险锁定语义：
- 已触发安全动作
- 保持静默锁定
- 只能显式 [`recover`](cli/openclaw_bridge.py:343)

## 远程能力面
- `status`
- `doctor`
- `arm`
- `disarm`
- `recover`
- `config-show`
- `set-safe-window`
- `events`
- `notifications`
- `action-test`

## 危险锁定期规则
允许：`status / doctor / events / notifications / config-show`
谨慎允许：`set-safe-window`
默认拒绝：隐式恢复、检测参数改写、大范围风险程序改写

## session 规则
[`session_policy`](core/engine.py:202) 已固定：`session-label` 只做会话隔离，不承载业务状态。
所有武装/锁定/恢复判断，必须回读 [`status`](cli/openclaw_bridge.py:280) 或 `state_snapshot`。

## 轻量观察能力
状态输出已包含：
- `monitoring_active`
- `last_action_result`
- `timeline`
- `remote_action_matrix`
- `notification_channel`
- `observability`
- `evidence_policy`

默认策略：**lightweight_no_forensics**，不做重度证据留存。

## 安装 / 更新到 OpenClaw workspace
若当前是开发仓库：
- 执行 [`scripts/install_openclaw_skill.ps1`](scripts/install_openclaw_skill.ps1)
- 该脚本会把**整个仓库**同步到 `~/.openclaw/workspace/skills/clawcamkeeper-openclaw`
- 这是完整项目副本，不是轻量 skill 壳

若当前已在 skill 工作区：
- 直接在当前目录安装依赖并运行
- 更新时重新同步整个仓库，不要只覆盖 [`SKILL.md`](SKILL.md)

## 最小工作流
1. 安装依赖：`python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`
2. 启动服务：`python .\main.py run`
3. 自检：`python .\main.py openclaw doctor`
4. 查询状态：`python .\main.py openclaw status`
5. 需要诊断动作链时：`python .\main.py openclaw action-test [--full-check]`

## 失败处理
- `service_unavailable`：本地服务未运行 / API 不可达
- `timeout`：调用超时；不要重复连发写操作
- `invalid_arguments`：参数错误；先修参
- 无 `state_snapshot` 的写操作失败 = **未确认生效**

## Agent 行为准则
- 先保命，再美化。
- 先读状态，再做写操作。
- 先本地 CLI 调通，再谈 openclaw ACP / chat。
- 报告先给结论，再给关键状态，再给耗时/错误。
- 若用户要求“安装这个 GitHub skill”，默认目标应是：把**整个仓库**放入 skill 工作区，而不是只复制 skill 子目录。
