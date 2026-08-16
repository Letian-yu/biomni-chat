#!/usr/bin/env python
"""Biomni Chat 服务器侧桥 (多模式: Ask / Plan / Act)

通过 stdio + JSON lines 与 VS Code 扩展通信。

协议:
  扩展 -> 桥:
    {"type": "chat", "prompt": "...", "mode": "ask|plan|act"}
    {"type": "clarify_answer", "question_id": 0, "option": "...", "answer": "..."}
    {"type": "plan_confirm"}
    {"type": "plan_edit", "content": "..."}
    {"type": "cancel"}
  桥 -> 扩展:
    {"type": "ready"} / {"type": "status", "text"} / {"type": "done", "result"}
    {"type": "clarification_question", "id", "question", "options"}
    {"type": "plan_draft", "content"} / {"type": "report", "content"}
    {"type": "error", "message"}

关键点:
  - 加载 BYOK 配置 (.env); A1 初始化 print 会污染 stdout -> 重定向到 stderr
  - send() 必须写模块加载时的 _orig_stdout, 不受运行时重定向影响
"""
import json
import os
import re
import select
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# A1 使用 MemorySaver(thread_id=42) 进程内持久化对话记忆；
# 若不重置，之前的对话（如测试用的 GSE30691）会污染后续全新课题。
try:
    from langgraph.checkpoint.memory import MemorySaver
except Exception:
    MemorySaver = None

# ---- .env 加载（必须在 import biomni 之前！） ----
# 坑: biomni 的 a1.py 在模块级 load_dotenv(".env")，但它在 default_config
# (biomni/config.py 模块级创建) 之后才执行，导致 default_config 读不到 BIOMNI_*，
# A1 会回退到默认 LLM 配置。因此这里必须在 import biomni 之前显式加载 .env。
# 路径优先级: $BIOMNI_ENV_FILE > 本仓库 .env > $BIOMNI_DIR/.env（默认 /data/biomni）
_BIOMNI_DIR = os.environ.get("BIOMNI_DIR", "/data/biomni")


def _resolve_env_file() -> Path:
    """解析 .env 文件路径。"""
    if os.environ.get("BIOMNI_ENV_FILE"):
        p = Path(os.environ["BIOMNI_ENV_FILE"])
        if p.exists():
            return p
    p = Path(__file__).resolve().parents[1] / ".env"
    if p.exists():
        return p
    return Path(_BIOMNI_DIR) / ".env"


def _load_env() -> None:
    """加载 .env 到 os.environ（幂等；override=False 让已有环境变量优先）。"""
    p = _resolve_env_file()
    if p.exists():
        load_dotenv(p, override=False)
        print(f"[biomni-bridge] loaded env: {p}", file=sys.stderr)
    else:
        print(f"[biomni-bridge] warning: no .env found at {p}", file=sys.stderr)


_load_env()

# ---- 落盘日志（复盘用）----
# 记录每次 Ai 消息/工具/代码/轮次，便于排查"假完成"等异常。
# 位置: $BIOMNI_BRIDGE_LOG_DIR 或 ~/.biomni-chat/bridge-<日期>.log
_BRIDGE_LOG_DIR = os.environ.get("BIOMNI_BRIDGE_LOG_DIR") or os.path.join(
    os.path.expanduser("~"), ".biomni-chat"
)


def _log_file_path() -> str:
    os.makedirs(_BRIDGE_LOG_DIR, exist_ok=True)
    return os.path.join(_BRIDGE_LOG_DIR, f"bridge-{time.strftime('%Y%m%d')}.log")


def log_line(*parts) -> None:
    """追加一行落盘日志（独立于 stdout 协议，任何异常不影响主流程）。"""
    try:
        ts = time.strftime("%H:%M:%S")
        with open(_log_file_path(), "a", encoding="utf-8") as f:
            f.write(f"[{ts}] " + " ".join(str(p) for p in parts) + "\n")
    except Exception:
        pass


# ---- 交付物目录 ----
# A1 报告里的相对路径（如 "output"）常是口头声称且不落盘，用户无法定位。
# 这里固定一个真实交付物根目录：$BIOMNI_OUTPUT_DIR 可覆盖，默认 <biomni_dir>/biomni_data/output。
# 每次执行创建独立子目录，结束由 bridge 扫描真实产物并连同绝对路径发给前端。
_OUTPUT_DIR = os.environ.get("BIOMNI_OUTPUT_DIR") or os.path.join(
    _BIOMNI_DIR, "biomni_data", "output"
)


def scan_deliverables(output_dir: str) -> list[dict]:
    """扫描交付物目录，返回真实存在的文件清单 [{name, path, size}]（按路径排序）。"""
    files: list[dict] = []
    if not os.path.isdir(output_dir):
        return files
    for root, _dirs, fnames in os.walk(output_dir):
        for fn in fnames:
            fp = os.path.join(root, fn)
            try:
                size = os.path.getsize(fp)
            except Exception:
                size = 0
            files.append({"name": os.path.relpath(fp, output_dir), "path": fp, "size": size})
    files.sort(key=lambda x: x["name"])
    return files


_orig_stdout = sys.stdout
_MAX_CLARIFY_ROUNDS = 4

# ---- 会话级记忆（方案2：thread 隔离，替代粗暴 reset）----
# LangGraph MemorySaver 原生支持多 thread_id 隔离。A1 原本写死 thread_id=42，
# 已 patch site-packages a1.py 使其读取 agent._thread_id。这里维护 session_id -> thread_id 映射：
#   - 同会话连续 chat：复用 thread → A1 记住所有轮次（真多轮）
#   - 新会话（未知 session_id）：新建 thread → 天然干净（无需 reset_agent_memory）
#   - 跨会话隔离：不同 thread 互不干扰（根治 GSE30691 污染 HCMV 类问题）
_threads: dict[str, str] = {}
_thread_seq = 0


def _new_thread_id() -> str:
    global _thread_seq
    _thread_seq += 1
    return f"biomni-{_thread_seq}-{int(time.time() * 1000)}"


def _resolve_thread(agent, session_id: str | None) -> None:
    """根据会话 id 设置 agent._thread_id：已有会话复用（保留记忆），新会话新建（干净）。"""
    if session_id:
        tid = _threads.get(session_id)
        if tid is None:
            tid = _new_thread_id()
            _threads[session_id] = tid
            log_line("=== 新会话 thread 创建: session=", session_id, "->", tid)
    else:
        tid = getattr(agent, "_thread_id", None) or _new_thread_id()
    agent._thread_id = tid


def _clear_thread(session_id: str | None = None) -> None:
    """显式清空指定会话（或全部）的 thread 记忆。"""
    global _threads
    if session_id:
        _threads.pop(session_id, None)
        log_line("=== 清空会话记忆: session=", session_id)
    else:
        _threads = {}
        log_line("=== 清空全部会话记忆 ===")


