// Biomni Chat 消息协议类型

export type ChatMode = "plan" | "act"  // Ask 已砍掉，仅保留 Plan（规划）与 Act（执行）

// 澄清选择题的选项详情（agent 建议：利弊 + 推荐）
export interface OptionDetail {
  text: string
  pros_cons?: string
  recommended?: boolean
}

// extension <-> webview 消息
// 设置项数据结构
export interface ByokConfig {
  provider: string
  baseUrl: string
  model: string
  hasKey: boolean
}

export interface MirrorConfig {
  conda: string
  pip: string
  cran: string
}

export interface EnvStatus {
  python: boolean
  biomni: boolean
  tools: string[]
  dataLake: boolean
  dataLakeSize: string
  envFile: boolean
  remoteOk: boolean
  remoteMode: string
}

// 动态 todo 条目（对齐 Roo TodoItem：pending / in_progress / completed）
export interface TodoItem {
  id: string
  content: string
  status: "pending" | "in_progress" | "completed"
}

// 交付物条目（A1 分析真实落盘的文件）
export interface DeliverableItem {
  name: string // 相对路径（相对交付物目录）
  path: string // 绝对路径
  size: number // 字节
}

export interface WebviewToExt {
  type: "send" | "ready" | "clarify_answer" | "plan_confirm" | "plan_edit" | "cancel" | "mention_open" | "new_conversation" | "export_report" | "settings_load" | "settings_save" | "settings_test" | "mirror_save" | "env_check" | "deploy" | "deploy_l2" | "generate_title"
  prompt?: string
  mode?: ChatMode
  questionId?: number
  option?: string
  answer?: string
  content?: string // plan_edit 编辑后的计划内容 / export_report 报告内容
  history?: { role: string; content: string }[] // 对话历史（作为 A1 上下文）
  byok?: ByokConfig // settings_save
  mirrors?: MirrorConfig // mirror_save
}

export interface ExtToWebview {
  type: "status" | "done" | "error" | "ready" | "clarification_question" | "plan_draft" | "report" | "mention_files" | "act_start" | "reasoning" | "stream" | "todo_update" | "deliverables" | "settings_data" | "settings_test_result" | "deploy_result" | "deploy_progress" | "title"
  text?: string // status 内容
  result?: string // done 最终结果
  message?: string // error 信息
  line?: string // deploy_progress 输出行
  id?: number // clarification_question 题号
  question?: string // clarification_question 问题文本
  options?: string[] // clarification_question 选项列表
  option_details?: OptionDetail[] // clarification_question 选项详情（利弊/推荐）
  content?: string // plan_draft / report / reasoning 内容
  files?: string[] // mention_files 工作区文件列表（相对路径）
  items?: TodoItem[] | DeliverableItem[] // todo_update 动态 todo / deliverables 交付物
  dir?: string // deliverables 交付物根目录（绝对路径）
  fresh?: boolean // act_start 是否新建执行卡片（plan 确认后=true；act 直接执行=false）
  source?: string // deploy_progress/result 来源（deploy=L0L1, l2=数据湖）
  byok?: ByokConfig
  mirrors?: MirrorConfig
  env?: EnvStatus
  ok?: boolean
}

// extension <-> python bridge 消息 (JSON lines over stdio)
export interface BridgeRequest {
  type: "chat" | "ping" | "cancel" | "clarify_answer" | "plan_confirm" | "plan_edit" | "new_conversation"
  prompt?: string
  mode?: ChatMode
  questionId?: number
  option?: string
  answer?: string
  content?: string
  history?: { role: string; content: string }[]
}

export interface BridgeResponse {
  type: "status" | "done" | "error" | "pong" | "ready" | "clarification_question" | "plan_draft" | "report" | "act_start" | "reasoning" | "stream" | "todo_update" | "deliverables"
  text?: string
  result?: string
  message?: string
  id?: number
  question?: string
  options?: string[]
  option_details?: OptionDetail[]
  content?: string
  fresh?: boolean
  items?: TodoItem[] | DeliverableItem[]
  dir?: string
}
