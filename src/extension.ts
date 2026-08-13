import * as vscode from "vscode"
import * as fs from "fs"
import * as path from "path"
import * as os from "os"
import { exec as execCb, spawn } from "child_process"
import { promisify } from "util"
import { BiomniBridge } from "./biomniBridge"
import type { ByokConfig, EnvStatus, ExtToWebview, MirrorConfig, WebviewToExt } from "./types"

const execAsync = promisify(execCb)
const ENV_FILE = "/data/biomni/.env"
const BIOMNI_DIR = "/data/biomni"

let currentPanel: vscode.WebviewPanel | null = null
let currentBridge: BiomniBridge | null = null

export function activate(context: vscode.ExtensionContext): void {
  console.log("[biomni-chat] activated")

  const disposable = vscode.commands.registerCommand("biomniChat.open", () => {
    openChatPanel(context)
  })
  context.subscriptions.push(disposable)
}

export function deactivate(): void {
  currentBridge?.dispose()
}

function pythonPath(): string {
  const config = vscode.workspace.getConfiguration("biomniChat")
  return config.get<string>("pythonPath") || "/data/biomni/envs/biomni_e1/bin/python"
}

function parseEnv(content: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of content.split("\n")) {
    const s = line.trim()
    if (!s || s.startsWith("#")) continue
    const eq = s.indexOf("=")
    if (eq > 0) out[s.slice(0, eq).trim()] = s.slice(eq + 1).trim().replace(/^['"]|['"]$/g, "")
  }
  return out
}

function updateEnvVar(content: string, key: string, value: string): string {
  const lines = content.split("\n")
  let found = false
  const next = lines.map((l) => {
    const s = l.trim()
    if (s.startsWith("#")) return l
    if (s.startsWith(key + "=")) {
      found = true
      return `${key}=${value}`
    }
    return l
  })
  if (!found) next.push(`${key}=${value}`)
  return next.join("\n")
}

function detectProvider(baseUrl: string): string {
  if (baseUrl.includes("deepseek.com")) return "deepseek"
  if (baseUrl.includes("openai.com")) return "openai"
  if (baseUrl.includes("anthropic")) return "anthropic"
  return "custom"
}

function readMirrors(): MirrorConfig {
  const condaPath = path.join(os.homedir(), ".condarc")
  let conda = "https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge"
  if (fs.existsSync(condaPath)) {
    const c = fs.readFileSync(condaPath, "utf-8")
    const m = c.match(/https:[^\s]+/)
    if (m) conda = m[0]
  }
  return {
    conda,
    pip: "https://pypi.tuna.tsinghua.edu.cn/simple",
    cran: "https://mirrors.tuna.tsinghua.edu.cn/CRAN/",
  }
}

function saveMirrors(m: MirrorConfig): void {
  fs.writeFileSync(
    path.join(os.homedir(), ".condarc"),
    `channels:\n  - ${m.conda}\n  - conda-forge\n  - bioconda\nshow_channel_urls: true\n`,
  )
  execAsync(`${pythonPath()} -m pip config set global.index-url ${m.pip}`).catch(() => {})
  fs.writeFileSync(path.join(os.homedir(), ".Rprofile"), `options(repos = c(CRAN = "${m.cran}"))\n`)
}

async function readSettings(panel: vscode.WebviewPanel): Promise<void> {
  let byok: ByokConfig = { provider: "custom", baseUrl: "", model: "", hasKey: false }
  try {
    if (fs.existsSync(ENV_FILE)) {
      const env = parseEnv(fs.readFileSync(ENV_FILE, "utf-8"))
      const baseUrl = env.BIOMNI_CUSTOM_BASE_URL || ""
      const key = env.BIOMNI_CUSTOM_API_KEY || ""
      byok = {
        provider: detectProvider(baseUrl),
        baseUrl,
        model: env.BIOMNI_LLM || "",
        hasKey: !!(key && !key.includes("在此填写")),
      }
    }
  } catch {}
  panel.webview.postMessage({ type: "settings_data", byok, mirrors: readMirrors() })
}

/** 检测扩展主机平台与远程类型（本插件仅支持 Remote-SSH 连接 Linux 服务器） */
function detectRemoteStatus(): { ok: boolean; mode: string } {
  const remote = vscode.env.remoteName // "ssh-remote" | "wsl" | undefined(本机)
  const platform = process.platform
  const isLinuxHost = platform === "linux"
  let mode = `扩展主机平台: ${platform}`
  if (isLinuxHost) {
    mode += remote ? `；远程类型: ${remote}` : "（本机 Linux 直接运行）"
  }
  return { ok: isLinuxHost, mode }
}

async function checkEnv(panel: vscode.WebviewPanel): Promise<void> {
  const remote = detectRemoteStatus()
  const env: EnvStatus = {
    python: fs.existsSync(pythonPath()),
    biomni: false,
    tools: [],
    dataLake: fs.existsSync(path.join(BIOMNI_DIR, "biomni_data", "data_lake")),
    dataLakeSize: "",
    envFile: fs.existsSync(ENV_FILE),
    remoteOk: remote.ok,
    remoteMode: remote.mode,
  }
  if (env.python) {
    try {
      await execAsync(`${pythonPath()} -c "import biomni"`)
      env.biomni = true
    } catch {}
  }
  const toolNames = ["blastn", "samtools", "bowtie2", "bwa", "bedtools", "fastqc", "trimmomatic", "mafft"]
  for (const t of toolNames) {
    try {
      await execAsync(`which ${t}`)
      env.tools.push(t)
    } catch {}
  }
  if (env.dataLake) {
    try {
      env.dataLakeSize = (await execAsync(`du -sh ${path.join(BIOMNI_DIR, "biomni_data", "data_lake")}`)).stdout.trim()
    } catch {}
  }
  panel.webview.postMessage({ type: "settings_data", env })
}

async function testConnection(panel: vscode.WebviewPanel): Promise<void> {
  panel.webview.postMessage({ type: "settings_test_result", ok: false, message: "正在测试连接..." })
  try {
    const { stdout } = await execAsync(`${pythonPath()} ${BIOMNI_DIR}/scripts/test_api.py`, { timeout: 60000 })
    const ok = stdout.includes("✅") || stdout.includes("连接成功")
    const message = stdout.split("\n").filter((l) => l.trim()).slice(-8).join("\n")
    panel.webview.postMessage({ type: "settings_test_result", ok, message })
  } catch (e) {
    panel.webview.postMessage({
      type: "settings_test_result",
      ok: false,
      message: `测试失败: ${String((e as Error).message).slice(0, 500)}`,
    })
  }
}

function saveByok(byok: ByokConfig, apiKey: string): void {
  let content = fs.existsSync(ENV_FILE) ? fs.readFileSync(ENV_FILE, "utf-8") : ""
  content = updateEnvVar(content, "BIOMNI_SOURCE", "Custom")
  content = updateEnvVar(content, "BIOMNI_CUSTOM_BASE_URL", byok.baseUrl)
  content = updateEnvVar(content, "BIOMNI_CUSTOM_API_KEY", apiKey)
  content = updateEnvVar(content, "BIOMNI_LLM", byok.model)
  fs.writeFileSync(ENV_FILE, content)
}

/** 一键部署（L0+L1）：无 biomni_e1 环境则跑完整脚本；有则快速升级 biomni */
async function runDeploy(panel: vscode.WebviewPanel): Promise<void> {
  const progress = (line: string) => panel.webview.postMessage({ type: "deploy_progress", line, source: "deploy" })
  // SSH/平台检测：非 Linux 主机拒绝部署
  const remote = detectRemoteStatus()
  if (!remote.ok) {
    panel.webview.postMessage({
      type: "deploy_result",
      source: "deploy",
      ok: false,
      message: `❌ 已拒绝部署：当前扩展主机不是 Linux（${process.platform}）。\n请使用 Remote-SSH 连接到 Linux 服务器后再部署 Biomni 环境。`,
    })
    return
  }
  progress(remote.mode)

  const py = pythonPath()
  if (!fs.existsSync(py)) {
    // 新用户：完整部署 L0+L1（创建 conda 环境 + 生物工具 + R）
    const script = "/data/biomni/scripts/install_biomni_e1.sh"
    if (!fs.existsSync(script)) {
      panel.webview.postMessage({
        type: "deploy_result",
        source: "deploy",
        ok: false,
        message: `❌ 未找到部署脚本: ${script}`,
      })
      return
    }
    progress("▶ 未检测到 biomni_e1 环境，开始完整部署（L0 核心 + L1 生信工具，首次可能需要 30-60 分钟）...")
    const child = spawn("bash", [script], { env: { ...process.env } })
    child.stdout.on("data", (d: Buffer) => progress(d.toString()))
    child.stderr.on("data", (d: Buffer) => progress(d.toString()))
    child.on("error", (e) =>
      panel.webview.postMessage({ type: "deploy_result", source: "deploy", ok: false, message: `无法启动部署: ${e.message}` }),
    )
    child.on("close", async (code) => {
      panel.webview.postMessage({
        type: "deploy_result",
        source: "deploy",
        ok: code === 0,
        message: code === 0
          ? "✅ L0+L1 部署完成（biomni 核心 + 生信工具 + 生物库 + R）。\n如需数据湖(L2)，点下方「下载数据湖」。"
          : `部署退出码: ${code}，请查看上方输出。`,
      })
      void checkEnv(panel)
    })
    return
  }

  // 已有环境：快速升级 biomni 及运行时依赖（L0）
  progress("▶ biomni_e1 环境已存在，快速升级 biomni 及运行时依赖...")
  const mirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
  const child = spawn(
    py,
    [
      "-m",
      "pip",
      "install",
      "--upgrade",
      "biomni",
      "pandas",
      "openai",
      "langchain-openai",
      "langchain-community",
      "-i",
      mirror,
    ],
    { env: { ...process.env, PYTHONUNBUFFERED: "1" } },
  )
  child.stdout.on("data", (d: Buffer) => progress(d.toString()))
  child.stderr.on("data", (d: Buffer) => progress(d.toString()))
  child.on("error", (e) =>
    panel.webview.postMessage({ type: "deploy_result", source: "deploy", ok: false, message: `无法启动安装: ${e.message}` }),
  )
  child.on("close", async (code) => {
    if (code !== 0) {
      panel.webview.postMessage({ type: "deploy_result", source: "deploy", ok: false, message: `安装退出码: ${code}` })
      return
    }
    try {
      await execAsync(`${py} -c "from biomni.agent import A1"`, { timeout: 30000 })
      panel.webview.postMessage({
        type: "deploy_result",
        source: "deploy",
        ok: true,
        message: "✅ biomni 升级完成，A1 导入验证通过。",
      })
    } catch {
      panel.webview.postMessage({ type: "deploy_result", source: "deploy", ok: false, message: "⚠ A1 导入验证失败，请查看上方输出。" })
    }
    void checkEnv(panel)
  })
}

/** 下载数据湖（L2，~11GB，流式进度） */
async function runDeployL2(panel: vscode.WebviewPanel): Promise<void> {
  const progress = (line: string) => panel.webview.postMessage({ type: "deploy_progress", line, source: "l2" })
  const remote = detectRemoteStatus()
  if (!remote.ok) {
    panel.webview.postMessage({
      type: "deploy_result",
      source: "l2",
      ok: false,
      message: `❌ 已拒绝：当前扩展主机不是 Linux（${process.platform}）。`,
    })
    return
  }
  const py = pythonPath()
  if (!fs.existsSync(py)) {
    panel.webview.postMessage({
      type: "deploy_result",
      source: "l2",
      ok: false,
      message: "❌ 请先完成 L0/L1 部署（需要 biomni_e1 环境）再下载数据湖。",
    })
    return
  }
  const script = "/data/biomni/scripts/download_data_lake.py"
  if (!fs.existsSync(script)) {
    panel.webview.postMessage({ type: "deploy_result", source: "l2", ok: false, message: `❌ 未找到脚本: ${script}` })
    return
  }
  progress(remote.mode)
  progress("▶ 开始下载数据湖（L2，约 11GB，请耐心等待；中断后已下载文件保留，可续传）...")
  const child = spawn(py, [script], { env: { ...process.env } })
  child.stdout.on("data", (d: Buffer) => progress(d.toString()))
  child.stderr.on("data", (d: Buffer) => progress(d.toString()))
  child.on("error", (e) =>
    panel.webview.postMessage({ type: "deploy_result", source: "l2", ok: false, message: `无法启动下载: ${e.message}` }),
  )
  child.on("close", (code) => {
    panel.webview.postMessage({
      type: "deploy_result",
      source: "l2",
      ok: code === 0,
      message: code === 0 ? "✅ 数据湖（L2）下载完成。" : `下载退出码: ${code}，可重新运行续传。`,
    })
    void checkEnv(panel)
  })
}

function openChatPanel(context: vscode.ExtensionContext): void {
  if (currentPanel) {
    currentPanel.reveal(vscode.ViewColumn.One)
    return
  }

  const panel = vscode.window.createWebviewPanel(
    "biomniChat",
    "Biomni Chat",
    vscode.ViewColumn.One,
    {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [vscode.Uri.joinPath(context.extensionUri, "out", "webview")],
    },
  )

  const config = vscode.workspace.getConfiguration("biomniChat")
  const pythonPath = config.get<string>("pythonPath") || "/data/biomni/envs/biomni_e1/bin/python"
  const bridgeScript = config.get<string>("bridgeScript") || "/data/biomni-chat/python/biomni_bridge.py"

  panel.webview.html = getWebviewHtml(panel, context.extensionUri)

  // 启动 Biomni 桥（服务器侧 python 进程）
  const bridge = new BiomniBridge(pythonPath, bridgeScript, (msg) => {
    panel.webview.postMessage(msg satisfies ExtToWebview)
  })
  bridge.start()

  panel.webview.onDidReceiveMessage((msg: WebviewToExt) => {
    switch (msg.type) {
      case "send":
        if (msg.prompt) {
          // 解析 prompt 中的 @文件 提及，把文件内容注入上下文
          injectMentionedFiles(panel, msg.prompt, (augmented) => {
            bridge.chat(augmented, msg.mode || "ask")
          })
        }
        break
      case "clarify_answer":
        bridge.clarifyAnswer(msg.questionId ?? 0, msg.option || "", msg.answer)
        break
      case "plan_confirm":
        bridge.planConfirm()
        break
      case "plan_edit":
        if (msg.content) bridge.planEdit(msg.content)
        break
      case "cancel":
        bridge.cancel()
        break
      case "new_conversation":
        bridge.newConversation()
        break
      case "export_report":
        exportReport(msg.content ?? "")
        break
      case "mention_open":
        listWorkspaceFiles(panel)
        break
      case "generate_title":
        bridge.generateTitle(msg.prompt || "")
        break
      case "settings_load":
        void readSettings(panel)
        break
      case "settings_save":
        if (msg.byok) {
          saveByok(msg.byok, msg.byok.apiKey || "")
          vscode.window.showInformationMessage("BYOK 配置已保存。重启 Biomni Chat 后生效。")
        }
        break
      case "settings_test":
        void testConnection(panel)
        break
      case "mirror_save":
        if (msg.mirrors) {
          saveMirrors(msg.mirrors)
          vscode.window.showInformationMessage("镜像源已保存（conda/pip/CRAN）。")
        }
        break
      case "env_check":
        void checkEnv(panel)
        break
      case "deploy":
        void runDeploy(panel)
        break
      case "deploy_l2":
        void runDeployL2(panel)
        break
      case "ready":
        // webview 就绪，无需额外操作
        break
    }
  })

  panel.onDidDispose(() => {
    bridge.dispose()
    currentPanel = null
    currentBridge = null
  })

  currentPanel = panel
  currentBridge = bridge
}

// 二进制/生成目录黑名单，@文件列表时跳过
const BINARY_EXT = /\.(png|jpe?g|gif|webp|ico|pdf|zip|tar|gz|rds|rdata|h5|h5ad|bam|bai|vcf|idx|pyc|so|dll|exe|woff2?|ttf|o|a)$/i

/** 解析 prompt 中的 @文件 提及，读取内容后拼接到 prompt */
function injectMentionedFiles(
  panel: vscode.WebviewPanel,
  prompt: string,
  cb: (augmented: string) => void,
): void {
  const root = vscode.workspace.workspaceFolders?.[0]?.uri
  if (!root) {
    cb(prompt)
    return
  }
  const mentions = [...prompt.matchAll(/@([^\s，。；：]+)/g)].map((m) => m[1])
  if (mentions.length === 0) {
    cb(prompt)
    return
  }
  const parts: string[] = [prompt]
  let pending = mentions.length
  for (const rel of mentions) {
    const uri = vscode.Uri.joinPath(root, rel)
    fs.promises
      .readFile(uri.fsPath, "utf-8")
      .then((content) => {
        const head = content.slice(0, 20000)
        parts.push(`\n\n[用户引用的文件: ${rel}]\n\`\`\`\n${head}\n\`\`\``)
      })
      .catch(() => {
        parts.push(`\n\n[用户引用的文件: ${rel}]\n(无法读取该文件)`)
      })
      .finally(() => {
        pending -= 1
        if (pending === 0) cb(parts.join(""))
      })
  }
}

/** 列出工作区文本文件（相对路径），发给 webview 供 @ 提及 */
async function listWorkspaceFiles(panel: vscode.WebviewPanel): Promise<void> {
  const root = vscode.workspace.workspaceFolders?.[0]
  if (!root) {
    panel.webview.postMessage({ type: "mention_files", files: [] })
    return
  }
  const uris = await vscode.workspace.findFiles(
    "**/*",
    "**/{node_modules,.git,out,dist,build,__pycache__,.venv,env,envs}/**",
    800,
  )
  const files = uris
    .map((u) => vscode.workspace.asRelativePath(u, false))
    .filter((p) => !BINARY_EXT.test(p))
  panel.webview.postMessage({ type: "mention_files", files })
}

/** 把报告内容导出为 Markdown 文件（服务器项目目录 biomni_reports/） */
async function exportReport(content: string): Promise<void> {
  try {
    const root = vscode.workspace.workspaceFolders?.[0]?.uri
    const dir = root
      ? vscode.Uri.joinPath(root, "biomni_reports")
      : vscode.Uri.file(require("path").join(require("os").homedir(), ".biomni", "reports"))
    await vscode.workspace.fs.createDirectory(dir)
    const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19)
    const file = vscode.Uri.joinPath(dir, `report_${ts}.md`)
    await vscode.workspace.fs.writeFile(file, Buffer.from(content, "utf-8"))
    vscode.window.showInformationMessage(`报告已导出: ${file.fsPath}`)
  } catch (e) {
    vscode.window.showErrorMessage(`报告导出失败: ${String(e)}`)
  }
}

function getWebviewHtml(panel: vscode.WebviewPanel, extensionUri: vscode.Uri): string {
  const indexPath = vscode.Uri.joinPath(extensionUri, "out", "webview", "index.html")
  let html = fs.readFileSync(indexPath.fsPath, "utf-8")

  // 将 vite 产物的相对资源路径转换为 webview 可访问的 URI
  const webviewRoot = panel.webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, "out", "webview"),
  )
  html = html.replace(/src="\.\//g, `src="${webviewRoot}/`)
  html = html.replace(/href="\.\//g, `href="${webviewRoot}/`)

  // CSP: 只允许来自 webview 自身的脚本
  const csp = [
    "default-src 'none'",
    `style-src ${panel.webview.cspSource} 'unsafe-inline'`,
    `script-src ${panel.webview.cspSource}`,
    `img-src ${panel.webview.cspSource} data:`,
    "font-src 'none'",
  ].join("; ")
  html = html.replace("<head>", `<head><meta http-equiv="Content-Security-Policy" content="${csp}">`)

  return html
}
