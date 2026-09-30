"""Bound API context without losing tool/result pairing or the current question.

The budget uses a conservative character estimate (one token per character), plus
image allowances. Old full turns are replaced with answer excerpts and session IDs;
original messages/notes remain on disk for display and read_previous_notes.
"""
import copy
import json
import re

CONTEXT_BUDGET = 24000
SUMMARY_LIMIT = 2400
IMAGE_COST = 1600


def _cost(message):
    count = len(json.dumps(message, ensure_ascii=False))
    if isinstance(message.get("content"), list):
        count += IMAGE_COST * sum(p.get("type") in ("image_ref", "image_path", "image_url") for p in message["content"])
    return count


def _question(message):
    if message.get("role") != "user":
        return False
    content = message.get("content")
    return not isinstance(content, list) or not any(p.get("type") == "image_path" for p in content)


def compact_history(history, budget=CONTEXT_BUDGET):
    history = copy.deepcopy(history)
    groups = []
    summary = ""
    for m in history:
        if m.get("context_summary"):
            summary = m.get("content", "")
            continue
        if _question(m) or not groups:
            groups.append([])
        groups[-1].append(m)
    if not groups:
        return []
    # Keep image files, but only send images from the current research turn.
    for group in groups[:-1]:
        for m in group:
            if isinstance(m.get("content"), list):
                m["content"] = [p for p in m["content"] if p.get("type") not in ("image_ref", "image_path", "image_url")]
                if not m["content"]:
                    m["content"] = "（历史图片保存在原对话中，可按需重新提供。）"
    # Bound bulky tool outputs even within the current turn; never trim call IDs/arguments.
    for group in groups:
        for m in group:
            if m.get("role") == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > 6000:
                m["content"] = m["content"][:6000] + "\n（结果已截断，可用 read_previous_notes 重读。）"
    def cost():
        return sum(_cost(m) for group in groups for m in group) + len(summary)
    while len(groups) > 1 and cost() > budget:
        old = groups.pop(0)
        excerpts = []
        for m in old:
            c = m.get("content")
            if m.get("role") in ("user", "assistant") and isinstance(c, str) and c:
                excerpts.append(f"{m['role']}: {c[:500]}")
            elif m.get("role") == "tool" and isinstance(c, str):
                sessions = re.findall(r"SESSION\s+([\w-]+)", c)
                links = re.findall(r"https?://[^\s)]+", c)
                if sessions or links:
                    excerpts.append("证据索引：" + " ".join([*("SESSION " + s for s in sessions), *links[:3]]))
        summary = (summary + "\n" + "\n".join(excerpts))[-SUMMARY_LIMIT:]
    # A long current turn can exceed the budget by itself. Distribute available
    # context across tool results rather than breaking their protocol relationships.
    tools = [m for group in groups for m in group if m.get("role") == "tool"]
    if tools and cost() > budget:
        overhead = cost() - sum(len(m.get("content", "")) for m in tools)
        allowance = max(0, (budget - overhead - 60 * len(tools)) // len(tools))
        for m in tools:
            if len(m["content"]) > allowance:
                m["content"] = m["content"][:allowance] + "\n（上下文预算已满，可重读原笔记。）"
    out = [m for group in groups for m in group]
    if summary:
        out.insert(0, {"role": "user", "context_summary": True,
                       "content": "（早期对话摘要，原始证据可用 read_previous_notes 重读）\n" + summary})
    if sum(_cost(m) for m in out) > budget:
        # Preserve the original question; never silently send a mutilated prompt.
        raise RuntimeError("本轮内容超过上下文预算，请减少图片或缩短问题后重新发送")
    return out


def api_messages(history):
    return [{k: v for k, v in m.items() if k != "context_summary"} for m in history]