def send(msg: dict) -> None:
    _orig_stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    _orig_stdout.flush()


def read_next_message(timeout: float | None = None) -> dict | None:
    """阻塞读取 stdin 下一条 JSON 消息（澄清/计划交互用）。"""
    while True:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        line = sys.stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except Exception:
            continue


# ---------- 澄清 / 计划 / 报告 LLM (BYOK) ----------
def _llm_client():
    from openai import OpenAI
    base = os.getenv("BIOMNI_CUSTOM_BASE_URL") or "https://api.deepseek.com/v1"
    key = os.getenv("BIOMNI_CUSTOM_API_KEY") or "EMPTY"
    return OpenAI(base_url=base, api_key=key)


def _llm_text(system: str, user: str) -> str:
    client = _llm_client()
    model = os.getenv("BIOMNI_LLM", "deepseek-chat")
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.5,
    )
    return resp.choices[0].message.content or ""


def _llm_json(system: str, user: str) -> dict:
    text = _llm_text(system + "\n只输出 JSON，不要其他内容。", user)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


_CLARIFY_SYSTEM = (
    "你是研究方案澄清助手。研究流程已完成前期调研，你掌握了课题背景。\n"
    "你的任务: 只针对「无法从公开信息确定、必须由用户决策」的技术细节，逐题向用户提问。\n"
    "规则:\n"
    "1. 每轮只输出一道选择题（JSON），澄清一个最关键、尚未明确的决策点。\n"
    "2. 为每个选项给出简单利弊权衡（pros_cons，格式如「优点：...；缺点：...」），"
    "   并推荐其中一个最稳妥/最通用的选项（recommended=true，只推荐一个）。\n"
    "3. 必须包含一个\"其他（自由填写）\"选项（该选项无需利弊与推荐）。\n"
    "4. 禁止重复已经问过的问题。\n"
    "5. 自己能根据调研确定的技术细节绝对不要问。一般问 2-3 道题就足够；\n"
    "   若没有必须用户决策的细节，输出 {\"done\": true}。\n"
    "6. JSON 格式: {\"question\": \"...\", \"options\": [\"...\", ...], "
    "\"option_details\": [{\"text\": \"选项原文\", \"pros_cons\": \"优点：...；缺点：...\", \"recommended\": true|false}, ...]}"
)

_PLAN_SYSTEM = (
    "你是一位资深生物信息学方法学专家。基于前期调研结果与用户澄清信息，"
    "生成一份详细、可执行的研究计划（Markdown）。\n"
    "结构: 研究目标 / 数据与方法 / 分析步骤（编号列表）/ 预期结果 / 风险评估 / 算力评估与耗时预估。\n"
    "「算力评估与耗时预估」栏目必须基于下方【服务器资源快照】真实评估，包含：\n"
    "  ① 数据规模（样本/细胞/基因数、数据量）\n"
    "  ② 计算复杂度（各步骤轻/中/重）\n"
    "  ③ 资源需求（内存/CPU/GPU/磁盘，结合服务器资源判断是否满足）\n"
    "  ④ 预计总耗时（分钟/小时，按步骤拆解）"
)

_REPORT_SYSTEM = (
    "你是一位科研报告撰写专家。根据 Biomni agent 的执行结果，"
    "生成一份结构化的研究报告（Markdown）。\n"
    "结构: 摘要 / 研究方法 / 主要结果 / 局限性 / 下一步建议。"
)

_RESEARCH_SYSTEM = (
    "你是一位资深生物信息学研究助理。对给定的研究课题进行前期调研（使用可用检索工具获取真实信息），"
    "为制定详细研究计划收集背景信息。\n"
    "输出（Markdown）:\n"
    "1. 课题背景与现状（简洁概括）\n"
    "2. 该课题的标准分析流程与常用方法/工具/数据库\n"
    "3. 通过公开信息可以自主确定的技术细节\n"
    "4. 无法从公开信息确定、必须由用户决策的技术细节（作为后续澄清候选问题）"
)


def generate_question(goal: str, details: list, research: str = "") -> dict:
    asked = "\n".join(f"- {d.get('q','')}" for d in details)
    detail_text = "\n".join(f"- {d.get('q','')}: {d.get('answer','')}" for d in details)
    user = (
        f"研究目标: {goal}\n\n"
        f"前期调研结果:\n{(research[:3000] if research else '(无)')}\n\n"
        f"已经问过的问题（绝对不要重复，问全新维度）:\n{asked or '(无)'}\n\n"
        f"已收集信息:\n{detail_text or '(无)'}\n\n"
        f"请基于调研结果，输出下一道「必须由用户决策」的技术细节选择题。"
    )
    return _llm_json(_CLARIFY_SYSTEM, user)


def _detect_gpu() -> str:
    """检测 GPU（nvidia-smi）。"""
    import subprocess

    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            return ", ".join(n.strip() for n in r.stdout.strip().splitlines()[:2])
    except Exception:
        pass
    return ""


def _mem_gb() -> int:
    """系统内存 GB。"""
    try:
        import psutil
        return int(psutil.virtual_memory().total / 1024**3)
    except Exception:
        pass
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal"):
                    return int(line.split()[1]) // 1024 // 1024
    except Exception:
        pass
    return 0


def _disk_free_gb(path: str) -> int:
    try:
        st = os.statvfs(path)
        return int(st.f_bavail * st.f_frsize / 1024**3)
    except Exception:
        return 0


def _env_resources_summary() -> str:
    """服务器资源快照（供 A1 算力评估，真实而非空谈）。"""
    cpu = os.cpu_count() or 0
    gpu = _detect_gpu()
    mem = _mem_gb()
    disk = _disk_free_gb(_BIOMNI_DIR)
    gpu_s = f"GPU: {gpu}" if gpu else "GPU: 无"
    return (
        f"【服务器资源快照】CPU {cpu} 核；内存约 {mem} GB；{gpu_s}；"
        f"磁盘剩余约 {disk} GB。请基于此评估每一步的算力与耗时是否可行。"
    )


def _fmt_duration(sec: float) -> str:
    s = int(max(0, sec))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m {s % 60}s"
    return f"{m // 60}h {m % 60}m"


def generate_plan(goal: str, details: list, research: str = "") -> str:
    detail_text = "\n".join(f"- {d.get('q','')}: {d.get('answer','')}" for d in details)
    user = (
        f"研究目标: {goal}\n\n"
        f"前期调研结果:\n{(research[:4000] if research else '(无)')}\n\n"
        f"用户澄清信息:\n{detail_text or '(无)'}\n\n"
        f"{_env_resources_summary()}\n\n"
        f"请结合调研、澄清与服务器资源，生成研究计划。"
    )
    return _llm_text(_PLAN_SYSTEM, user)


