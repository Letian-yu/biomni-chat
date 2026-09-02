# Biomni Chat

> **Unofficial community plugin.** Biomni is a trademark of Stanford SNAP Lab.

**Biomni Chat** lets you use [Biomni](https://github.com/snap-stanford/Biomni) (Stanford SNAP Lab's bioinformatics AI agent) through a clean chat UI inside VS Code, running on your Linux bioinformatics server.

输入研究任务 → 自动调研与澄清 → 生成研究计划 → 确认后由 A1 自动执行分析 → 交付物与报告直接呈现。数据不出服务器。

## Features

- **Plan / Act 双模式**：Plan 自动识别任务类型——分析型走完整规划流程（调研 → 澄清 → 计划 → 确认 → 执行）；调研/咨询型直接执行
- **澄清引擎**：带 LLM 推荐与利弊权衡的选择题 → 可编辑研究计划 → 确认后执行
- **执行过程时间线**：思维链 + 工具调用交错展示，状态颜色编码（思考/工具/代码/完成/错误）
- **多会话管理**：侧边栏会话列表、LLM 自动命名、本地持久化、上下文隔离
- **消息操作**：复制 / 重试 / 编辑重发 / 还原检查点 / 删除
- **一键部署**：设置页自动部署 E1 环境（`biomni_e1`）+ biomni 包，并可下载 Data lake
- **报告导出**：会话 / 研究计划 / 执行报告导出为 Markdown
- **@ 文件提及**：把工作区文件内容注入给 agent

## Installation

### 从 VSIX / Marketplace 安装
1. 下载最新的 `biomni-chat-x.y.z.vsix`（本仓库 **Releases**，或从 VS Code Marketplace 搜索 "Biomni Chat"）
2. VS Code → 扩展面板 → `...` → **从 VSIX 安装...**（选择下载的 `.vsix`）
3. 通过 **Remote-SSH** 连接到你的 **Linux** 服务器后即可使用

> ⚠️ 本插件仅在 **Linux 服务器**（Remote-SSH）上完整支持。在本地 Windows / macOS 直接运行时，设置页的环境部署会被禁用。

### 从源码运行（开发者）
```bash
git clone <repo-url>
cd biomni-chat
npm install
npm run compile      # 构建 extension + webview
# VS Code 中按 F5 启动 Extension Development Host
```

## Quick Start

1. **打开**：点击左侧活动栏的 **Biomni** 图标（或命令面板 → `Biomni Chat: Open Chat`）
2. **首次使用**：进入「设置」→ 配置模型（BYOK：DeepSeek / OpenAI / Anthropic / 自定义）→ 测试连接 → **一键部署**（E1 环境 + biomni 包，约 30-60 分钟）→ 按需下载 Data lake
3. **开始对话**：
   - **Plan 模式**：发研究任务（如 "对 GSE176078 乳腺癌单细胞数据做差异分析并挖掘关键靶点"）→ 自动调研、澄清、生成计划 → 你确认后执行
   - **Act 模式**：直接执行
4. 视图可拖到**右侧辅助侧边栏**，与 Copilot / Cline 并列使用

## Requirements

- VS Code 1.90+，通过 **Remote-SSH** 连接 **Linux x86_64** 服务器
- 服务器有 conda 即可——插件设置页可**自动创建** `biomni_e1` 环境并安装 biomni 包（无需手动部署）
- BYOK LLM：在设置页配置 API Key（DeepSeek / OpenAI / Anthropic / 自定义，兼容 OpenAI 协议）

## Platform Support

| Platform | Architecture | Support |
|----------|--------------|---------|
| Linux | x86_64 | ✅ Full |
| macOS | Intel / Apple Silicon | ⚠️ Partial |
| Windows | any | ❌ Not supported |

## How it works

```
[VS Code 侧边栏 Webview (React)] ←JSON→ [扩展主进程 (TS)] ←stdio→ [biomni_bridge.py]
                                                                          ↓
                                                       [服务器上的 biomni (A1) agent]
```

## Privacy

- API Key 只保存在服务器上的 `.env` 文件
- 数据分析过程全部在服务器本地完成，数据**不出服务器**

## License

[Apache-2.0](LICENSE)
