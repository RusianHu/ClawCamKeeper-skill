# ClawCamKeeper-skill

## 产品定位

一个运行在本地、由 openclaw 参与控制与通知的轻量的 **工位摸鱼防护预警技能**。
目标不是识别“谁来了”，而是判断 **是否有人进入可见屏幕的危险空间**，并在必要时把当前环境切到安全态。

>本文档信息要求保持 **精简、高信息熵**

## 产品原则

- **只做摸鱼防护**：不泛化成安防平台。
- **先把报警做成立**：先确保能救场，再优化聪明程度。
- **双阶段策略**：先无感预备，再自动切窗。
- **本地优先**：真正触发时，以本地监控与动作链为主；openclaw 负责控制入口与消息转发。
- **轻量不黑盒**：状态少、反馈清楚、长期可常驻。
- **用户可控**：安全窗口、风险程序、武装状态都应可明确控制。

## 明确边界

- [ ] 不做身份识别 / 人脸识别
- [ ] 不做通用安防、门禁、巡检平台
- [ ] 不做重度图像留存与取证系统
- [ ] 不做复杂行为分析优先于报警闭环
- [ ] 不做自动恢复原窗口，默认手动恢复

## 参考技术栈（可根据实际需要调整）

- Python：主语言
- MediaPipe：首版视觉检测核心之一
- OpenCV：摄像头采集、基础图像处理、调试渲染
- Windows 桌面控制能力：窗口切换、最小化、焦点切换
- 检测器策略：首版快速可用优先，后续可替换或扩展视觉检测方案
- WebUI：状态展示、配置编辑、配置文件保存、分类热加载、调试控制台；人工使用，确保关键功能可被 CLI 覆盖（避免 openclaw 打开浏览器走这个控制链路）
- CLI：供 openclaw 调用、调试、自检、状态查询
- openclaw skill：远程控制与消息转发入口
- GSAP：WebUI 风格化与动态反馈

## 工具形态

- **Core**：本地常驻监控核心，负责状态机、双阶段判断、报警动作链
- **CLI**：主要控制入口，便于自动化调用、调试、自检、状态查询
- **WebUI**：负责状态展示、配置编辑、配置文件落盘、分类热加载、校准、轻量调试
- **OpenClaw**：作为外部控制、会话承载与消息转发层，不替代本地执行主链路
- **约束**：CLI 负责控制与查询，持续监控由 Core 常驻承担
- **约束**：OpenClaw 默认通过 CLI 的机器可读接口调用，不直接操作 Core 内部对象
- **约束**：远程控制不依赖打开 WebUI 浏览器链路
- **约束**：关键状态与检查命令应支持机器可读输出

## CLI 建议范围

- arm / disarm / recover
- status / doctor
- events
- config-show / config-set
- set-safe-window / set-risk-apps（可视为 config-set 的场景化别名）

## 核心状态

- **未武装**：程序运行中，但当前不承担防护承诺
- **已武装**：摄像头在线、安全窗口可切、本地动作链可用
- **危险锁定**：已触发安全动作，保持静默锁定，等待手动恢复

## 核心闭环

1. 用户武装系统
2. 系统进入监控
3. 有人进入屏幕可见风险空间
4. 第一阶段：无感预备
5. 条件满足（继续靠近 / 停留超过阈值）
6. 第二阶段：切到安全态
7. 联动最小化风险程序、切换输入焦点
8. 进入危险锁定
9. 用户手动恢复

## 成功标准

- [ ] 用户能明确知道当前是否已武装
- [ ] 报警成功的定义是：**已切到安全态**
- [ ] 系统状态一眼可懂，不依赖复杂解释
- [ ] 安全窗口支持用户指定，且具备主备双目标
- [ ] 危险触发后可静默锁定，不反复打扰
- [ ] openclaw 至少可完成远程武装/解除、查看状态、改目标窗口中的核心能力
- [ ] 整体保持轻量工具感，而不是臃肿平台

---

# Phase 0｜产品骨架定稿

