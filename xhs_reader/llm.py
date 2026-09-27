"""Agent loop for any OpenAI-compatible chat API (DeepSeek, 通义千问, Kimi, 智谱, OpenAI…).

The model gets the function tools from tools.py, which also enforce the per-turn
budgets (rather than trusting the prompt; smaller models follow instructions less
reliably). Conversation history (in OpenAI message format) is
kept in chat["llm_history"]; pasted images are stored there by file name and
re-inlined as data URLs when sent.
"""
import base64
import json
from pathlib import Path
import threading
import time

from . import agent, procutil, settings
from .tools import TOOLS, TOOLS_TEXT, ToolRunner

MAX_STEPS = 10           # model calls per turn

class ApiRun:
    def __init__(self, cid, turn_id=""):
        self.cid = cid
        self.turn_id = turn_id
        self.pending_images = []  # paths from view_note_images, sent after the tool results
        self.stopped = False
        self.proc = None
        self.tools = ToolRunner(turn_id, on_proc=lambda p: setattr(self, "proc", p))
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


def start(cid, turn_id=""):
    run = ApiRun(cid, turn_id)
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
                if p.get("type") in ("image_ref", "image_path"):
                    f = agent.image_dir(cid) / p["name"] if p["type"] == "image_ref" else Path(p["path"])
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


def run_turn(run):
    cid = run.cid
    chat = agent.load(cid)
    user = chat["messages"][-2]
    history = _repair(chat.get("llm_history") or [])
    history.append({"role": "user", "content": _user_content(cid, user["text"], user.get("images"))})
    cl, model = client()
    prompt = agent.system_prompt(TOOLS_TEXT)
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
                result = _call_tool(run, tc, user["text"])
            history.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
            agent.update(cid, save_history)
        if run.pending_images:  # tool messages can't carry images; send them as a user turn
            history.append({"role": "user", "content": [{"type": "text", "text": "（以下是 view_note_images 返回的笔记图片）"}]
                            + [{"type": "image_path", "path": str(p)} for p in run.pending_images]})
            run.pending_images = []
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


def _call_tool(run, tc, question):
    """Execute one tool call via the shared runner, mirroring it as a step in the chat."""
    cid, name = run.cid, tc["function"]["name"]
    try:
        args = json.loads(tc["function"]["arguments"] or "{}")
    except ValueError:
        args = {}
    step = agent.tool_step(name, args, tc["id"])
    agent.update(cid, lambda c, m: m["parts"].append(step))
    r = run.tools.call(name, args, question)
    if step["type"] == "images":
        run.pending_images += r["images"]
        text = r["text"] + ("（图片附在下一条消息里）" if r["images"] else "")
        agent.apply_images_output(step, r["text"], len(r["images"]), r["is_error"])
    else:
        text = r["text"]
        agent.apply_tool_output(step, text, r["is_error"])
    agent.update(cid, lambda c, m: _replace_step(m, step))
    return text[-60000:]


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