def generate_report(goal: str, execution_summary: str) -> str:
    user = f"研究任务: {goal}\n\n执行结果摘要:\n{execution_summary[:4000]}\n\n请生成研究报告。"
    return _llm_text(_REPORT_SYSTEM, user)


def parse_stream_output(out: str):
    """从 A1 pretty_print 输出中解析出 (消息类型, 内容)。"""
    m = re.match(r"^=+\s*(\w+)\s*Message\s*=+\s*(?:\n+)?(.*)$", out, re.DOTALL)
    if m:
        return m.group(1), m.group(2).strip()
    return None, out.strip()


def extract_solution(text: str) -> str:
    """从文本中提取 <solution> 标签内容；无标签则返回原文。"""
    m = re.search(r"<solution>(.*?)</solution>", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


def final_answer_from_log(agent) -> str:
    """从 A1 的 self.log（pretty_print 每步输出）提取最后的 Ai 消息干净答案。"""
    for out in reversed(getattr(agent, "log", []) or []):
        kind, content = parse_stream_output(out)
        if kind == "Ai" and content:
            return extract_solution(content)
    return ""


# ============ 原生 checklist 提取 + bridge 驱动动态 todo（对齐 Roo TodoList）============
# Roo 的 parseMarkdownChecklist 正则: /^(?:-\s*)?\[\s*([ xX\-~])\s*\]\s+(.+)$/
# 这里扩展支持编号前缀（DeepSeek 实测输出 "1. [ ] 步骤"）
_CHECKLIST_RE = re.compile(
    r"^\s*(?:\d+[\.\)、]\s*)?(?:[-*]\s*)?\[\s*([ xX\-~])\s*\]\s+(.+?)\s*$"
)
_TODO_ADVANCE_EVERY = 2  # 每 N 个执行动作（工具/代码 observation）推进一个 todo


def _todo_id(text: str, status: str) -> str:
    import hashlib

    return hashlib.md5(f"{text}|{status}".encode("utf-8")).hexdigest()[:12]


def parse_checklist(content: str) -> list[dict] | None:
    """从 Ai 消息提取 checklist（对齐 Roo parseMarkdownChecklist + 编号支持）。
    返回 [{'id','content','status'}]；无 checklist 返回 None。
    """
    items: list[dict] = []
    for line in content.splitlines():
        line = line.strip()
        m = _CHECKLIST_RE.match(line)
        if not m:
            continue
        marker, text = m.group(1), m.group(2).strip()
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)  # 去掉 DeepSeek 的 **加粗**
        text = text.strip().rstrip(":").strip()
        if not text:
            continue
        if marker in ("x", "X"):
            status = "completed"
        elif marker in ("-", "~"):
            status = "in_progress"
        else:
            status = "pending"
        items.append({"id": _todo_id(text, status), "content": text[:120], "status": status})
    return items or None


def _send_todos(items: list[dict] | None) -> None:
    if items:
        send({"type": "todo_update", "items": items, "source": "checklist"})


def _same_todo_list(a: list[dict] | None, b: list[dict] | None) -> bool:
    return bool(a and b and len(a) == len(b) and [t["id"] for t in a] == [t["id"] for t in b])


def sync_todos_from_checklist(state: dict | None, parsed: list[dict]) -> dict:
    """首次提取→初始化（激活第一项）；内容相同的重复输出→忽略（保留已推进状态）；
    全新 checklist→替换。返回新 state。"""
    if state is None:
        state = {"items": [], "obs": 0}
    current = state["items"]
    if _same_todo_list(current, parsed):
        return state  # 内容未变（DeepSeek 重复输出同一初始计划）→ 不覆盖已推进状态
    todos = [dict(t) for t in parsed]
    # 无进行中且未全完成 → 激活第一项
    if not any(t["status"] == "in_progress" for t in todos) and not all(
        t["status"] == "completed" for t in todos
    ):
        first = next((i for i, t in enumerate(todos) if t["status"] == "pending"), None)
        if first is not None:
            todos[first]["status"] = "in_progress"
    _send_todos(todos)
    return {"items": todos, "obs": 0}


def advance_todos(state: dict | None) -> dict | None:
    """每个执行动作（observation）调用：每 N 次把当前进行中项标记完成并激活下一项。"""
    if not state:
        return state
    items = state["items"]
    if not items or all(t["status"] == "completed" for t in items):
        return state
    state["obs"] += 1
    if state["obs"] % _TODO_ADVANCE_EVERY != 0:
        return state
    idx = next((i for i, t in enumerate(items) if t["status"] == "in_progress"), None)
    if idx is None:
        idx = next((i for i, t in enumerate(items) if t["status"] == "pending"), None)
        if idx is not None:
            items[idx]["status"] = "in_progress"
            _send_todos(items)
        return state
    items[idx]["status"] = "completed"
    nxt = next(
        (i for i, t in enumerate(items[idx + 1:], start=idx + 1) if t["status"] == "pending"),
        None,
    )
    if nxt is not None:
        items[nxt]["status"] = "in_progress"
    _send_todos(items)
    return state


def check_cancel() -> bool:
    """非阻塞检查 stdin 是否有 cancel 消息。"""
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if ready:
        line = sys.stdin.readline()
        try:
            msg = json.loads(line)
            if msg.get("type") == "cancel":
                print("[biomni-bridge] cancel received", file=sys.stderr)
                return True
        except Exception:
            pass
    return False


# A1 的 LLM 偶尔会把 <solution> 包住整个回复（含 <execute>），导致 generate 节点
# answer_match 命中 -> next_step="end" 提前终止（只输出计划/第一步）。以下标记用于
# 检测这种"未完成"收尾，触发自动续跑。
_INCOMPLETE_MARKERS = [
    "让我重新开始",
    "开始执行第一步",
    "首先，让我",
    "让我先",
    "接下来我将",
    "现在开始执行",
    "<execute>",
    "先制定计划",
    "尚未完成",
]


def looks_incomplete(text: str) -> bool:
    return any(m in text for m in _INCOMPLETE_MARKERS)


def reset_agent_memory(agent) -> None:
    """重置 A1 的会话记忆（MemorySaver），防止旧对话污染新课题。"""
    if MemorySaver is None:
        return
    try:
        agent.checkpointer = MemorySaver()
        agent.app.checkpointer = agent.checkpointer
    except Exception:
        pass


_TITLE_SYSTEM = (
    "你是会话命名助手。根据用户第一条消息的内容，生成一个简洁的会话标题。\n"
    "要求：不超过 15 个字，概括任务主题，不要标点结尾，不要引号。\n"
    "示例：用户消息『我想对HCMV关联神经疾病做一个meta分析，帮我调研选题』→『HCMV与神经疾病meta调研』\n"
    "只输出标题本身。"
)


