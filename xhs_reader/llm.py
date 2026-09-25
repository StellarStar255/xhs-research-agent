"""Agent loop for any OpenAI-compatible chat API (DeepSeek, 通义千问, Kimi, 智谱, OpenAI…).

The model gets two function tools that wrap the scraper CLI. Per-turn budgets are
enforced here rather than trusted to the prompt, since smaller models follow
instructions less reliably. Conversation history (in OpenAI message format) is
kept in chat["llm_history"]; pasted images are stored there by file name and
re-inlined as data URLs when sent.
"""
import base64
import json
import subprocess
import threading
import time

from . import agent, paths, procutil, settings

MAX_STEPS = 10           # model calls per turn
MAX_SEARCHES = 3         # search_xiaohongshu calls per turn
MAX_NOTES = 24           # notes scraped per turn
DEFAULT_NOTES, MAX_NOTES_PER_SEARCH = 8, 12

TOOLS = [
    {"type": "function", "function": {
        "name": "search_xiaohongshu",
        "description": "在小红书搜索一个关键词，慢速抓取前几篇笔记的正文和热门评论（约 2–3 分钟）。"
                       "返回 SESSION id，以及每篇笔记的标题、作者、日期、赞/藏/评数、链接、正文和评论。",
        "parameters": {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "搜索关键词，用小红书用户会用的口语化中文"},
            "limit": {"type": "integer", "description": f"抓取笔记数，默认 {DEFAULT_NOTES}，最多 {MAX_NOTES_PER_SEARCH}"},
        }, "required": ["keyword"]}}},
    {"type": "function", "function": {
        "name": "read_previous_notes",
        "description": "重新读取之前某次搜索抓到的笔记（不访问小红书，很快）。",
        "parameters": {"type": "object", "properties": {
            "session": {"type": "string", "description": "之前搜索结果里给出的 SESSION id"},
        }, "required": ["session"]}}},
]

TOOLS_TEXT = f"""你有两个工具：

- `search_xiaohongshu(keyword, limit)`：在小红书搜索并抓取笔记（慢）。每轮对话最多调用 {MAX_SEARCHES} 次、合计最多 {MAX_NOTES} 篇。
- `read_previous_notes(session)`：重新读取之前抓过的笔记，不访问小红书。

工具返回里的笔记链接可以直接用于引用。"""


class ApiRun:
    def __init__(self, cid):
        self.cid = cid
        self.stopped = False
        self.proc = None
        self.thread = threading.Thread(target=self._main, daemon=True)

    def alive(self):
        return self.thread.is_alive()

    def stop(self):
        self.stopped = True
        p = self.proc
        if p and p.poll() is None:
            procutil.kill_tree(p)

    def _main(self):
        try:
            run_turn(self)
            agent.finish(self.cid, stopped=self.stopped)
        except Exception as e:  # surface API errors in the chat instead of dying silently
            agent.finish(self.cid, stopped=self.stopped, error=None if self.stopped else explain(e))


def start(cid):
    run = ApiRun(cid)
    run.thread.start()
    return run


def explain(e):
    """Turn SDK exceptions into something a non-programmer can act on."""
    import openai
    msg = str(getattr(e, "message", "") or e)
    if isinstance(e, openai.AuthenticationError):
        return "API Key 无效或已过期，请在「设置」里检查。"
    if isinstance(e, openai.PermissionDeniedError):
        return f"没有权限调用这个模型（可能需要实名认证或开通服务）：{msg}"
    if isinstance(e, openai.NotFoundError):
        return f"找不到这个模型或接口地址不对，请在「设置」里检查模型名和接口地址：{msg}"
    if isinstance(e, openai.RateLimitError):
        return f"API 限流或余额不足，请稍后再试或检查账户余额：{msg}"
    if isinstance(e, openai.APIConnectionError):
        return "连不上模型服务，请检查网络和接口地址。"
    if isinstance(e, openai.BadRequestError) and any(k in msg.lower() for k in ("image", "vision", "multimodal", "图片")):
        return f"当前模型可能不支持图片，请换一个支持看图的模型（设置里有提示）：{msg}"
    return f"{type(e).__name__}: {msg}"[:600]


