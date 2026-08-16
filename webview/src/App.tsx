import { useEffect, useRef, useState } from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { SettingsPanel } from "./SettingsPanel"

interface ClarificationItem {
  id: number
  question: string
  options: string[]
  optionDetails?: { text: string; pros_cons?: string; recommended?: boolean }[]
  selected?: string // 已选选项
  freeText?: string // "其他"自由填写
  answered: boolean
}

interface ChatMessage {
  role: "user" | "assistant"
  content: string // 正式回答（Markdown）
  taskStatus?: string // 当前工作状态
  taskHistory?: string[] // 工作状态历史
  taskExpanded?: boolean // 工作状态区是否展开
  startedAt?: number // 任务开始时间（耗时计算）
  finishedAt?: number // 任务结束时间（结束后耗时停止）
  clarifications?: ClarificationItem[] // 澄清选择题（待答/已答）
  planDraft?: string // 待确认的研究计划
  planRound?: number // 计划版本（多轮 plan 第 N 版）
  planStale?: boolean // 计划是否已被新版替代（多轮 plan 唯一可确认）
  planEditing?: boolean // 计划是否处于编辑态
  planConfirmed?: boolean // 计划是否已确认
  report?: string // 执行报告
  reportExpanded?: boolean // 报告是否展开
  reasoning?: string[] // 思维链（<think> 内容，对齐 Roo ReasoningBlock）
  reasoningExpanded?: boolean // 思维链是否展开（默认展开）
  timeline?: TimelineItem[] // 执行过程时间线（思考+工作交错）
  todos?: { id?: string; text: string; status: "pending" | "in_progress" | "completed"; source?: "plan" | "checklist" }[] // 执行步骤进度（对齐 Roo TodoList）
  deliverables?: { dir: string; items: { name: string; path: string; size: number }[] } // 交付物清单（A1 真实落盘文件 + 绝对路径）
}

type Mode = "plan" | "act"  // Ask 已砍掉

// 执行过程时间线条目（思考/工作交错，按时间顺序；work/code/done 为工作细分色）
interface TimelineItem {
  kind: "thinking" | "work" | "code" | "done"
  text: string
}

// 会话（对齐 Roo Task 历史管理）
interface ChatSession {
  id: string
  title: string
  messages: ChatMessage[]
  createdAt: number
}

// VS Code webview 提供的 API
declare function acquireVsCodeApi(): {
  postMessage: (msg: unknown) => void
  getState: () => any
  setState: (state: any) => void
}
const vscode = acquireVsCodeApi()

const MODES: { key: Mode; label: string; hint: string }[] = [
  // 对齐 Roo 核心模式：只保留 Plan（规划）与 Act（执行），Ask 砍掉
  { key: "plan", label: "Plan", hint: "分析任务规划" },
  { key: "act", label: "Act", hint: "完整执行" },
]

const OTHER_LABEL = "其他（自由填写）"

// 从计划文本提取执行步骤（编号/列表行，最多 10 个）
function extractTodos(plan?: string): { id?: string; text: string; status: "pending" | "in_progress" | "completed"; source?: "plan" | "checklist" }[] {
  if (!plan) return []
  const todos: { id?: string; text: string; status: "pending" | "in_progress" | "completed"; source?: "plan" | "checklist" }[] = []
  for (const line of plan.split("\n")) {
    const m = line.match(/^\s*(?:\d+[.、)]|[-*])\s+(.+)$/)
    if (m && m[1].trim().length > 3) {
      todos.push({ text: m[1].trim().slice(0, 60), status: "pending", source: "plan" })
      if (todos.length >= 10) break
    }
  }
  return todos
}

// 时间线容器：新条目到达时自动滚动到最新状态
function TimelineBody({ items }: { items: TimelineItem[] }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = ref.current
    if (el) el.scrollTop = el.scrollHeight
  }, [items.length])
  return (
    <div className="timeline-body" ref={ref} onClick={(e) => e.stopPropagation()}>
      {items.slice(-50).map((item, j) => (
        <div key={j} className={`timeline-item ${item.kind}`}>
          <span className="timeline-kind">
            {item.kind === "thinking"
              ? "思考"
              : item.kind === "code"
                ? "代码"
                : item.kind === "done"
                  ? "完成"
                  : "执行"}
          </span>
          <span className="timeline-text">{item.text}</span>
        </div>
      ))}
    </div>
  )
}

function fmtElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60}s`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

export default function App() {
  // 多会话管理：sessions 持久化仓库，messages 为当前会话消息
  const [sessions, setSessions] = useState<ChatSession[]>(() => {
    const saved = vscode.getState()?.sessions
    if (Array.isArray(saved) && saved.length > 0) return saved
    return [{ id: "s1", title: "新对话", messages: [], createdAt: Date.now() }]
  })
  const [currentSessionId, setCurrentSessionId] = useState(() => sessions[0]?.id ?? "s1")
  const [messages, setMessages] = useState<ChatMessage[]>(() => sessions[0]?.messages ?? [])
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [view, setView] = useState<"chat" | "settings">("chat")
  const [input, setInput] = useState("")
  const [busy, setBusy] = useState(false)
  const [mode, setMode] = useState<Mode>("plan")
  const [now, setNow] = useState(Date.now())
  const [mentionOpen, setMentionOpen] = useState(false)
  const [mentionFiles, setMentionFiles] = useState<string[]>([])
  const [mentionFilter, setMentionFilter] = useState("")
  const bottomRef = useRef<HTMLDivElement>(null)
  const mentionRef = useRef<HTMLDivElement>(null)
  // ⚠️ 消息监听 useEffect 依赖为 []，其闭包只能拿到首次渲染的 state；
  // 必须用 ref 保持最新值，否则 LLM 异步返回的 title 会写到错误的会话
  const currentSessionIdRef = useRef(currentSessionId)
  const stateMessagesRef = useRef(messages)
  const pendingTitleSessionRef = useRef<string | null>(null) // 标题归属会话（触发 generate_title 时记录）
  useEffect(() => {
    currentSessionIdRef.current = currentSessionId
    stateMessagesRef.current = messages
  }, [currentSessionId, messages])
  // 滚动控制（对齐 Roo ChatView isAtBottomRef）：仅在用户位于底部时自动滚动
  const messagesRef = useRef<HTMLDivElement>(null)
  const atBottomRef = useRef(true)
  const browsingRef = useRef(false)
  const [showScrollBtn, setShowScrollBtn] = useState(false)
  // 删除会话的二次确认（VS Code webview 禁用 window.confirm，改用内联确认，对齐 Roo DeleteTaskDialog）
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)
  // 用户消息编辑
  const [editingUserIndex, setEditingUserIndex] = useState<number | null>(null)
  const [editedUserText, setEditedUserText] = useState("")

  // 会话持久化（VS Code 自动保存 webview 状态，跨重启保留）
  useEffect(() => {
    vscode.setState({ sessions })
  }, [sessions])

  // 当前会话消息变化 → 同步回 sessions 并自动命名（标题=首条用户消息前30字）
  useEffect(() => {
    setSessions((prev) =>
      prev.map((s) =>
        s.id === currentSessionId
          ? { ...s, messages, title: s.title !== "新对话" ? s.title : autoTitle(messages) }
          : s,
      ),
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages, currentSessionId])

  const autoTitle = (msgs: ChatMessage[]): string => {
    const firstUser = msgs.find((m) => m.role === "user")
    const t = (firstUser?.content ?? "").trim().slice(0, 30)
    return t || "新对话"
  }

  const currentSession = sessions.find((s) => s.id === currentSessionId)

  // 多轮 plan：是否存在待确认的研究计划（用于输入框提示）
  const lastPendingPlan = [...messages].reverse().find((mm) => mm.planDraft && !mm.planConfirmed)?.planDraft

  // 把当前 messages 写回 sessions（切换/新建前调用）
  const saveCurrentSession = () => {
    setSessions((prev) =>
      prev.map((s) =>
        s.id === currentSessionId
          ? { ...s, messages, title: s.title !== "新对话" ? s.title : autoTitle(messages) }
          : s,
      ),
    )
  }

  const switchSession = (id: string) => {
    if (busy || id === currentSessionId) return
    const target = sessions.find((s) => s.id === id)
    if (!target) return
    saveCurrentSession()
    setCurrentSessionId(id)
    setMessages(target.messages)
    // 会话记忆由 bridge 按 sessionId 自动隔离：发送时带上 id 即可，无需清空
  }

  const newSession = () => {
    if (busy) return
    saveCurrentSession()
    const ns: ChatSession = {
      id: String(Date.now()),
      title: "新对话",
      messages: [],
      createdAt: Date.now(),
    }
    setSessions((prev) => [...prev, ns])
    setCurrentSessionId(ns.id)
    setMessages([])
    // 新会话首次发送时携带 ns.id，bridge 自动为该会话创建独立的 A1 记忆 thread
  }

  // 导出会话报告为 Markdown（存服务器 biomni_reports/）
  const exportReport = () => {
    if (busy) return
    const lines: string[] = ["# Biomni Chat 会话报告", "", `- 会话: ${currentSession?.title ?? ""}`, ""]
    messages.forEach((m) => {
      if (m.role === "user") {
        lines.push(`## 用户\n\n${m.content}`)
      } else {
        const parts: string[] = []
        if (m.content) parts.push(m.content)
        if (m.planDraft) parts.push(`### 研究计划\n\n${m.planDraft}`)
        if (m.report) parts.push(`### 执行报告\n\n${m.report}`)
        if (parts.length) lines.push(`## Biomni\n\n${parts.join("\n\n")}`)
      }
    })
    vscode.postMessage({ type: "export_report", content: lines.join("\n\n") })
  }

  const deleteSession = (id: string) => {
    if (busy) return
    if (sessions.length <= 1) return
    if (confirmDeleteId !== id) {
      // 第一次点击：进入确认态
      setConfirmDeleteId(id)
      return
    }
    // 第二次点击：真正删除
    setConfirmDeleteId(null)
    setSessions((prev) => {
      const next = prev.filter((s) => s.id !== id)
      if (id === currentSessionId) {
        const target = next[0]
        setCurrentSessionId(target.id)
        setMessages(target.messages)
        // 切换目标会话；A1 记忆由发送时的 sessionId 自动切换
      }
      return next
    })
  }

  // 仅在任务进行中每秒刷新（用于耗时显示；任务结束后计时停止）
  useEffect(() => {
    if (!busy) return
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [busy])

  useEffect(() => {
    vscode.postMessage({ type: "ready" })

    const handler = (e: MessageEvent) => {
      const msg = e.data
      if (!msg || typeof msg.type !== "string") return

      if (msg.type === "ready") {
        // 空状态由欢迎页渲染（messages.length === 0）
      } else if (msg.type === "status") {
        const text = String(msg.text ?? "")
        if (!text) return
        patchLast((last) => {
          const history = last.taskHistory ?? []
          const deduped =
            history.length > 0 && history[history.length - 1] === text ? history : [...history, text]
          // 时间线：工作条目按状态细分（执行代码=紫 / 已完成=绿 / 其他=蓝），去重相邻相同
          const timeline = last.timeline ?? []
          const prev = timeline[timeline.length - 1]
          const workKind: "work" | "code" | "done" = text.includes("执行代码")
            ? "code"
            : text.startsWith("已完成")
              ? "done"
              : "work"
          const newTimeline =
            prev && prev.kind === workKind && prev.text === text
              ? timeline
              : [...timeline, { kind: workKind, text }]
          // 执行步骤进度：按工作条目数粗粒度推进（仅对 planDraft 提取的 todo 生效；
          // 原生 checklist（source=checklist）由 bridge 的 todo_update 精确驱动，这里跳过）
          let todos = last.todos
          if (todos && todos.length > 0 && todos[0]?.source !== "checklist") {
            const workCount = newTimeline.filter((t) => t.kind !== "thinking").length
            const active = Math.min(Math.floor(workCount / 2), todos.length - 1)
            todos = todos.map((td, k) => ({
              ...td,
              status: k <= active ? ("in_progress" as const) : ("pending" as const),
            }))
          }
          return { ...last, taskStatus: text, taskHistory: deduped, timeline: newTimeline, todos }
        })
      } else if (msg.type === "clarification_question") {
        patchLast((last) => {
          const item: ClarificationItem = {
            id: Number(msg.id ?? 0),
            question: String(msg.question ?? ""),
            options: Array.isArray(msg.options) ? msg.options : [],
            optionDetails: Array.isArray(msg.option_details) ? msg.option_details : [],
            answered: false,
          }
          const existing = (last.clarifications ?? []).filter((c) => c.id !== item.id)
          return { ...last, clarifications: [...existing, item] }
        })
      } else if (msg.type === "plan_draft") {
        // 新计划到达：之前所有未确认的计划标记为「已替代」（多轮 plan 唯一可确认）
        setMessages((prev) =>
          prev.map((mm) =>
            mm.planDraft && !mm.planConfirmed ? { ...mm, planStale: true } : mm,
          ),
        )
        patchLast((last) => ({
          ...last,
          planDraft: String(msg.content ?? ""),
          planRound: msg.round,
          planStale: false,
          planEditing: false,
          planConfirmed: false,
        }))
        // 多轮 plan：计划回来后释放 busy，用户可继续提要求或确认执行
        setBusy(false)
      } else if (msg.type === "plan_explain") {
        // 提问解答：写入当前 assistant 占位消息；计划未变（未发新 plan_draft）→ 上一条计划确认按钮保留
        const ans = String(msg.content ?? "").trim()
        if (ans) {
          patchLast((last) => ({ ...last, content: (last.content || "") + ans }))
          setBusy(false)
        }
      } else if (msg.type === "report") {
        patchLast((last) => ({ ...last, report: String(msg.content ?? ""), reportExpanded: false }))
      } else if (msg.type === "todo_update") {
        // 原生 checklist 动态 todo（bridge 驱动，对齐 Roo TodoList）
        const items = Array.isArray(msg.items) ? msg.items : []
        if (items.length > 0) {
          patchLast((last) => ({
            ...last,
            todos: items.map((t) => ({
              id: t.id,
              text: String(t.content ?? "").slice(0, 120),
              status:
                t.status === "completed"
                  ? ("completed" as const)
                  : t.status === "in_progress"
                    ? ("in_progress" as const)
                    : ("pending" as const),
              source: "checklist" as const,
            })),
          }))
        }
      } else if (msg.type === "deliverables") {
        // A1 真实落盘的交付物（含绝对路径）
        const items = Array.isArray(msg.items) ? msg.items : []
        if (items.length > 0) {
          patchLast((last) => ({
            ...last,
            deliverables: { dir: String(msg.dir ?? ""), items },
          }))
        }
      } else if (msg.type === "stream") {
        // 最终回答流式追加（打字机效果）
        const text = String(msg.content ?? "")
        if (!text) return
        patchLast((last) => ({ ...last, content: (last.content || "") + text }))
      } else if (msg.type === "reasoning") {
        // 思维链：累积到当前 assistant 消息 + 写入时间线（思考条目）
        const text = String(msg.content ?? "")
        if (!text) return
        patchLast((last) => {
          const r = last.reasoning ?? []
          const timeline = last.timeline ?? []
          return {
            ...last,
            reasoning: [...r, text],
            timeline: [...timeline, { kind: "thinking" as const, text }],
          }
        })
      } else if (msg.type === "title") {
        // LLM 生成的会话标题：写回「发起该消息的会话」（异步返回时可能已切换/新建会话）
        const t = String(msg.content ?? "").trim()
        if (t) {
          const sid = pendingTitleSessionRef.current ?? currentSessionIdRef.current
          pendingTitleSessionRef.current = null
          if (sid) {
            setSessions((prev) =>
              prev.map((s) => (s.id === sid ? { ...s, title: t } : s)),
            )
          }
        }
      } else if (msg.type === "mention_files") {
        setMentionFiles(Array.isArray(msg.files) ? msg.files : [])
      } else if (msg.type === "act_start") {
        // 对齐 Roo switch_mode：收到 act_start 自动切换到 Act 模式（右上角标签）
        setMode("act")
        if (msg.fresh) {
          // plan 确认后：新建一条独立的 Act 执行卡片（区别于 plan 卡片）
          // 从最近的研究计划提取执行步骤（todo）——用 ref 避免闭包拿到旧会话消息
          const lastPlan = [...stateMessagesRef.current].reverse().find((mm) => mm.planDraft)?.planDraft
          const todos = extractTodos(lastPlan)
          setMessages((prev) => [
            ...prev,
            {
              role: "assistant",
              content: "",
              taskStatus: "Act 模式执行中...",
              taskHistory: [],
              timeline: [],
              todos,
              startedAt: Date.now(),
              clarifications: [],
              taskExpanded: true, // 工作状态默认展开
            },
          ])
          setBusy(true)
        } else {
          // act/research 直接执行：更新已有占位消息 + 显式告知用户已切换模式
          patchLast((last) => {
            const timeline = last.timeline ?? []
            const notice = "已自动切换到 Act 模式执行（调研/咨询类任务直接执行，不再走计划流程）"
            return {
              ...last,
              taskStatus: "Act 模式执行中...",
              timeline: [...timeline, { kind: "work" as const, text: notice }],
            }
          })
        }
      } else if (msg.type === "done") {
        patchLast((last) => ({
          ...last,
          taskStatus: "已完成",
          content: String(msg.result ?? ""),
          finishedAt: Date.now(),
          taskExpanded: false, // 开始生成总回复：收起工作状态，让位给正式回答
          todos: last.todos?.map((td) => ({ ...td, status: "completed" as const })),
        }))
        setBusy(false)
        setMentionOpen(false)
      } else if (msg.type === "error") {
        patchLast((last) => ({
          ...last,
          taskStatus: "出错",
          content: String(msg.message ?? ""),
          finishedAt: Date.now(),
        }))
        setBusy(false)
      }
    }
    window.addEventListener("message", handler)
    return () => window.removeEventListener("message", handler)
  }, [])

  // 就地更新最后一条 assistant 消息
  const patchLast = (fn: (last: ChatMessage) => ChatMessage) => {
    setMessages((prev) => {
      const next = [...prev]
      const last = next[next.length - 1]
      if (last && last.role === "assistant") {
        next[next.length - 1] = fn(last)
      }
      return next
    })
  }

  // 自动滚动：只有用户位于底部时才跟随新消息（对齐 Roo isAtBottomRef）
  useEffect(() => {
    if (atBottomRef.current && !browsingRef.current) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" })
    }
  }, [messages])

  const handleMessagesScroll = () => {
    const el = messagesRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60
    atBottomRef.current = atBottom
    browsingRef.current = !atBottom
    setShowScrollBtn(!atBottom)
  }

  const scrollToBottom = () => {
    atBottomRef.current = true
    browsingRef.current = false
    setShowScrollBtn(false)
    bottomRef.current?.scrollIntoView({ behavior: "smooth" })
  }

  // 统一重发：msgs 为截断后的历史（不含新 user），prompt 为新请求（对齐 Roo submitEditedMessage/retry）
  const resendFrom = (msgs: ChatMessage[], prompt: string, sendMode?: Mode) => {
    const history = msgs
      .map((mm) => ({ role: mm.role, content: mm.content || mm.taskStatus || "" }))
      .filter((h) => h.content.trim())
      .slice(-4)
    // 新会话首次发送：请求 LLM 生成简洁标题（记录归属会话，防止异步返回时串到别的会话）
    if (msgs.length === 0) {
      pendingTitleSessionRef.current = currentSessionId
      vscode.postMessage({ type: "generate_title", prompt })
    }
    setMessages([
      ...msgs,
      { role: "user", content: prompt },
      {
        role: "assistant",
        content: "",
        taskStatus: "准备中...",
        taskHistory: [],
        timeline: [],
        startedAt: Date.now(),
        clarifications: [],
        taskExpanded: true,
      },
    ])
    vscode.postMessage({ type: "send", prompt, mode, history, sessionId: currentSessionId })
    setInput("")
    setMentionOpen(false)
    setBusy(true)
  }

  const send = () => {
    const text = input.trim()
    if (!text || busy) return
    resendFrom(messages, text)
  }

  // 重试：回到该 assistant 对应的 user 请求重新执行
  const retryMessage = (idx: number) => {
    if (busy) return
    let userIdx = -1
    for (let k = idx; k >= 0; k--) {
      if (messages[k].role === "user") {
        userIdx = k
        break
      }
    }
    if (userIdx < 0) return
    resendFrom(messages.slice(0, userIdx), messages[userIdx].content)
  }

  // 还原检查点：截断到该消息，之后可继续（轻量版，对齐 Roo checkpoint 恢复）
  const revertCheckpoint = (idx: number) => {
    if (busy) return
    setMessages((prev) => prev.slice(0, idx + 1))
    vscode.postMessage({ type: "new_conversation" })
    setMentionOpen(false)
  }

  // 删除单条消息
  const deleteMessage = (idx: number) => {
    if (busy) return
    setMessages((prev) => prev.filter((_, k) => k !== idx))
  }

  // 编辑 user 消息后重发
  const startEditUser = (idx: number) => {
    if (busy) return
    setEditingUserIndex(idx)
    setEditedUserText(messages[idx].content)
  }

  const saveEditUser = (idx: number) => {
    const text = editedUserText.trim()
    if (!text || busy) return
    setEditingUserIndex(null)
    resendFrom(messages.slice(0, idx), text)
  }

  const stop = () => {
    vscode.postMessage({ type: "cancel" })
    setBusy(false)
    patchLast((last) => ({ ...last, taskStatus: "已取消", finishedAt: Date.now() }))
  }

  // 快捷键（对齐 Roo）：Esc 停止 / Ctrl(⌘)+M 切换模式
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === "Escape" && busy) {
        stop()
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "m") {
        e.preventDefault()
        setMode((m) => (m === "plan" ? "act" : "plan"))
      }
    }
    window.addEventListener("keydown", h)
    return () => window.removeEventListener("keydown", h)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy])

  const toggleTask = (index: number) => {
    setMessages((prev) => {
      const next = [...prev]
      const m = next[index]
      if (m && m.role === "assistant") next[index] = { ...m, taskExpanded: !m.taskExpanded }
      return next
    })
  }

  // ---------- 澄清回答 ----------
  const submitClarify = (index: number, c: ClarificationItem) => {
    const isOther = c.selected === OTHER_LABEL
    const option = isOther ? "" : c.selected || ""
    const answer = isOther ? c.freeText || "" : c.freeText || ""
    if (!option && !answer) return
    vscode.postMessage({ type: "clarify_answer", questionId: c.id, option, answer })
    patchLastAt(index, (last) => ({
      ...last,
      clarifications: (last.clarifications ?? []).map((x) =>
        x.id === c.id ? { ...x, answered: true } : x,
      ),
    }))
  }

  const patchLastAt = (index: number, fn: (last: ChatMessage) => ChatMessage) => {
    setMessages((prev) => {
      const next = [...prev]
      next[index] = fn(next[index])
      return next
    })
  }

  // ---------- 计划确认/编辑 ----------
  const confirmPlan = (index: number, draft: string) => {
    const m = messages[index]
    if (!m) return
    if (m.planEditing) {
      // 编辑态：把编辑后的计划提交给 bridge（更新 pending），退出编辑态
      vscode.postMessage({ type: "plan_edit", content: draft, sessionId: currentSessionId })
      patchLastAt(index, (last) => ({ ...last, planEditing: false }))
      return
    }
    // 确认 = 标记已确认 + 自动发送「开始实施」→ 走 plan 反馈流程执行（用户可见）
    patchLastAt(index, (last) => ({ ...last, planConfirmed: true, finishedAt: Date.now() }))
    setMode("act") // 对齐 Roo switch_mode：确认后自动切换到 Act 模式
    resendFrom(messages, "开始实施", "plan")
  }

  const togglePlanEdit = (index: number) => {
    patchLastAt(index, (last) => ({ ...last, planEditing: !last.planEditing }))
  }

  const setPlanDraft = (index: number, content: string) => {
    patchLastAt(index, (last) => ({ ...last, planDraft: content }))
  }

  const copyText = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      const ta = document.createElement("textarea")
      ta.value = text
      document.body.appendChild(ta)
      ta.select()
      document.execCommand("copy")
      document.body.removeChild(ta)
    }
  }

  // ---------- @ 文件提及 ----------
  const onInputKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      send()
    } else if (e.key === "@") {
      setMentionOpen(true)
      setMentionFilter("")
      vscode.postMessage({ type: "mention_open" })
    } else if (e.key === "Escape") {
      setMentionOpen(false)
    }
  }

  const pickMention = (f: string) => {
    setInput((prev) => {
      const at = prev.lastIndexOf("@")
      const base = at >= 0 ? prev.slice(0, at) : prev
      return `${base}@${f} `
    })
    setMentionOpen(false)
  }

  const filteredMentions = mentionFiles.filter((f) => f.toLowerCase().includes(mentionFilter.toLowerCase()))

  return (
    <div className="app">
      {!sidebarCollapsed && (
        <aside className="sidebar">
          <div className="sidebar-head">
            <span>会话</span>
            <button className="sidebar-collapse" onClick={() => setSidebarCollapsed(true)} title="收起">
              «
            </button>
          </div>
          <div className="sidebar-list">
            {sessions.map((s) => (
              <div
                key={s.id}
                className={`sidebar-item ${s.id === currentSessionId ? "active" : ""}`}
                onClick={() => switchSession(s.id)}
                title={s.title}
              >
                <div className="sidebar-item-top">
                  <span className="sidebar-title">{s.title}</span>
                  {sessions.length > 1 && (
                    <button
                      className={`sidebar-del ${confirmDeleteId === s.id ? "confirm" : ""}`}
                      title="删除会话"
                      onClick={(e) => {
                        e.stopPropagation()
                        deleteSession(s.id)
                      }}
                    >
                      {confirmDeleteId === s.id ? "确认？" : "×"}
                    </button>
                  )}
                </div>
                <div className="sidebar-meta">{s.messages.length} 条消息</div>
              </div>
            ))}
          </div>
          <button className="sidebar-new" onClick={newSession} disabled={busy}>
            + 新建会话
          </button>
        </aside>
      )}
      <div className="main">
        <header className="header">
          <div className="header-left">
            <button
              className="sidebar-toggle"
              onClick={() => setSidebarCollapsed((v) => !v)}
              title="会话列表"
              disabled={busy}
            >
              ☰
            </button>
            <span className="brand">Biomni Chat</span>
            <span className="session-title">{currentSession?.title ?? ""}</span>
          </div>
          <div className="header-right">
            <button
              className="new-chat-btn"
              onClick={() => setView(view === "settings" ? "chat" : "settings")}
              title={view === "settings" ? "返回聊天" : "设置（BYOK/镜像/部署）"}
            >
              {view === "settings" ? "聊天" : "设置"}
            </button>
            <button className="new-chat-btn" onClick={exportReport} title="导出会话报告为 Markdown" disabled={busy}>
              导出
            </button>
            <button className="new-chat-btn" onClick={newSession} title="新建会话" disabled={busy}>
              新对话
            </button>
            <div className="mode-switch">
          {MODES.map((m) => (
            <button
              key={m.key}
              className={`mode-btn ${mode === m.key ? "active" : ""}`}
              title={m.hint}
              onClick={() => setMode(m.key)}
              disabled={busy}
            >
              {m.label}
            </button>
          ))}
          </div>
        </div>
      </header>

      {view === "settings" ? (
        <SettingsPanel vscode={vscode} />
      ) : (
        <>
      <div className="messages" ref={messagesRef} onScroll={handleMessagesScroll}>
        {messages.length === 0 && (
          <div className="welcome">
            <div className="welcome-title">Biomni Chat</div>
            <div className="welcome-subtitle">生物信息学 AI 研究助手 · 基于 Biomni A1</div>
            <div className="welcome-modes">
              {MODES.map((m) => (
                <button
                  key={m.key}
                  className={`welcome-mode ${mode === m.key ? "active" : ""}`}
                  onClick={() => setMode(m.key)}
                >
                  <span className="welcome-mode-label">{m.label}</span>
                  <span className="welcome-mode-hint">{m.hint}</span>
                </button>
              ))}
            </div>
            <div className="welcome-tips">
              <span>试试：</span>
              <code>@文件 对 GSE30691 做一个差异分析</code>
            </div>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <div className="msg-label">{m.role === "user" ? "You" : "Biomni"}</div>

            {/* ===== 执行过程时间线（思考+工作交错，按时间顺序；折叠显示当前状态） ===== */}
            {m.role === "assistant" && m.timeline && m.timeline.length > 0 && (() => {
              const s = m.taskStatus ?? ""
              const statusClass = s.includes("已完成")
                ? "done"
                : s.includes("出错")
                  ? "error"
                  : s.includes("已取消")
                    ? "cancelled"
                    : s.includes("思考")
                      ? "thinking"
                      : s.includes("调用工具")
                        ? "tool"
                        : s.includes("执行代码")
                          ? "code"
                          : "working"
              const lastWork = [...m.timeline].reverse().find((t) => t.kind === "work")
              const displayStatus = m.taskStatus || lastWork?.text || "工作中"
              return (
                <div
                  className={`timeline-block ${statusClass}`}
                  onClick={() => toggleTask(i)}
                  role="button"
                  aria-expanded={!!m.taskExpanded}
                >
                  <span className="task-chevron">{m.taskExpanded ? "▾" : "▸"}</span>
                  {busy && i === messages.length - 1 && (
                    <span className="task-spinner" aria-label="工作中" />
                  )}
                  <span className="timeline-title">{m.taskExpanded ? "执行过程" : displayStatus}</span>
                  {m.startedAt && (
                    <span className="task-elapsed">
                      {m.finishedAt ? fmtElapsed(m.finishedAt - m.startedAt) : fmtElapsed(now - m.startedAt)}
                    </span>
                  )}
                  {m.todos && m.todos.length > 0 && (
                    <div className="todo-list" onClick={(e) => e.stopPropagation()}>
                      {m.todos.map((td, k) => (
                        <div key={td.id || k} className={`todo-item ${td.status}`}>
                          <span className="todo-check">
                            {td.status === "completed" ? "✓" : td.status === "in_progress" ? "▶" : "○"}
                          </span>
                          <span className="todo-text">{td.text}</span>
                        </div>
                      ))}
                    </div>
                  )}
                  {m.taskExpanded && <TimelineBody items={m.timeline} />}
                </div>
              )
            })()}

            {/* ===== 澄清选择题卡片（Plan 模式） ===== */}
            {m.role === "assistant" && m.clarifications && m.clarifications.length > 0 && (
              <div className="clarify-card">
                {m.clarifications
                  .filter((c) => !c.answered)
                  .map((c) => (
                    <div key={c.id} className="clarify-item">
                      <div className="clarify-question">{c.question}</div>
                      <div className="clarify-options">
                        {c.options.map((opt) => {
                          const detail = (c.optionDetails ?? []).find((d) => d.text === opt)
                          return (
                            <div key={opt} className="clarify-opt-wrap">
                              <button
                                className={`clarify-opt ${c.selected === opt ? "selected" : ""}`}
                                onClick={() =>
                                  patchLastAt(i, (last) => ({
                                    ...last,
                                    clarifications: (last.clarifications ?? []).map((x) =>
                                      x.id === c.id ? { ...x, selected: opt } : x,
                                    ),
                                  }))
                                }
                              >
                                {detail?.recommended && (
                                  <span className="clarify-opt-rec">推荐</span>
                                )}
                                <span>{opt}</span>
                              </button>
                              {detail?.pros_cons && (
                                <div className="clarify-opt-detail">{detail.pros_cons}</div>
                              )}
                            </div>
                          )
                        })}
                      </div>
                      {c.selected === OTHER_LABEL && (
                        <input
                          className="clarify-free"
                          placeholder="请输入..."
                          value={c.freeText ?? ""}
                          onChange={(e) =>
                            patchLastAt(i, (last) => ({
                              ...last,
                              clarifications: (last.clarifications ?? []).map((x) =>
                                x.id === c.id ? { ...x, freeText: e.target.value } : x,
                              ),
                            }))
                          }
                        />
                      )}
                      <button
                        className="clarify-submit"
                        disabled={!c.selected || (c.selected === OTHER_LABEL && !c.freeText?.trim())}
                        onClick={() => submitClarify(i, c)}
                      >
                        提交回答
                      </button>
                    </div>
                  ))}
              </div>
            )}

            {/* ===== 可编辑计划卡片（Plan 模式，多轮可精进） ===== */}
            {m.role === "assistant" && m.planDraft && (
              <div className="plan-card">
                <div className="plan-header">
                  <span>研究计划{m.planRound && m.planRound > 1 ? `（第 ${m.planRound} 版）` : ""}</span>
                  <span className="plan-badge">
                    {m.planConfirmed ? "已确认" : m.planStale ? "已替代" : "待确认"}
                  </span>
                </div>
                {!m.planConfirmed && !m.planStale && !m.planEditing && (
                  <div className="plan-hint">
                    💡 可直接对计划提问（我会解答，计划不变）、提要求（生成新版计划），或点「确认计划」执行
                  </div>
                )}
                {m.planEditing ? (
                  <textarea
                    className="plan-textarea"
                    value={m.planDraft}
                    onChange={(e) => setPlanDraft(i, e.target.value)}
                    rows={18}
                  />
                ) : (
                  // 非编辑态：Markdown 渲染（含表格/列表），对齐 Roo MarkdownBlock
                  <div className="plan-body markdown-body">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.planDraft}</ReactMarkdown>
                  </div>
                )}
                <div className="plan-actions">
                  {m.planConfirmed ? (
                    <span className="plan-status-label confirmed">✓ 已确认</span>
                  ) : m.planStale ? (
                    <span className="plan-status-label stale">⏳ 已由后续版本替代</span>
                  ) : m.planEditing ? (
                    <>
                      <button className="plan-btn primary" onClick={() => confirmPlan(i, m.planDraft!)}>
                        ✓ 确认修改并执行
                      </button>
                      <button className="plan-btn" onClick={() => togglePlanEdit(i)}>
                        取消编辑
                      </button>
                    </>
                  ) : (
                    <>
                      <button className="plan-btn primary" onClick={() => confirmPlan(i, m.planDraft!)}>
                        ✓ 确认计划
                      </button>
                      <button className="plan-btn" onClick={() => togglePlanEdit(i)}>
                        编辑
                      </button>
                    </>
                  )}
                </div>
              </div>
            )}

            {/* ===== 交付物卡片（A1 真实落盘文件 + 绝对路径，点击复制） ===== */}
            {m.role === "assistant" && m.deliverables && m.deliverables.items.length > 0 && (
              <div className="deliverables-card">
                <div className="deliverables-header">
                  <span className="deliverables-title">
                    📁 交付物（{m.deliverables.items.length} 个文件）
                  </span>
                  <button
                    className="report-copy"
                    onClick={() => copyText(m.deliverables!.dir)}
                    title="复制交付物目录绝对路径"
                  >
                    复制目录路径
                  </button>
                </div>
                <div className="deliverables-dir" onClick={() => copyText(m.deliverables!.dir)} title="点击复制">
                  {m.deliverables.dir}
                </div>
                <ul className="deliverables-list">
                  {m.deliverables.items.map((d, k) => (
                    <li
                      key={k}
                      className="deliverable-item"
                      onClick={() => copyText(d.path)}
                      title={`点击复制路径\n${d.path}`}
                    >
                      <span className="deliverable-icon">
                        {/\.(png|jpe?g|gif|svg|pdf|html)$/i.test(d.name) ? "🖼️" : "📄"}
                      </span>
                      <span className="deliverable-name">{d.name}</span>
                      <span className="deliverable-size">
                        {d.size > 1024 * 1024
                          ? `${(d.size / 1024 / 1024).toFixed(1)} MB`
                          : d.size > 1024
                            ? `${(d.size / 1024).toFixed(1)} KB`
                            : `${d.size} B`}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {/* ===== 报告卡片（Act / Plan 执行后） ===== */}
            {m.role === "assistant" && m.report && (
              <div className="report-card">
                <div
                  className="report-header"
                  onClick={() =>
                    patchLastAt(i, (last) => ({ ...last, reportExpanded: !last.reportExpanded }))
                  }
                  role="button"
                >
                  <span className="task-chevron">{m.reportExpanded ? "▾" : "▸"}</span>
                  <span>执行报告</span>
                  <button
                    className="report-copy"
                    onClick={(e) => {
                      e.stopPropagation()
                      copyText(m.report!)
                    }}
                  >
                    复制
                  </button>
                </div>
                {m.reportExpanded && (
                  <div className="report-body markdown-body">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.report}</ReactMarkdown>
                  </div>
                )}
              </div>
            )}

            {/* ===== 正式回答区 ===== */}
            {m.role === "user" && editingUserIndex === i ? (
              <div className="user-edit">
                <textarea
                  className="input-textarea"
                  value={editedUserText}
                  onChange={(e) => setEditedUserText(e.target.value)}
                  rows={3}
                />
                <div className="user-edit-actions">
                  <button className="plan-btn primary" onClick={() => saveEditUser(i)}>
                    保存并发送
                  </button>
                  <button className="plan-btn" onClick={() => setEditingUserIndex(null)}>
                    取消
                  </button>
                </div>
              </div>
            ) : (
              m.content && (
                <div className="msg-content markdown-body">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown>
                </div>
              )
            )}
            {/* 消息操作（对齐 Roo ChatRow hover 操作） */}
            {m.role === "user" && (
              <div className="msg-actions">
                <button className="msg-action" onClick={() => startEditUser(i)}>
                  编辑
                </button>
              </div>
            )}
            {m.role === "assistant" && m.content && (
              <div className="msg-actions">
                <button className="msg-action" onClick={() => copyText(m.content)}>
                  复制
                </button>
                <button className="msg-action" onClick={() => retryMessage(i)}>
                  重试
                </button>
                <button className="msg-action" onClick={() => revertCheckpoint(i)}>
                  还原到此
                </button>
                <button className="msg-action danger" onClick={() => deleteMessage(i)}>
                  删除
                </button>
              </div>
            )}
          </div>
        ))}
        <div ref={bottomRef} />
        {showScrollBtn && (
          <button className="scroll-bottom-btn" onClick={scrollToBottom}>
            ↓ 回到底部
          </button>
        )}
      </div>

      {/* ===== 输入区：@ 提及 + Send/Stop（多行自适应） ===== */}
      <div className="input-row">
        <div className="input-wrap">
          <textarea
            className="input-textarea"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onInputKeyDown}
            onInput={(e) => {
              const el = e.currentTarget
              el.style.height = "auto"
              el.style.height = Math.min(el.scrollHeight, 160) + "px"
            }}
            placeholder={
              mode === "plan" && lastPendingPlan
                ? "对当前计划提要求（发送消息即可精进），或点「确认计划」执行..."
                : `Ask Biomni... (${mode === "plan" ? "研究方案" : "完整任务"})  @引用文件 · Enter发送 · Shift+Enter换行`
            }
            disabled={busy}
            rows={1}
          />
          {mentionOpen && (
            <div className="mention-menu" ref={mentionRef}>
              <input
                className="mention-filter"
                placeholder="过滤文件..."
                value={mentionFilter}
                onChange={(e) => setMentionFilter(e.target.value)}
                autoFocus
              />
              <div className="mention-list">
                {filteredMentions.length === 0 && <div className="mention-empty">（无匹配文件）</div>}
                {filteredMentions.slice(0, 50).map((f) => (
                  <div key={f} className="mention-item" onClick={() => pickMention(f)}>
                    {f}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
        {busy ? (
          <button className="send-btn stop" onClick={stop} title="停止任务">
            ⏹
          </button>
        ) : (
          <button className="send-btn" onClick={send} disabled={!input.trim()} title="发送">
            ➤
          </button>
        )}
      </div>
        </>
      )}
      </div>
    </div>
  )
}
