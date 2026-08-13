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

_dotenv_path = Path(__file__).resolve().parents[1] / ".env"
if not _dotenv_path.exists():
    _dotenv_path = Path("/data/biomni/.env")
if _dotenv_path.exists():
    load_dotenv(_dotenv_path)
    print(f"[biomni-bridge] loaded env: {_dotenv_path}", file=sys.stderr)

_orig_stdout = sys.stdout
_MAX_CLARIFY_ROUNDS = 4


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
    "结构: 研究目标 / 数据与方法 / 分析步骤（编号列表）/ 预期结果 / 风险评估 / 时间安排。"
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


def generate_plan(goal: str, details: list, research: str = "") -> str:
    detail_text = "\n".join(f"- {d.get('q','')}: {d.get('answer','')}" for d in details)
    user = (
        f"研究目标: {goal}\n\n"
        f"前期调研结果:\n{(research[:4000] if research else '(无)')}\n\n"
        f"用户澄清信息:\n{detail_text or '(无)'}\n\n"
        f"请结合调研与澄清生成研究计划。"
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


def stream_agent(agent, task: str, max_rounds: int = 3) -> str:
    """驱动 A1 go_stream，检测"提前收尾"并自动续跑；返回最终答案。

    - 支持 cancel（返回空串表示已取消）
    - A1 可能因 <solution> 提前 end 而只输出计划+第一步，检测到后用其输出继续驱动
    """
    current = task
    for rnd in range(1, max_rounds + 1):
        if rnd > 1:
            send({"type": "status", "text": f"检测到任务未完成，继续执行（第{rnd}轮）..."})

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
                    name = tool_m.group(1)
                    # 工具调用：提取关键参数拼成具体说明（如 检索 GSE30691）
                    desc = tool_call_desc(content, name)
                    send({"type": "status", "text": f"正在调用工具 {desc}"})
                elif "<execute>" in content:
                    # 代码执行：用 LLM 概括这段代码在做什么（如 正在运行差异分析）
                    code = extract_execute_code(content)
                    desc = describe_code(code)
                    send({
                        "type": "status",
                        "text": f"正在执行代码 · {desc}" if desc else "正在执行代码...",
                    })
                # 工具/代码执行结果返回：生成简短总结（反馈工作量，也帮助 agent 回顾）
                obs_m = re.search(r"<observation>(.*?)</observation>", content, re.DOTALL)
                if obs_m and obs_m.group(1).strip():
                    summary = summarize_observation(obs_m.group(1))
                    if summary:
                        send({"type": "status", "text": f"已完成 · {summary}"})
        finally:
            sys.stdout = _saved

        final = final_answer_from_log(agent)
        if not final:
            return ""
        if not looks_incomplete(final):
            return final
        # 提前收尾 -> 用当前输出作为新 prompt 续跑
        current = final + "\n\n请继续完成上述任务中尚未完成的所有步骤，最终给出完整结果。"
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


def execute_act(agent, task: str, with_report: bool, fresh_task: bool = False) -> None:
    """执行 A1 完整流程：act_start 标记新卡片；流式工作状态；可选生成研究报告；支持 cancel。

    fresh_task=True 时前端会新建一条 Act 执行消息（plan 确认后进入执行阶段）；
    False 时复用已有的占位消息（ask/act 直接执行）。
    """
    send({"type": "act_start", "fresh": fresh_task, "mode": "act"})
    send({"type": "status", "text": "Act 模式执行中..."})
    # 执行阶段：确保关闭调研模式，允许下载数据与运行主分析
    agent.research_only_mode = False
    # 强化执行纪律：必须逐项完成计划所有步骤/交付物（对齐 Roo update_todo_list 逐项完成机制）
    execution_prompt = (
        f"{task}\n\n"
        "【执行纪律（必须严格遵守）】\n"
        "1. 你正在执行用户已批准的研究计划，必须逐项完成计划中列出的每一个步骤与交付物\n"
        "2. 每完成一个步骤，简要说明该步已完成\n"
        "3. 计划中提到的所有图表/表格/结果文件都必须产出，不可遗漏"
        "（如火山图、热图、Venn图、富集分析图等，全部都要交付）\n"
        "4. 全部步骤完成后，输出最终结果前，先逐条对照计划核对交付清单，补齐任何缺失项\n"
        "5. 只有所有步骤与交付物都完成时才输出最终答案（放在 <solution> 标签内）"
    )
    final = stream_agent(agent, execution_prompt)
    if not final:
        send({"type": "done", "result": "[任务未产生有效结果]"})
        return  # 已取消或无结果
    send_result_streaming(final)
    if with_report and final:
        try:
            report = generate_report(task, final)
            send({"type": "report", "content": report})
        except Exception as e:  # noqa: BLE001
            send({"type": "status", "text": f"报告生成失败: {e}"})


def handle_ask(agent, prompt: str) -> None:
    execute_act(agent, prompt, with_report=False)


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
    execute_act(agent, prompt, with_report=True)


def handle_plan(agent, prompt: str) -> None:
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
    send({"type": "plan_draft", "content": plan})

    # 4. 等待确认 / 编辑 / 取消
    msg = read_next_message()
    if not msg:
        return
    if msg.get("type") == "cancel":
        send({"type": "status", "text": "已取消"})
        return
    if msg.get("type") == "plan_edit" and msg.get("content"):
        plan = msg["content"]

    # 5. 确认 → 执行（前端创建新的 Act 执行卡片）
    execute_act(agent, plan, with_report=True, fresh_task=True)


def main() -> None:
    # A1 初始化期间的 print 会污染 stdout 协议 -> 先重定向到 stderr
    _saved = sys.stdout
    sys.stdout = sys.stderr

    from biomni.agent import A1  # noqa: E402

    agent = A1(path="/data/biomni", expected_data_lake_files=[])

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
                # 用户开启新对话：清空 A1 记忆，避免旧课题污染
                reset_agent_memory(agent)
                print("[biomni-bridge] new conversation (memory reset)", file=sys.stderr)
            elif req_type == "generate_title":
                title = generate_title(req.get("prompt", ""))
                if title:
                    send({"type": "title", "content": title})
            elif req_type == "chat":
                prompt = req.get("prompt", "")
                mode = req.get("mode", "ask")
                print(f"[biomni-bridge] chat mode={mode}", file=sys.stderr)
                # 每次 chat 前重置 A1 记忆（防跨消息/跨课题污染）
                reset_agent_memory(agent)
                # 注入前端显式传来的对话历史（保留同课题连续对话的上文）
                history = req.get("history") or []
                ctx = build_history_context(history)
                if ctx:
                    prompt = ctx + "\n\n" + prompt
                if mode == "plan":
                    handle_plan(agent, prompt)
                elif mode == "act":
                    handle_act(agent, prompt)
                else:
                    handle_ask(agent, prompt)
        except Exception as e:  # noqa: BLE001
            send({"type": "error", "message": str(e)})


if __name__ == "__main__":
    main()