def client(conf=None):
    from openai import OpenAI
    api = (conf or settings.load())["api"]
    return OpenAI(base_url=api["base_url"], api_key=api["api_key"], timeout=180, max_retries=2), api["model"]


def _user_content(cid, text, images):
    if not images:
        return text or agent.DEFAULT_IMAGE_PROMPT
    parts = [{"type": "text", "text": text or agent.DEFAULT_IMAGE_PROMPT}]
    parts += [{"type": "image_ref", "name": n} for n in images]
    return parts


def _inline_images(cid, history):
    """Replace stored image_ref parts with data URLs for sending."""
    out = []
    for m in history:
        if isinstance(m.get("content"), list):
            content = []
            for p in m["content"]:
                if p.get("type") == "image_ref":
                    f = agent.image_dir(cid) / p["name"]
                    mime = next((k for k, v in agent.EXT.items() if f.suffix == "." + v), "image/png")
                    data = base64.b64encode(f.read_bytes()).decode()
                    content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
                else:
                    content.append(p)
            m = {**m, "content": content}
        out.append(m)
    return out


def _repair(history):
    """Give every tool call a result; an interrupted turn can leave one dangling,
    and APIs reject histories like that."""
    out, answered = [], {m.get("tool_call_id") for m in history if m.get("role") == "tool"}
    for m in history:
        out.append(m)
        for tc in m.get("tool_calls") or []:
            if tc["id"] not in answered:
                out.append({"role": "tool", "tool_call_id": tc["id"], "content": "（这次调用被中断，没有结果）"})
    return out


