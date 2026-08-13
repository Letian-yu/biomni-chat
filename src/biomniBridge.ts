import { spawn, type ChildProcessWithoutNullStreams } from "child_process"
import type { BridgeRequest, BridgeResponse } from "./types"

/**
 * BiomniBridge: 负责在 VS Code 扩展侧与服务器上的 biomni_bridge.py 进程通信。
 * 使用 stdio + JSON lines 协议。
 * 注意: spawn 子进程时必须清理会串扰 Python 的环境变量 (PYTHONPATH / LD_LIBRARY_PATH 等)。
 */
export class BiomniBridge {
  private proc: ChildProcessWithoutNullStreams | null = null
  private buffer = ""

  constructor(
    private pythonPath: string,
    private scriptPath: string,
    private onMessage: (msg: BridgeResponse) => void,
  ) {}

  start(): void {
    const env = this.cleanEnv()
    this.proc = spawn(this.pythonPath, [this.scriptPath], { env })
    this.proc.stdout.on("data", (d: Buffer) => this.onStdout(d))
    this.proc.stderr.on("data", (d: Buffer) => {
      console.error(`[biomni-bridge stderr] ${d.toString().trimEnd()}`)
    })
    this.proc.on("exit", (code) => {
      console.log(`[biomni-bridge] process exited: ${code}`)
      this.proc = null
    })
    this.proc.on("error", (err) => {
      console.error(`[biomni-bridge] spawn error: ${err.message}`)
      this.onMessage({ type: "error", message: `无法启动 Biomni 桥进程: ${err.message}` })
    })
  }

  /** 清理可能污染子进程 Python 的环境变量，保证与用户已有 conda/python 环境隔离 */
  private cleanEnv(): NodeJS.ProcessEnv {
    const env: NodeJS.ProcessEnv = { ...process.env }
    delete env.PYTHONPATH
    delete env.PYTHONHOME
    delete env.CONDA_DEFAULT_ENV
    delete env.CONDA_PREFIX
    // LD_LIBRARY_PATH 可能指向 conda 的 lib，可能导致嵌入式/独立 python 加载错误的 C 库
    // 这里保留系统路径部分（如果存在则仅保留非 conda 路径，P0 简化为清空）
    env.PYTHONUNBUFFERED = "1"
    return env
  }

  private onStdout(data: Buffer): void {
    this.buffer += data.toString()
    const lines = this.buffer.split("\n")
    this.buffer = lines.pop() ?? ""
    for (const line of lines) {
      const trimmed = line.trim()
      if (!trimmed) continue
      try {
        const msg = JSON.parse(trimmed) as BridgeResponse
        this.onMessage(msg)
      } catch {
        // 非 JSON 输出（A1 的 print 漏网）忽略
      }
    }
  }

  send(req: BridgeRequest): void {
    this.proc?.stdin.write(JSON.stringify(req) + "\n")
  }

  chat(prompt: string, mode: "ask" | "plan" | "act" = "ask", history?: { role: string; content: string }[]): void {
    this.send({ type: "chat", prompt, mode, history })
  }

  /** 开启新对话：清空 A1 会话记忆，避免旧课题污染 */
  newConversation(): void {
    this.send({ type: "new_conversation" })
  }

  /** 用 LLM 为会话生成标题 */
  generateTitle(prompt: string): void {
    this.send({ type: "generate_title", prompt })
  }

  /** 回答桥端的澄清选择题 */
  clarifyAnswer(questionId: number, option: string, answer?: string): void {
    this.send({ type: "clarify_answer", questionId, option, answer })
  }

  /** 确认桥端生成的研究计划 */
  planConfirm(): void {
    this.send({ type: "plan_confirm" })
  }

  /** 将编辑后的计划内容发回桥端 */
  planEdit(content: string): void {
    this.send({ type: "plan_edit", content })
  }

  cancel(): void {
    this.send({ type: "cancel" })
  }

  dispose(): void {
    try {
      this.proc?.kill()
    } catch {
      /* noop */
    }
    this.proc = null
  }
}
