# ClawCamKeeper-skill

## 项目规划

- 目的是构建一个类似监控预警的功能，本地摄像头实时监控，检测到 领导/老板 来时，通过 openclaw传送预警，并切换窗口为一个可选的程序窗口。
- 目的是建立一个在 openclaw 上使用的 skill ，符合 claude skill 标准。
- 使用 scripts 文件夹管理 skill 提供的核心逻辑与代码，cli 结构。
- 使用例如 `config.yaml` 集中管理项目重要配置信息。
- 提供提美观的 webui 调试控制台（ios风格），可选声音报警，监控区 人物框绘制、配置信息动态修改/加载 等，默认启用，use gsap skill。
- openclaw 消息通道至少接入 feishu（@larksuite/openclaw-lark） 、QQBot（@sliverp/qqbot） 插件，可选启用。
- 技术栈： python 、MediaPipe ...

## todo core

1. 先实现核心逻辑架构与 webui调试界面，达成：摄像头-检测、渲染逻辑-webui报警 通路。
2. 实现到 feishu、qqbot 通路。
3. 构筑 skill 结构。