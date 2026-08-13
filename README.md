# Biomni Chat

> **Unofficial community plugin.** Biomni is a trademark of Stanford SNAP Lab.

Biomni AI agent chat for VS Code, running on your Linux bioinformatics server.

## Features
- **Plan / Act 双模式**：Plan 自动识别任务类型——分析型走完整规划流程（调研→澄清→计划→确认→执行）；调研/咨询型直接执行
- **澄清引擎**：选择题（带 LLM 推荐与利弊权衡）→ 可编辑研究计划 → 确认后执行
- **执行过程时间线**：思维链 + 工具调用交错展示，状态颜色编码（思考/工具/代码/完成/错误）
- **多会话管理**：侧边栏会话列表、LLM 自动命名、本地持久化（重启保留）、上下文隔离
- **消息操作**：复制 / 重试 / 编辑重发 / 还原检查点 / 删除
- **设置页**：BYOK 模型配置（DeepSeek / OpenAI / Anthropic / 自定义）、镜像源、环境检测、一键部署（L0+L1）与数据湖下载（L2）
- **报告导出**：会话 / 研究计划 / 执行报告导出为 Markdown
- **@ 文件提及**：把工作区文件内容注入给 agent

## Installation
1. 从 [Releases](../../releases) 下载最新的 `biomni-chat-x.y.z.vsix`
2. VS Code → 扩展 → `...` → 从 VSIX 安装
3. 通过 **Remote-SSH** 连接你的 Linux 服务器后打开

> ⚠️ 本插件仅支持 Linux 服务器（通过 Remote-SSH 使用）。在本地 Windows/macOS 直接运行时，设置页会警告并拒绝部署环境。

## Quick Start
1. 打开 Biomni Chat（命令面板 → `Biomni Chat: Open Chat`）
2. 首次使用：进入「设置」→ 配置模型（BYOK）→ 测试连接 → 环境检测 → 一键部署（L0+L1，约 30-60 分钟）
3. 开始对话：**Plan** 模式发研究任务（自动规划），**Act** 模式直接执行

## Platform Support
| Platform | Architecture | Support |
|----------|--------------|---------|
| Linux | x86_64 | ✅ Full support |
| macOS | Intel / Apple Silicon | ⚠️ Partial (core agent only, limited) |
| Windows | any | ❌ Not supported |

## Prerequisites
- VS Code with Remote-SSH to a Linux bioinformatics server
- A deployed Biomni environment (`biomni_e1` conda env) on the server
- BYOK LLM configuration in an `.env` file (e.g. `/data/biomni/.env`)

## 开发原则（铁律）

> **每次改动或构建新的代码，必须先对齐 Roo Code 源码，确认其对应实现方式，再动手实现。**
> 本项目是一比一复刻 Roo Code 的 UI 与交互效果，禁止"凭感觉"实现 Roo 已有特性的功能。

- **参考源码**：Roo Code（`RooCodeInc/Roo-Code`）已克隆到 `/data/roo-code/Roo-Code-main/`
- **比对入口**：
  - 前端组件：`webview-ui/src/components/chat/`（TaskHeader、ChatRow、ReasoningBlock、ProgressIndicator、CommandExecution 等）
  - 核心逻辑：`src/core/`（工具、Task、系统提示词）、`src/core/prompts/`（各模式 system prompt）
- **已对齐过的特性**（开发时复用这些结论）：
  - 工作状态卡 + 耗时：ROO `ReasoningBlock`（`isStreaming` 控制计时）+ `ProgressIndicator`（旋转环）
  - 澄清交互：ROO `ask_followup_question` 工具（agent 自主提问、选项建议）
  - 模式切换：ROO `switch_mode` 工具（plan 批准后切换模式执行）
  - Plan 流程：ROO plan mode 系统提示（信息收集 → 澄清 → 计划 → 批准 → 切换模式实施）
- **流程**：先查 Roo 对应实现 → 理解其设计 → 一比一复刻或在其基础上改进 → 再写代码

## Development (P0)
```bash
cd /data/biomni-chat
npm install
npm run compile      # build extension + webview
# Press F5 in VS Code to launch Extension Development Host
# Command Palette -> "Biomni Chat: Open Chat"
```

## Architecture
```
[VS Code Webview (React)]  --JSON--  [Extension (TS)]  --stdio JSON-lines--
                                              |
                                  [biomni_bridge.py] -- A1 agent on server
```
