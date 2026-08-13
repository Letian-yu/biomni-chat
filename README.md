# Biomni Chat

> **Unofficial community plugin.** Biomni is a trademark of Stanford SNAP Lab.

Biomni AI agent chat for VS Code, running on your Linux bioinformatics server.

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