def generate_title(prompt: str) -> str:
    try:
        t = _llm_text(_TITLE_SYSTEM, f"用户消息：{prompt[:800]}").strip().strip('"').strip("'")
        return t[:30]
    except Exception:
        return ""


def build_history_context(history) -> str:
    """把前端传来的对话历史转成上下文文本（保留同课题连续对话的上文）。
    只取最近 4 条，避免旧课题（如测试数据集）污染新课题。
    """
    lines = []
    for h in (history or [])[-4:]:
        role = "用户" if h.get("role") == "user" else "助手"
        text = (h.get("content") or "").strip()[:2000]
        if text:
            lines.append(f"{role}: {text}")
    if not lines:
        return ""
    return "【之前的对话上下文】\n" + "\n".join(lines)


_DESCRIBE_SYSTEM = (
    "你是生物信息学助手，负责用一句话向用户说明 agent 正在执行的动作。\n"
    "输入是一段代码，输出一句不超过 20 字的中文概括（不要代码、不要函数名、面向生物研究者）。\n"
    "示例：\n"
    "  代码含 limma/差异分析 → \"正在运行差异分析\"\n"
    "  代码下载 GEO/数据集 → \"正在下载数据集\"\n"
    "  代码做富集分析 → \"正在进行功能富集分析\"\n"
    "  代码查 PubMed → \"正在检索文献\"\n"
    "只输出那一句话，不要引号。"
)


def describe_code(code: str) -> str:
    """用 LLM 概括一段代码在做什么（一句中文）。失败时返回空串（前端回退通用文案）。"""
    snippet = (code or "").strip()[:1500]
    if not snippet:
        return ""
    try:
        text = _llm_text(_DESCRIBE_SYSTEM, f"代码：\n{snippet}")
        return text.strip().strip('"').strip("'")
    except Exception:
        return ""


def extract_execute_code(content: str) -> str:
    m = re.search(r"<execute>(.*?)</execute>", content, re.DOTALL)
    return m.group(1) if m else content


def tool_call_desc(content: str, tool_name: str) -> str:
    """从 Ai 消息中提取工具调用的关键参数，拼成简短说明（不调 LLM，正则提取）。"""
    param_pat = re.compile(
        r"(?:search_term|query|uniprot_id|accession|gene|keyword|dataset|term|id|prompt)\s*=\s*['\"]([^'\"]{1,60})['\"]",
        re.IGNORECASE,
    )
    params = [m.group(1) for m in param_pat.finditer(content)][:2]
    if params:
        return f"{tool_name}（{' / '.join(params)}）"
    return f"{tool_name}()"


_OBS_SUMMARY_SYSTEM = (
    "你是生物信息学助手。agent 刚完成一次工具调用/代码执行并返回结果。\n"
    "请用一句不超过 30 字的中文概括这次拿到了什么结果并简短评价\n"
    "（如检索到了什么、数据质量如何、是否满足需求）。\n"
    "示例：\n"
    "  已获取 GSE30691 数据集信息：大鼠背根神经节，56 样本\n"
    "  limma 分析完成，得到 779 个显著基因\n"
    "  文献检索未找到直接相关结果，建议扩大关键词\n"
    "只输出那一句话，不要引号。"
)


def summarize_observation(obs: str) -> str:
    """用 LLM 对一次工具/代码执行的结果生成简短总结（一句中文）。失败时返回空串。"""
    snippet = (obs or "").strip()[:800]
    if not snippet:
        return ""
    try:
        text = _llm_text(_OBS_SUMMARY_SYSTEM, f"工具返回结果：\n{snippet}")
        return text.strip().strip('"').strip("'")
    except Exception:
        return ""


# ---- 方法学透明性辅助（A1 脚本落盘 / A2 执行日志 / A3 METHODOLOGY）----

def _detect_lang(code: str) -> str:
    """根据 <execute> 内容判断语言。"""
    first = code.lstrip()
    if first.startswith("#!R") or first.startswith("# R code") or first.startswith("# R script"):
        return "R"
    if first.startswith("#!BASH") or first.startswith("# Bash") or first.startswith("#!CLI"):
        return "BASH"
    return "PY"


def _safe_tag(desc: str) -> str:
    """把描述转成安全文件名片段（保留字母数字/中文，去特殊字符）。"""
    if not desc:
        return ""
    keep: list[str] = []
    for ch in desc:
        if ch.isalnum():
            keep.append(ch)
        elif keep and keep[-1] != "_":
            keep.append("_")
        if len(keep) >= 12:
            break
    return "".join(keep).strip("_") or ""


def _write_script(task_dir: str, seq: int, desc: str, code: str):
    """把一段 <execute> 代码落盘为脚本文件（方法学透明性 A1）。返回 (新seq, 相对路径或None)。"""
    try:
        lang = _detect_lang(code)
        ext = {"R": "R", "BASH": "sh", "PY": "py"}[lang]
        tag = _safe_tag(desc) or f"step{seq:02d}"
        name = f"step{seq:02d}_{tag}.{ext}"
        seq += 1
        scripts_dir = os.path.join(task_dir, "scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        header = f"# ---- {desc or '分析步骤'} ----\n# 由 Biomni Chat 自动落盘（方法学透明性）\n\n"
        with open(os.path.join(scripts_dir, name), "w", encoding="utf-8") as f:
            f.write(header + code + "\n")
        return seq, f"scripts/{name}"
    except Exception as e:
        log_line("脚本落盘失败:", e)
        return seq, None


def _flush_exec_log(task_dir: str | None, exec_log: list[str]) -> None:
    """把执行轨迹写入 task_dir/execution_log.md（A2）。"""
    if not task_dir or not exec_log:
        return
    try:
        os.makedirs(task_dir, exist_ok=True)
        with open(os.path.join(task_dir, "execution_log.md"), "w", encoding="utf-8") as f:
            f.write("# 分析执行日志\n\n" + "\n".join(exec_log) + "\n")
    except Exception as e:
        log_line("execution_log 写入失败:", e)


def _pip_key_versions() -> list[str]:
    """获取关键包版本（A3，用于 METHODOLOGY）。"""
    import subprocess

    keys = ["scanpy", "anndata", "pandas", "numpy", "scipy", "leidenalg", "umap", "matplotlib", "seaborn", "rpy2", "biomni"]
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True, timeout=25)
        lines = r.stdout.splitlines()
        return [ln for ln in lines if any(ln.lower().startswith(k) for k in keys)]
    except Exception:
        return []