def _run_cli(run, args):
    """Run the scraper CLI, killable via run.stop()."""
    run.proc = procutil.popen(paths.cli_command(*args), cwd=paths.DATA_DIR, env=paths.child_env(),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = run.proc.communicate(timeout=900)
    except subprocess.TimeoutExpired:
        run.stop()
        out = "ERROR: 抓取超时"
    code, run.proc = run.proc.returncode, None
    return out, code != 0


def run_turn(run):
    cid = run.cid
    chat = agent.load(cid)
    user = chat["messages"][-2]
    history = _repair(chat.get("llm_history") or [])
    history.append({"role": "user", "content": _user_content(cid, user["text"], user.get("images"))})
    cl, model = client()
    prompt = agent.system_prompt(TOOLS_TEXT)
    searches = notes = 0
    usage = {"prompt": 0, "completion": 0}

    def save_history(chat, msg):
        chat["llm_history"] = history
        msg["usage"] = dict(usage)

    for step in range(MAX_STEPS):
        if run.stopped:
            break
        last = step == MAX_STEPS - 1
        stream = _create(cl, model=model, stream=True, tools=TOOLS, tool_choice="none" if last else "auto",
                         messages=[{"role": "system", "content": prompt}] + _inline_images(cid, history))
        content, calls, flushed = "", {}, 0.0
        for chunk in stream:
            if run.stopped:
                stream.close()
                break
            if chunk.usage:
                usage["prompt"] += chunk.usage.prompt_tokens or 0
                usage["completion"] += chunk.usage.completion_tokens or 0
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                content += delta.content
                if time.time() - flushed > 0.3:  # throttle disk writes while streaming
                    flushed = time.time()
                    agent.update(cid, lambda c, m, t=content: m.update(draft=t))
            for tc in delta.tool_calls or []:
                c = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                c["id"] = tc.id or c["id"]
                if tc.function:
                    c["name"] += tc.function.name or ""
                    c["args"] += tc.function.arguments or ""

        assistant = {"role": "assistant", "content": content}
        if calls:
            assistant["tool_calls"] = [
                {"id": c["id"] or f"call_{step}_{i}", "type": "function",
                 "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                for i, c in sorted(calls.items())]
        history.append(assistant)

        def add_text(chat, msg, text=content):
            msg["draft"] = ""
            if text.strip():
                msg["parts"].append({"type": "text", "text": text})
            save_history(chat, msg)
        agent.update(cid, add_text)
        if not calls or run.stopped:
            break

        for tc in assistant["tool_calls"]:
            if run.stopped:
                result = "已被用户停止。"
            else:
                result, searches, notes = _call_tool(run, tc, user["text"], searches, notes)
            history.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
            agent.update(cid, save_history)


_no_usage_option = set()  # base_urls that rejected stream_options


def _create(cl, **kw):
    """chat.completions.create, asking for token usage where the provider allows it."""
    import openai
    if str(cl.base_url) not in _no_usage_option:
        try:
            return cl.chat.completions.create(stream_options={"include_usage": True}, **kw)
        except openai.BadRequestError as e:
            if "stream_options" not in str(e):
                raise
            _no_usage_option.add(str(cl.base_url))
    return cl.chat.completions.create(**kw)


def _call_tool(run, tc, question, searches, notes):
    """Execute one tool call; returns (result_text, searches, notes)."""
    cid, name = run.cid, tc["function"]["name"]
    try:
        args = json.loads(tc["function"]["arguments"] or "{}")
    except ValueError:
        args = {}

    if name == "search_xiaohongshu":
        step = {"type": "search", "keyword": str(args.get("keyword", "")).strip(), "tool_id": tc["id"], "status": "running"}
        room = MAX_NOTES - notes
        if searches >= MAX_SEARCHES or room <= 0:
            step.update(status="limited", error="本轮搜索次数已用完")
            agent.update(cid, lambda c, m: m["parts"].append(step))
            return (f"本轮已经搜索了 {searches} 次、{notes} 篇笔记，达到上限，不能再搜。请用已有信息回答。", searches, notes)
        if not step["keyword"]:
            return "缺少 keyword 参数。", searches, notes
        try:
            limit = int(args.get("limit") or DEFAULT_NOTES)
        except (TypeError, ValueError):
            limit = DEFAULT_NOTES
        limit = max(1, min(limit, MAX_NOTES_PER_SEARCH, room))
        agent.update(cid, lambda c, m: m["parts"].append(step))
        out, failed = _run_cli(run, ["research", step["keyword"], "-q", question or step["keyword"], "-n", str(limit)])
        agent.apply_tool_output(step, out, failed)
        agent.update(cid, lambda c, m: _replace_step(m, step))
        return out[-60000:], searches + 1, notes + (step.get("count") or 0)

    if name == "read_previous_notes":
        sid = str(args.get("session", "")).strip()
        step = {"type": "read", "session": sid, "tool_id": tc["id"], "status": "running"}
        agent.update(cid, lambda c, m: m["parts"].append(step))
        out, failed = _run_cli(run, ["digest", sid]) if sid else ("缺少 session 参数。", True)
        step["status"] = "error" if failed else "done"
        agent.update(cid, lambda c, m: _replace_step(m, step))
        return out[-60000:], searches, notes

    return f"没有叫 {name} 的工具。", searches, notes


def _replace_step(msg, step):
    for i, p in enumerate(msg["parts"]):
        if p.get("tool_id") == step["tool_id"]:
            msg["parts"][i] = step


def test(conf):
    """Quick connectivity check for the settings dialog."""
    cl, model = client(conf)
    r = cl.chat.completions.create(model=model, max_tokens=20,
                                   messages=[{"role": "user", "content": "只回复两个字：你好"}])
    return (r.choices[0].message.content or "").strip()


def list_models(conf):
    cl, _ = client(conf)
    return sorted(m.id for m in cl.models.list())
