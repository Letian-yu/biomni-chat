#!/usr/bin/env python3
"""对 biomni site-packages 的 agent/a1.py 应用 biomni-chat 补丁（幂等，可重复执行）。

补丁内容（方案2：会话级记忆 thread 隔离）：
  1. thread_id 参数化：go / go_stream 从 agent._thread_id 读取（默认 42），
     使 bridge 能按会话设置不同 thread → 多会话记忆隔离。
  2. 跨调用历史恢复：go / go_stream 先用 get_state 恢复该 thread 的历史消息，
     再追加新 prompt（否则 LangGraph 会用 inputs 覆盖恢复的历史 → 跨轮记忆丢失）。

用法：
  python scripts/patch_a1.py                     # 自动定位 site-packages a1.py
  python scripts/patch_a1.py /path/to/a1.py      # 指定文件
"""
import os
import sys

# ---------------- 补丁内容 ----------------
# 已打补丁的标识（用于幂等检测）
_MARKER = "[biomni-chat patch]"

# 每个补丁: (描述, 替换对列表)
# 替换对: (old, new) —— old 为"未打补丁"文本，new 为"打补丁后"文本
_PATCHES = [
    {
        "desc": "go(): thread_id 参数化 + 跨调用历史恢复",
        "pairs": [
            # thread_id 参数化
            (
                '        inputs = {"messages": [HumanMessage(content=prompt)], "next_step": None}\n'
                '        config = {"recursion_limit": 500, "configurable": {"thread_id": 42}}\n',
                '        _tid = getattr(self, "_thread_id", 42)\n'
                '        config = {"recursion_limit": 500, "configurable": {"thread_id": _tid}}\n'
                '        # [biomni-chat patch] 跨调用恢复该 thread 历史并追加新消息：\n'
                '        # 若直接 inputs={"messages":[新消息]}，LangGraph 会用 inputs 覆盖 checkpointer 恢复的\n'
                '        # 历史 → 跨 go_stream 调用记忆丢失。这里先取历史，再追加当前 prompt。\n'
                '        try:\n'
                '            _snap = self.app.get_state(config)\n'
                '            _prev = list(_snap.values.get("messages", [])) if _snap and _snap.values else []\n'
                '        except Exception:\n'
                '            _prev = []\n'
                '        if _prev:\n'
                '            inputs = {"messages": _prev + [HumanMessage(content=prompt)], "next_step": None}\n'
                '        else:\n'
                '            inputs = {"messages": [HumanMessage(content=prompt)], "next_step": None}\n',
            ),
        ],
    },
    {
        "desc": "go_stream(): thread_id 参数化 + 跨调用历史恢复",
        "pairs": [
            (
                '        inputs = {"messages": [HumanMessage(content=prompt)], "next_step": None}\n'
                '        config = {"recursion_limit": 500, "configurable": {"thread_id": 42}}\n',
                '        _tid = getattr(self, "_thread_id", 42)\n'
                '        config = {"recursion_limit": 500, "configurable": {"thread_id": _tid}}\n'
                '        # [biomni-chat patch] 跨调用恢复该 thread 历史并追加新消息（见 go() 注释）\n'
                '        try:\n'
                '            _snap = self.app.get_state(config)\n'
                '            _prev = list(_snap.values.get("messages", [])) if _snap and _snap.values else []\n'
                '        except Exception:\n'
                '            _prev = []\n'
                '        if _prev:\n'
                '            inputs = {"messages": _prev + [HumanMessage(content=prompt)], "next_step": None}\n'
                '        else:\n'
                '            inputs = {"messages": [HumanMessage(content=prompt)], "next_step": None}\n',
            ),
        ],
    },
]


def locate_a1() -> str:
    """定位 site-packages 的 agent/a1.py。"""
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        return sys.argv[1]
    try:
        import biomni  # noqa: E402

        p = os.path.join(os.path.dirname(biomni.__file__), "agent", "a1.py")
        if os.path.exists(p):
            return p
    except ImportError:
        pass
    return "/data/biomni/envs/biomni_e1/lib/python3.11/site-packages/biomni/agent/a1.py"


def main() -> int:
    path = locate_a1()
    print(f"目标: {path}")
    if not os.path.exists(path):
        print(f"❌ 文件不存在: {path}")
        return 1

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    # 幂等检测：已有 marker 且已含 thread 参数化 → 已打补丁
    already = _MARKER in src and "getattr(self, \"_thread_id\", 42)" in src
    if already:
        print("✅ 已打补丁，跳过")
        return 0

    changed = False
    for patch in _PATCHES:
        for old, new in patch["pairs"]:
            if old in src:
                src = src.replace(old, new, 1)
                print(f"  应用: {patch['desc']}")
                changed = True
            else:
                print(f"  ⚠️ 未找到待替换文本: {patch['desc']}（可能已打过补丁或版本不同）")

    if not changed:
        print("❌ 无任何补丁被应用（文件版本与预期不符，请人工检查）")
        return 1

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print("✅ 补丁应用完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