def _write_methodology(task_dir: str, task: str, deliverables: list[dict], elapsed_sec: float | None = None) -> None:
    """A3: 生成 METHODOLOGY.md（数据来源/执行轨迹/包版本/交付物/实际耗时）。"""
    try:
        lines = [
            "# 分析方法学（Methodology）",
            "",
            f"- 任务: {task[:200]}",
            f"- 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if elapsed_sec is not None:
            lines.append(f"- 实际总耗时: {_fmt_duration(elapsed_sec)}")
        log_path = os.path.join(task_dir, "execution_log.md")
        if os.path.exists(log_path):
            log_text = open(log_path, encoding="utf-8").read()
            gses = sorted(set(re.findall(r"GSE\d+", log_text)))
            if gses:
                lines += ["", "## 数据来源", ""] + [f"- {g}" for g in gses]
            lines += ["", "## 执行轨迹", "", "```", log_text, "```"]
        pkg_ver = _pip_key_versions()
        if pkg_ver:
            lines += ["", "## 关键包版本", "", "```"] + pkg_ver + ["```"]
        if deliverables:
            lines += ["", "## 交付物"]
            for d in deliverables:
                lines.append(f"- `{d['name']}` ({d['size']} B)")
        with open(os.path.join(task_dir, "METHODOLOGY.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        log_line("METHODOLOGY 生成失败:", e)


def stream_agent(agent, task: str, max_rounds: int = 3, require_execution: bool = False, task_dir: str | None = None) -> str:
    """驱动 A1 go_stream，检测"提前收尾/假完成"并自动续跑；返回最终答案。

    - 支持 cancel（返回空串表示已取消）
    - A1 可能因 <solution> 提前 end 而只输出计划+第一步，检测到后用其输出继续驱动
    - require_execution=True（分析执行阶段）：若一轮结束未执行任何工具/代码就输出
      结论，判定为"假完成"并强制续跑（对齐 Roo：agent 必须通过工具/代码产出真实结果）
    - 原生 checklist 提取 + bridge 驱动动态 todo（对齐 Roo TodoList）：
      * 从 Ai 消息提取 A1 原生 checklist → 初始化 todo 列表（首个激活）
      * DeepSeek 从不更新 checklist，故由 bridge 按执行动作（observation）数推进状态
    - task_dir 提供时：自动捕获 A1 的 <execute> 代码落盘为 scripts/，并记录执行日志
      （方法学透明性 A1/A2，不依赖 A1 自觉保存）
    """
    current = task
    todo_state = None  # {"items": [{id,content,status}], "obs": 执行动作计数}
    script_seq = 0
    exec_log: list[str] = []
    log_line("=== stream_agent 开始 require_execution=", require_execution,
             "| task_dir=", task_dir, "| task:", task[:200].replace("\n", " "))
    for rnd in range(1, max_rounds + 1):
        executed = False  # 本轮是否真实执行过工具/代码
        if rnd > 1:
            send({"type": "status", "text": f"检测到任务未完成，继续执行（第{rnd}轮）..."})
        log_line(f"--- 第 {rnd} 轮开始 ---")

        _saved = sys.stdout
        sys.stdout = sys.stderr
        try:
            for step in agent.go_stream(current):
                if check_cancel():
                    send({"type": "status", "text": "已取消"})
                    return ""
                out = step.get("output", "")
                mtype, content = parse_stream_output(out)
                if mtype != "Ai" or not content:
                    continue
                log_line(f"[Ai#{rnd}] {content[:2000]}")
                # 原生 checklist 提取：A1 首轮输出编号 checkbox 计划（DeepSeek 实测约 75% 任务会输出）
                parsed = parse_checklist(content)
                if parsed:
                    todo_state = sync_todos_from_checklist(todo_state, parsed)
                # 思维链：优先 <think> 标签；否则提取标签前的文本作为思考/意图说明
                # （对齐 Biomni a1.py 的 thinking 提取逻辑：execute/solution 标签前的文本）
                think_m = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
                if think_m and think_m.group(1).strip():
                    think_text = think_m.group(1).strip()[:2000]
                    send({"type": "reasoning", "content": think_text})
                else:
                    first_tag = re.search(r"<execute>|<solution>", content)
                    if first_tag:
                        thinking = content[: first_tag.start()].strip()
                        if thinking:
                            send({"type": "reasoning", "content": thinking[:800]})
                tool_m = re.search(
                    r"from biomni\.tool\.\w+ import (\w+)", content
                ) or re.search(
                    r"\b(query_\w+|get_\w+|search_\w+|design_\w+|annotate_\w+|run_\w+|download_\w+|predict_\w+)\s*\(",
                    content,
                )
                if tool_m:
                    executed = True
                    name = tool_m.group(1)
                    # 工具调用：提取关键参数拼成具体说明（如 检索 GSE30691）
                    desc = tool_call_desc(content, name)
                    exec_log.append(f"- 工具 `{name}` {desc}")
                    send({"type": "status", "text": f"正在调用工具 {desc}"})
                elif "<execute>" in content:
                    executed = True
                    # 代码执行：用 LLM 概括这段代码在做什么（如 正在运行差异分析）
                    code = extract_execute_code(content)
                    desc = describe_code(code)
                    # 脚本落盘（方法学透明性 A1）：把真实代码保存为可重跑脚本
                    if task_dir:
                        script_seq, script_rel = _write_script(task_dir, script_seq, desc or "code", code)
                        if script_rel:
                            exec_log.append(f"- 代码 `{script_rel}` · {desc or '执行代码'}")
                    send({
                        "type": "status",
                        "text": f"正在执行代码 · {desc}" if desc else "正在执行代码...",
                    })
                # 工具/代码执行结果返回：推进动态 todo + 生成简短总结（反馈工作量，也帮助 agent 回顾）
                obs_m = re.search(r"<observation>(.*?)</observation>", content, re.DOTALL)
                if obs_m and obs_m.group(1).strip():
                    executed = True
                    todo_state = advance_todos(todo_state)
                    summary = summarize_observation(obs_m.group(1))
                    if summary:
                        exec_log.append(f"  ↳ 结果: {summary}")
                        send({"type": "status", "text": f"已完成 · {summary}"})
        finally:
            sys.stdout = _saved

        final = final_answer_from_log(agent)
        if not final:
            _flush_exec_log(task_dir, exec_log)
            return ""
        log_line(f"--- 第 {rnd} 轮结束: executed={executed} final={final[:300]!r} ---")
        # 假完成检测：要求执行但本轮零执行且输出看似完成 → 强制续跑
        if require_execution and not executed and not looks_incomplete(final):
            send({"type": "status", "text": "检测到未实际执行任何工具/代码就输出结论，强制重新执行..."})
            current = (
                final
                + "\n\n【重要！你刚才在没有执行任何工具调用或代码的情况下直接输出了结论，"
                "这是不可接受的。你是在执行真实的数据分析任务，必须立即实际动手：\n"
                "1. 先调用工具下载/查询数据，或读取本地文件\n"
                "2. 运行真实的分析代码（<execute>），观察执行结果\n"
                "3. 基于真实执行结果逐步完成所有步骤\n"
                "禁止在未执行任何工具或代码时输出最终答案。"
            )
            continue
        if not looks_incomplete(final):
            _flush_exec_log(task_dir, exec_log)
            return final
        # 提前收尾 -> 用当前输出作为新 prompt 续跑
        current = final + "\n\n请继续完成上述任务中尚未完成的所有步骤，最终给出完整结果。"
    _flush_exec_log(task_dir, exec_log)
    return final


def research_topic(agent, goal: str) -> str:
    """对课题进行轻量级前期调研（用 A1 + 工具检索），返回调研报告文本。

    调研阶段开启 research_only_mode，A1 只能检索信息（元数据/文献），
    禁止下载数据集或运行主分析——真正的分析留给确认计划后的执行阶段。
    """
    send({"type": "status", "text": "正在对课题进行前期调研..."})
    prompt = (
        f"请对以下研究课题进行轻量级前期调研，为制定研究计划收集背景信息：\n\n{goal}\n\n"
        "【重要：调研阶段限制】\n"
        "- 严格遵循用户请求的范围与目标：用户只要选题建议/背景调研时，只输出选题建议与评估，\n"
        "  不要擅自制定完整执行计划，不要开展具体数据分析\n"
        "- 你只负责检索信息：查询数据集元数据（物种/平台/样本数/分组/实验设计）、检索相关文献\n"
        "- 绝对禁止：下载任何数据集文件、运行分析代码、写入文件到 data_lake、读取本地表达矩阵\n"
        "- 真正的数据下载与主分析将在用户确认计划后的执行阶段进行，调研阶段不做\n\n"
        "要求（务必控制时间和范围）：\n"
        "1. 使用工具检索数据库/文献获取真实信息，检索控制在 2-4 次以内\n"
        "2. 重点获取：课题背景、数据集概况（物种/平台/样本数/分组设计）、"
        "标准分析流程、常用方法与数据库\n"
        "3. 明确区分「可自主确定的技术细节」与「必须由用户决策的技术细节」"
        "（后者是后续澄清的候选问题）\n"
        "4. 检索到足够信息后立即整理输出调研结论（放在 <solution> 标签内）；"
        "禁止用 <solution> 包裹 <execute>\n"
        "5. 单个工具失败或超时立即跳过，不要重试卡住；整个调研尽量在 2 分钟内完成"
    )
    agent.research_only_mode = True
    try:
        return stream_agent(agent, prompt)
    finally:
        agent.research_only_mode = False


def send_result_streaming(final: str) -> None:
    """把最终答案按句切分流式发送（打字机效果），最后发 done。"""
    final = final or ""
    # 按句子/换行切分，再合并成 >=20 字块，控制块数量
    raw = [p for p in re.split(r"(?<=[。！？.!?\n])", final) if p.strip()]
    parts: list = []
    buf = ""
    for p in raw:
        buf += p
        if len(buf) >= 20:
            parts.append(buf)
            buf = ""
    if buf:
        parts.append(buf)
    if not parts:
        parts = [final]
    for part in parts:
        send({"type": "stream", "content": part})
        time.sleep(0.02)
    send({"type": "done", "result": final})


def execute_act(agent, task: str, with_report: bool, fresh_task: bool = False, require_execution: bool = False) -> None:
    """执行 A1 完整流程：act_start 标记新卡片；流式工作状态；可选生成研究报告；支持 cancel。

    fresh_task=True 时前端会新建一条 Act 执行消息（plan 确认后进入执行阶段）；
    False 时复用已有的占位消息（act 直接执行）。
    require_execution=True（分析执行）时，若 A1 未执行任何工具/代码就输出结论会强制续跑。
    交付物：每次执行创建独立子目录 <output>/<时间戳>/，纪律强制 A1 写绝对路径，
    结束扫描真实文件并连同绝对路径发给前端（避免 A1 口头声称"output"却不落盘）。
    """
    send({"type": "act_start", "fresh": fresh_task, "mode": "act"})
    send({"type": "status", "text": "Act 模式执行中..."})
    _exec_t0 = time.time()  # 记录执行开始时间（② 预估 vs 实际耗时对照）
    # 执行阶段：确保关闭调研模式，允许下载数据与运行主分析
    agent.research_only_mode = False
    # 交付物目录：本次执行独立子目录（隔离 + 便于扫描本次新增）
    task_dir = os.path.join(_OUTPUT_DIR, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(task_dir, exist_ok=True)
    # 强化执行纪律：必须逐项完成计划所有步骤/交付物（对齐 Roo update_todo_list 逐项完成机制）
    execution_prompt = (
        f"{task}\n\n"
        "【执行纪律（必须严格遵守）】\n"
        "0. 禁止在未执行任何工具或代码时直接输出最终答案！你必须先实际动手：\n"
        "   下载/读取数据、运行真实的分析代码（<execute>）、观察执行结果，\n"
        "   再基于真实结果输出结论。未执行任何工具/代码就输出结论将被视为无效\n"
        f"1. 所有交付物（图表/表格/结果文件）必须真实保存到绝对路径目录：{task_dir}\n"
        "   （在该目录下创建子目录或直接写文件，用 os.path.join 拼接绝对路径，不要用相对路径）\n"
        "2. 你正在执行用户已批准的研究计划，必须逐项完成计划中列出的每一个步骤与交付物\n"
        "3. 每完成一个步骤，简要说明该步已完成\n"
        "4. 计划中提到的所有图表/表格/结果文件都必须产出，不可遗漏"
        "（如火山图、热图、Venn图、富集分析图等，全部都要交付）\n"
        "5. 全部步骤完成后，输出最终结果前，先逐条对照计划核对交付清单，补齐任何缺失项\n"
        "6. 最终报告里必须列出所有交付物的完整绝对路径，供用户直接访问"
        "（放在 <solution> 标签内）"
    )
    log_line("=== execute_act 开始 fresh=", fresh_task, "| task_dir=", task_dir,
             "| task:", task[:200].replace("\n", " "))
    final = stream_agent(agent, execution_prompt, require_execution=require_execution, task_dir=task_dir)
    if not final:
        send({"type": "done", "result": "[任务未产生有效结果]"})
        return  # 已取消或无结果
    log_line("=== execute_act 完成，final 长度:", len(final))
    send_result_streaming(final)
    # 扫描本次真实交付物（区别于 A1 口头声称），连同绝对路径发给前端
    deliverables = scan_deliverables(task_dir)
    # 生成 METHODOLOGY.md（方法学透明性 A3：数据来源/轨迹/包版本/交付物/实际耗时）
    _write_methodology(task_dir, task, deliverables, elapsed_sec=time.time() - _exec_t0)
    deliverables = scan_deliverables(task_dir)  # METHODOLOGY 加入后重新扫描
    if deliverables:
        send({"type": "deliverables", "dir": task_dir, "items": deliverables})
        log_line(f"=== 交付物 {len(deliverables)} 个: {task_dir}")
    else:
        log_line("=== 警告: task_dir 无任何交付物文件:", task_dir)
    if with_report and final:
        try:
            report = generate_report(task, final)
            send({"type": "report", "content": report})
        except Exception as e:  # noqa: BLE001
            send({"type": "status", "text": f"报告生成失败: {e}"})


_CLASSIFY_SYSTEM = (
    "判断用户请求此刻的意图：\n"
    "- 如果用户想「立即执行数据分析/跑模型/处理数据」"
    "（如：跑差异分析、下载数据集分析、建模、执行分析流程），返回 analysis\n"
    "- 如果用户想「先做调研/文献检索/选题建议/方案咨询/知识问答」"
    "（不运行数据分析，如：帮我调研、帮我选题、查一下文献、评估方向），返回 research\n"
    "注意以用户明确的请求意图为准：即使提到 'meta分析'，只要用户要求'帮我调研/选题'，也应返回 research。\n"
    "只输出一个词：analysis 或 research"
)


def classify_task(prompt: str) -> str:
    """LLM 判断任务类型：analysis（分析型）或 research（调研/咨询型）。"""
    try:
        t = _llm_text(_CLASSIFY_SYSTEM, f"用户请求：{prompt[:1000]}").strip().lower()
        return "analysis" if "analysis" in t else "research"
    except Exception:
        return "analysis"  # 默认按分析型处理（走完整 plan）


def handle_research(agent, prompt: str) -> None:
    """调研/咨询类任务：直接检索调研并给出结论，不跑数据分析、不走计划流程。"""
    send({"type": "act_start", "fresh": False, "mode": "act"})
    send({"type": "status", "text": "开始调研..."})
    research_prompt = (
        f"用户请求：{prompt}\n\n"
        "【任务要求】\n"
        "请完成用户的调研/检索/选题建议请求。\n"
        "1. 使用工具检索文献/数据库获取真实信息，检索控制在 5 次以内\n"
        "2. 若用户要选题建议：给出多个候选方向并权衡利弊，给出推荐\n"
        "3. 若用户要前期调研：输出调研结论（背景、研究现状、关键文献/数据、下一步建议）\n"
        "4. 不要下载数据集或运行数据分析（那是分析阶段的活）\n"
        "5. 检索到足够信息后输出完整结论（放在 <solution> 标签内）\n"
        "6. 单个工具失败立即跳过继续，不要卡住"
    )
    agent.research_only_mode = True
    try:
        final = stream_agent(agent, research_prompt)
    finally:
        agent.research_only_mode = False
    send_result_streaming(final or "[未产生有效结果]")


def handle_act(agent, prompt: str) -> None:
    execute_act(agent, prompt, with_report=True, require_execution=True)  # 分析执行：强制真实动手


# ---- 多轮 plan（方案 B，对标 Roo "对话即迭代"）----
# session_id -> {"plan", "round", "goal", "research", "details", "details_text"}
_pending_plan: dict[str, dict] = {}

_PLAN_INTENT_SYSTEM = (
    "判断用户对一份已生成研究计划的反馈意图。\n"
    "- 如果用户表达「同意/满意/开始执行/开始实施/就按这个来/OK/好/执行吧/可以」等"
    "明确同意并要开始实施 → 输出 execute\n"
    "- 如果用户对计划提要求/修改意见/补充/质疑/换方法/增加步骤等"
    "（即使提到'执行/实施'，只要是提出修改意见）→ 输出 refine\n"
    "只输出一个词：execute 或 refine"
)


def _classify_plan_intent(prompt: str, plan: str) -> str:
    """LLM 判断用户对计划的反馈意图：execute（同意执行）或 refine（提要求精进）。"""
    try:
        t = _llm_text(_PLAN_INTENT_SYSTEM, f"计划摘要:\n{plan[:1200]}\n\n用户消息:\n{prompt[:800]}").strip().lower()
        return "execute" if "execute" in t else "refine"
    except Exception:
        return "refine"  # 判断失败默认精进（安全，不会误执行）


_PLAN_REFINE_SYSTEM = (
    "你是一位资深生物信息学方法学专家。用户对一份已生成的研究计划提出了修改要求，"
    "请基于原计划 + 用户反馈 + 调研信息，精进并重新生成完整的研究计划（Markdown）。\n"
    "保持原有栏目结构，并把用户的修改要求落实进对应部分。\n"
    "结构: 研究目标 / 数据与方法 / 分析步骤（编号列表）/ 预期结果 / 风险评估 / 算力评估与耗时预估。"
)


def refine_plan(pending: dict, feedback: str) -> str:
    """基于原计划 + 用户反馈生成新版计划（多轮 plan 的核心）。"""
    user = (
        f"研究目标: {pending.get('goal', '')}\n\n"
        f"前期调研结果:\n{(pending.get('research') or '')[:4000]}\n\n"
        f"用户澄清信息:\n{pending.get('details_text') or '无'}\n\n"
        f"{_env_resources_summary()}\n\n"
        f"当前计划（第 {pending.get('round', 1)} 版）:\n{pending.get('plan', '')}\n\n"
        f"用户新的修改要求:\n{feedback}\n\n"
        f"请精进计划（这是第 {pending.get('round', 1) + 1} 版）。"
    )
    return _llm_text(_PLAN_REFINE_SYSTEM, user)


def handle_plan_feedback(agent, prompt: str, pending: dict, session_id: str) -> None:
    """方案 B：pending 计划存在时，用户输入框消息 = 反馈（精进）或执行（同意）。"""
    intent = _classify_plan_intent(prompt, pending["plan"])
    print(f"[biomni-bridge] plan intent={intent}", file=sys.stderr)
    if intent == "execute":
        plan = pending["plan"]
        _pending_plan.pop(session_id, None)
        send({"type": "status", "text": "已确认计划，开始执行..."})
        execute_act(agent, plan, with_report=True, fresh_task=True, require_execution=True)
    else:
        send({"type": "status", "text": "正在根据你的要求精进计划..."})
        new_plan = refine_plan(pending, prompt)
        pending["plan"] = new_plan
        pending["round"] += 1
        send({"type": "plan_draft", "content": new_plan, "round": pending["round"]})
        send({"type": "status", "text": f"已生成第 {pending['round']} 版计划，可继续提要求或确认执行"})


def handle_plan(agent, prompt: str, session_id: str | None = None) -> None:
    """Plan 模式：先判断任务类型。
    - research（调研/咨询类）：直接调研输出，不做完整计划
    - analysis（分析型）：前期调研 → 澄清 → 计划 → 确认 → 执行
    """
    # 0. 判断任务类型
    send({"type": "status", "text": "正在判断任务类型..."})
    task_type = classify_task(prompt)
    print(f"[biomni-bridge] plan task_type={task_type}", file=sys.stderr)
    if task_type == "research":
        send({"type": "status", "text": "检测到调研/咨询类任务，直接执行调研..."})
        handle_research(agent, prompt)
        return
    # 1. 前期调研（agent 用工具获取课题背景）
    send({"type": "status", "text": "开始制定研究计划..."})
    research = research_topic(agent, prompt)
    if not research:
        return  # 调研被取消或失败
    send({"type": "status", "text": "调研完成，正在澄清关键细节..."})

    # 2. 澄清（只问无法自主把控的技术细节）
    details = []
    for round_no in range(_MAX_CLARIFY_ROUNDS):
        q = generate_question(prompt, details, research)
        if not q or q.get("done"):
            break
        options = list(q.get("options", []))
        # 规范化 agent 提供的选项利弊与推荐（与 options 一一对应）
        details_map = {}
        for d in (q.get("option_details") or []):
            if isinstance(d, dict) and d.get("text"):
                details_map[str(d.get("text"))] = d
        option_details = []
        for opt in options:
            d = details_map.get(str(opt), {})
            option_details.append({
                "text": str(opt),
                "pros_cons": str(d.get("pros_cons", "") or ""),
                "recommended": bool(d.get("recommended")),
            })
        if not any(("其他" in str(o)) for o in options):
            options.append("其他（自由填写）")
            option_details.append({"text": "其他（自由填写）", "pros_cons": "", "recommended": False})
        send({
            "type": "clarification_question",
            "id": round_no,
            "question": q.get("question", "请补充一个关键细节"),
            "options": options,
            "option_details": option_details,
        })
        msg = read_next_message()
        if not msg:
            return
        if msg.get("type") == "cancel":
            send({"type": "status", "text": "已取消"})
            return
        if msg.get("type") == "clarify_answer":
            details.append({
                "q": q.get("question", ""),
                "answer": msg.get("answer") or msg.get("option") or "",
            })

    # 3. 生成可编辑计划（带调研+澄清信息）
    send({"type": "status", "text": "正在生成研究计划..."})
    plan = generate_plan(prompt, details, research)
    send({"type": "plan_draft", "content": plan, "round": 1})

    # 4. 挂起（方案 B 多轮 plan）：把计划存入 pending，等待用户反馈/确认。
    #    用户可在输入框直接发消息提要求 → bridge 判断精进(plan_refine)或执行(execute)
    _pending_plan[session_id or ""] = {
        "plan": plan,
        "round": 1,
        "goal": prompt,
        "research": research,
        "details": details,
        "details_text": "\n".join(f"- {d.get('q','')}: {d.get('answer','')}" for d in details),
    }
    send({"type": "status", "text": "计划已生成：可直接对计划提要求精进，或点「确认计划」执行"})


def main() -> None:
    # 保险：确保 .env 已加载（模块级已加载；此处防御未来 import 顺序变化）
    _load_env()

    # A1 初始化期间的 print 会污染 stdout 协议 -> 先重定向到 stderr
    _saved = sys.stdout
    sys.stdout = sys.stderr

    from biomni.agent import A1  # noqa: E402

    agent = A1(path=_BIOMNI_DIR, expected_data_lake_files=[])
    agent._thread_id = _new_thread_id()  # 初始会话 thread

    sys.stdout = _saved

    send({"type": "ready", "message": "Biomni bridge ready (multi-mode)"})
    print("[biomni-bridge] ready, waiting for requests...", file=sys.stderr)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue

        try:
            req_type = req.get("type")
            if req_type == "ping":
                send({"type": "pong"})
            elif req_type == "cancel":
                print("[biomni-bridge] cancel (idle)", file=sys.stderr)
            elif req_type == "new_conversation":
                # 显式清空会话记忆（带 session_id 清该会话；不带清全部，兼容旧前端）
                _clear_thread(req.get("session_id"))
                _pending_plan.pop(req.get("session_id") or "", None)  # 清多轮 plan 状态
                print("[biomni-bridge] new conversation (memory cleared)", file=sys.stderr)
            elif req_type == "generate_title":
                title = generate_title(req.get("prompt", ""))
                if title:
                    send({"type": "title", "content": title})
            elif req_type == "chat":
                prompt = req.get("prompt", "")
                # 仅支持 plan / act 两模式（Ask 已砍掉）；默认 plan
                mode = req.get("mode", "plan")
                print(f"[biomni-bridge] chat mode={mode}", file=sys.stderr)
                # 会话级记忆：按 session_id 解析 thread（同会话复用记忆=真多轮；
                # 新会话新建 thread=干净）。不再无条件 reset，避免跨轮失忆。
                _resolve_thread(agent, req.get("session_id"))
                # 注入前端显式传来的对话历史（兜底：bridge 重启/冷启动时帮助对齐）
                history = req.get("history") or []
                ctx = build_history_context(history)
                if ctx:
                    prompt = ctx + "\n\n" + prompt
                if mode == "act":
                    handle_act(agent, prompt)
                else:
                    # 方案 B 多轮 plan：该会话有待确认计划 → 用户输入视为反馈/执行
                    _sid = req.get("session_id") or ""
                    pending = _pending_plan.get(_sid)
                    if pending:
                        handle_plan_feedback(agent, prompt, pending, _sid)
                    else:
                        handle_plan(agent, prompt, req.get("session_id"))
            elif req_type == "plan_confirm":
                # 用户点「确认计划」→ 执行当前待确认计划
                _sid = req.get("session_id") or ""
                pending = _pending_plan.get(_sid)
                if pending:
                    plan = pending["plan"]
                    _pending_plan.pop(_sid, None)
                    send({"type": "status", "text": "已确认计划，开始执行..."})
                    execute_act(agent, plan, with_report=True, fresh_task=True, require_execution=True)
            elif req_type == "plan_edit":
                # 用户手动编辑计划 → 更新 pending 的计划内容
                _sid = req.get("session_id") or ""
                content = req.get("content") or ""
                if _pending_plan.get(_sid) and content:
                    _pending_plan[_sid]["plan"] = content
                    send({"type": "status", "text": "计划已编辑（可继续提要求或确认执行）"})
        except Exception as e:  # noqa: BLE001
            send({"type": "error", "message": str(e)})


if __name__ == "__main__":
    main()