## 目标
统一产品边界、状态语言、报警闭环与 phase 划分，避免后续做散。

## TODO
- [x] 固化一句话定位与口号：openclaw 预警技能
- [x] 固化三态模型：未武装 / 已武装 / 危险锁定
- [x] 固化“报警成功”定义：已切到安全态
- [x] 固化双阶段策略：无感预备 → 自动切窗
- [x] 固化恢复规则：仅手动恢复
- [x] 固化边界：不做身份识别
- [x] 固化工具形态：Core + CLI + WebUI + openclaw

---

# Phase 1｜MVP 报警闭环

## 目标
先打通最小可用闭环：**武装 → 监控 → 预警 → 切安全态 → 锁定**。

## TODO
- [x] 提供一键武装 / 解除武装
- [x] 明确区分“程序运行中”与“已武装”
- [x] 提供轻确认，保证用户知道武装已生效
- [x] 实时监控本地摄像头画面
- [x] 围绕“屏幕可见风险空间”建立基础触发逻辑
- [x] 实现第一阶段无感预备
- [x] 实现第二阶段自动切到安全窗口
- [x] 切换输入焦点到安全窗口
- [x] 最小化用户标记的风险程序
- [x] 触发后进入静默危险锁定
- [x] 支持用户手动恢复
- [x] 提供最小 CLI 控制能力：arm / disarm / status / doctor

## 执行拆解

### Core
- [x] 建立最小状态机：未武装 / 已武装 / 危险锁定
- [x] 建立双阶段触发流：无感预备 → 自动切窗
- [x] 建立危险锁定与手动恢复机制

### 检测链路
- [x] 打通摄像头读取与实时处理
- [x] 实现“有人进入屏幕可见风险空间”的基础判定
- [x] 实现继续靠近 / 停留阈值的升级条件

### 动作链路
- [x] 实现主安全窗口切换
- [x] 实现输入焦点切换
- [x] 实现风险程序最小化
- [x] 验证动作触发后的锁定行为

### CLI
- [x] 打通 arm / disarm
- [x] 打通 status / doctor
- [x] 预留 events 输出入口
- [x] 为关键命令预留机器可读输出

### WebUI
- [x] 提供最小状态展示面
- [x] 提供最小武装状态可视反馈
- [x] 提供基础调试画面与检测结果展示
- [x] 支持在 WebUI 中编辑大部分核心配置
- [x] 支持从 WebUI 保存配置文件
- [x] 支持对配置变更执行分类热加载反馈

## 验收
- [x] 坐下后可快速进入防护状态
- [x] 有人靠近时能真正完成一次保命动作
- [x] 触发后不会自动切回导致二次翻车

---

# Phase 2｜安全态可靠性增强

## 目标
让“切到安全态”更可靠，不把用户暴露在假成功里。

## TODO
- [x] 支持主安全窗口 + 备选安全窗口
- [x] 当主目标不可用时自动切换到备选目标
- [x] 明确提示安全窗口失效
- [x] 明确提示本地动作链失效
- [x] 明确提示摄像头不可用导致无法武装
- [x] 支持用户手动指定风险程序列表
- [x] 收敛“当前前台高风险内容”与“用户指定高风险程序”的行为优先级

## 验收
- [x] “已武装”状态具备真实含义，不是形式状态
- [x] 安全态失败时，用户能立刻知道保护能力下降

---

# Phase 3｜openclaw 接入

## 目标
把本地防护工具变成真正的 OpenClaw skill，并建立稳定的远程控制入口；远程负责发起控制意图，本地仍是实际检测、触发与执行的第一责任链路。

## 核心原则
- OpenClaw 是外部控制层，不是本地 Core 的替代执行器
- 远程调用默认经由 CLI 的 JSON 模式完成，不直接操作 Core 内部对象
- WebUI 面向人工可视操作；CLI 才是远程自动化的稳定边界
- OpenClaw 负责会话、消息、调用编排与结果转发，不负责替代本地动作链
- 所有自动化结果必须保留机器可读字段，不能退化为仅人类可读文本
- 任何远程能力都不得破坏“本地优先、危险锁定需手动恢复”的总原则
- OpenClaw 侧故障默认只影响远程入口，不影响本地 Core 常驻能力

## 首批远程能力面
- status
- doctor
- arm
- disarm
- recover
- config-show
- set-safe-window（主 / 备安全窗口）
- events（只读、限量）
- notifications（轻量通知队列，只读、限量）

## TODO
- [x] 固化 OpenClaw skill 结构、工作目录与配置注入方式
- [x] 打通 OpenClaw → 本地 CLI(JSON) → Core 的最小调用链
- [x] 固化首批远程命令白名单：status / doctor / arm / disarm / recover / config-show / set-safe-window / events / notifications
- [x] 统一机器可读返回结构：ok / message / data / timings / source / state_snapshot
- [x] 统一错误语义：参数非法 / 当前状态不允许 / 本地服务未运行 / CLI 调用失败 / 动作失败 / 调用超时
- [x] 接入消息转发通道（成功 / 失败 / 锁定 / 需人工恢复）
- [x] 约束 OpenClaw 不直接调用 WebUI 页面链路，不直接操作 Core 内部对象

## 执行拆解

### 3A｜控制边界与命令面定稿
- [x] 为每个远程动作建立 CLI 命令映射表
- [x] 明确所有远程动作默认使用 `--json`
- [x] 明确每个动作的输入参数面：`safe_window.primary` / `safe_window.backup` / `limit` / `full_check`
- [x] 明确每个动作的最小成功字段与失败字段
- [x] 明确 openclaw session / session-label 只承担会话隔离，不承载业务状态

### 3B｜最小调用链打通
- [x] 可远程调用 status
- [x] 可远程调用 doctor
- [x] 可远程调用 arm / disarm / recover
- [x] 可远程调用 config-show
- [x] 可远程调用 set-safe-window，并验证配置已落盘且本地即时生效
- [x] 可远程调用 events（只读）
- [x] 调用结果保留 CLI 与服务端 timings 字段

### 3C｜锁定期与状态规则收敛
- [x] 形成三态下的远程动作允许矩阵：未武装 / 已武装 / 危险锁定
- [x] 明确 danger_locked 期间允许：status / doctor / events / config-show / notifications
- [x] 明确 danger_locked 期间谨慎允许：修改主 / 备安全窗口
- [x] 明确 danger_locked 期间默认拒绝：风险程序批量改写 / 检测参数改写 / 隐式恢复类操作
- [x] 恢复必须显式调用 recover，不允许通过 config-set 间接解锁
- [x] 远程配置变更不得破坏本地优先与手动恢复语义

### 3D｜消息转发与事件语义
- [x] 定义需转发的关键事件：arm 成功 / disarm 成功 / 进入危险锁定 / 动作链失败 / 需人工恢复
- [x] 明确哪些事件即时推送，哪些事件仅供查询
- [x] 为转发消息保留状态摘要与关键 timings
- [x] 为重复告警设计节流 / 去重规则，避免刷屏
- [x] 保证消息转发失败不影响本地动作链执行

### 3E｜调试与联调
- [x] 先用本地 CLI(JSON) 完成单机调试，再接 OpenClaw 远程入口
- [x] 用 status / doctor / action-test 建立联调前置检查清单
- [x] 分开验证 quick 与 full 两条检查路径
- [x] 验证本地服务未运行、Gateway 不可达、远程调用超时、CLI 非零退出码的处理
- [x] 保留最小可复现日志：请求参数、CLI stdout/stderr、退出码、关键 timings
- [x] 为远程修改安全窗口后即时生效建立回归检查步骤

### 3F｜验收、回退与上线约束
- [x] 提供 Phase 3 验收脚本或手工清单
- [x] 提供“关闭 openclaw 接入后仍可纯本地运行”的回退路径
- [x] 约束 OpenClaw 侧故障不得阻塞本地 Core 常驻
- [x] 明确 Gateway / OpenClaw 侧故障时的降级提示与人工操作建议
- [x] 把 Phase 3 完成条件映射到后续 Phase 4 的事件与观察能力

## 远程动作映射草案
- `status` → `clawcamkeeper status --json`
- `doctor` → `clawcamkeeper doctor --json`
- `arm` → `clawcamkeeper arm --json`
- `disarm` → `clawcamkeeper disarm --json`
- `recover` → `clawcamkeeper recover --json`
- `config-show` → `clawcamkeeper config-show --json`
- `set-safe-window` → `clawcamkeeper config-set --safe-window <primary> --backup-window <backup> --json`
- `events` → `clawcamkeeper events --limit <n> --json`
- `notifications` → `clawcamkeeper notifications --since-id <id> --limit <n> --json`
- 联调动作测试 → `clawcamkeeper action-test --json [--full-check]`

## 危险锁定期规则
- 允许：status / doctor / events / notifications / config-show
- 谨慎允许：修改主 / 备安全窗口
- 默认拒绝：大范围风险程序改写、检测参数改写、任何可能改变锁定语义的远程操作
- 恢复必须显式调用 recover，不允许通过配置变更隐式恢复
- disarm 是否允许在危险锁定期间执行，必须在接入前明确并固定为单一语义

## 联调前置检查
- [x] 本地 `run` 服务已启动且稳定运行
- [x] `status --json` 返回结构稳定
- [x] `doctor --json` 可区分摄像头 / 安全窗口 / 动作链状态
- [x] `action-test --json` 的 quick 模式可快速确认切窗链路
- [x] `action-test --json --full-check` 可用于完整故障诊断
- [x] 配置文件路径、日志路径、openclaw skill 工作目录已统一

## 当前接入现状
- [x] 已新增本地桥接命令：`python main.py openclaw ...`
- [x] 已新增 workspace skill：`clawcamkeeper-openclaw`
- [x] 已新增安装脚本：`scripts/install_openclaw_skill.ps1`
- [x] 已新增回归脚本：`scripts/test_openclaw_bridge.ps1`
- [x] 已通过回归脚本验证：`status / doctor / events / config-show / arm / action-test / disarm / status`
- [x] 已把 `notifications` 纳入 bridge / CLI / WebUI 三层入口，并补入回归脚本
- [x] 已额外验证 `set-safe-window --primary Weixin.exe --backup calc.exe` 返回 `ok=true`，且 `data.config_set` / `data.config_reload` / `state_snapshot` 一致
- [x] 已额外验证 `recover` 在非 `danger_locked` 状态下会显式拒绝，并返回可供上游判断的 `state_snapshot`
- [x] 已通过 FastAPI TestClient 验证 `danger_locked -> recover` 正向恢复路径、`/api/notifications` 输出，以及 Phase 4/5 所需核心字段与页面结构
- [ ] `openclaw agent --local` 直通 chat 指令验证仍受当前 embedded model 鉴权阻塞（401 invalid access token or token expired）

## 验收
- [x] OpenClaw 可作为外部控制入口使用
- [x] OpenClaw 调用链以 CLI(JSON) 为唯一稳定自动化边界
- [x] 远程 `status / doctor / arm / disarm / config-show / set-safe-window / events / notifications` 全部跑通
- [x] `recover` 已验证显式恢复语义与非锁定态拒绝返回
- [x] `danger_locked -> recover` 成功路径已在本地 API / 状态机层专项验证通过
- [x] 本地仍是实际触发与执行的第一责任链路
- [x] OpenClaw 侧故障不会破坏本地纯 CLI / WebUI 能力
- [x] 危险锁定期间的远程行为符合白名单，不会绕过手动恢复语义

---

# Phase 4｜轻量可观察性

## 目标
增强信任感，但保持轻量，不做复杂取证系统。

## TODO
- [x] 展示当前是否在监控 / 是否已武装 / 是否已锁定
- [x] 提供轻量事件列表
- [x] 记录关键动作结果：是否成功切窗、是否最小化风险程序
- [x] 提供最小必要的触发时间线
- [x] 明确显示最近一次动作执行结果
- [x] 保持默认不保存重度图像证据

## 验收
- [x] 用户不会陷入“不知道是否在监控”的黑盒感
- [x] 用户能快速看懂最近一次报警是否真正成立

---

# Phase 5｜风格化体验增强

## 目标
在不损伤轻量性的前提下，加入项目辨识度与仪式感。

## TODO
- [x] 做出有辨识度的调试 / 控制台风格
- [x] 引入轻度游戏化反馈，如警戒感、危险态氛围
- [x] 保持日常常驻时极简、低存在感
- [x] 把风格主要集中在调试台与武装仪式中，而非日常打扰

## 验收
- [x] 项目看起来像一个真正的 openclaw 预警技能，而不是普通 demo
- [x] 风格增强不干扰首要目标：报警成立

---

# 当前进度快照

## 当前已完成
- [x] Phase 0 产品骨架定稿
- [x] Phase 1 MVP 报警闭环
- [x] Phase 2 安全态可靠性增强
- [x] Phase 3 openclaw 接入
- [x] Phase 4 轻量可观察性
- [x] Phase 5 风格化体验增强
- [x] WebUI 配置控制台增强（配置编辑 / 保存配置文件 / 分类热加载）

## 当前阶段结论
- [x] 已达到“WebUI 可见可控的本地报警逻辑阶段”
- [x] WebUI 已不再只是状态展示与开关控制，而是具备本地配置编辑、落盘与热加载反馈能力
- [x] 已完成 OpenClaw 薄适配层、本地 skill 落位、桥接自测与主命令联调
- [x] 已补齐轻量通知、动作结果、时间线、远程动作矩阵、会话规则与轻量证据策略展示
- [x] 已形成“本地战术控制台”风格的 WebUI，并保持武装/危险态集中增强而非日常打扰
- [ ] openclaw chat 指令直通验证仍受当前 embedded model 鉴权阻塞

# 当前优先级

## 必须先做
- [x] Phase 1 MVP 报警闭环
- [x] Phase 2 安全态可靠性增强
- [x] Phase 3 openclaw 接入（代码、文档、回归脚本与本地 API 层验证已完成，agent/chat 直通仍受外部鉴权阻塞）
- [x] Phase 4 轻量可观察性
- [x] Phase 5 风格化体验增强

## 之后再做
- [ ] 待 embedded model 鉴权恢复后，补做 `openclaw agent --local` / chat 直通专项验证

## 性能观测与调试规范

### 目标
- Web API、CLI、Core 必须形成统一的性能观测链路，不允许只有日志而没有机器可读字段。
- 任何“感觉慢”的反馈，都必须能拆解到：请求层 / 可用性探测 / 切窗动作 / 最小化动作 / 检测链路 / 配置热加载 中的具体一段。
- 默认调试路径优先服务“快速确认能不能救场”，完整诊断作为按需能力存在。

### Quick / Full 测试策略
- 默认测试链路走 **quick** 模式：
  - 跳过完整摄像头探测
  - 重点验证切窗动作是否可在百毫秒级完成
- 显式指定 `full_check=true` 时走 **full** 模式：
  - 执行完整可用性刷新
  - 用于诊断摄像头、动作链、安全窗口是否整体可用
- Quick 模式用于“现在点测试切换会不会及时救场”；Full 模式用于“为什么系统整体不健康”。

### CLI 全链路调试要求
- CLI 状态类命令至少输出：
  - CLI 总耗时
  - 服务端路由耗时
  - 业务动作耗时（若存在）
- `run` 命令至少输出：
  - 初始化耗时
  - 服务启动总耗时
  - 关闭耗时
- CLI 必须保留 JSON 输出模式，确保所有性能字段可被自动化消费。
- 应提供可直接触发动作链测试的 CLI 命令，避免每次都依赖 WebUI 手工点击。

## 始终检查
- [ ] 是否仍然保持“轻量、直接、真能救场”
- [ ] 是否仍然聚焦工位摸鱼防护，而没有做散
- [ ] 是否仍然让用户清楚知道：现在到底有没有在保护我
