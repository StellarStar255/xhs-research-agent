"""Chat agent. Each chat is answered by one backend, fixed when the chat is created:

  "claude": `claude -p` (the user's own Claude Code login) with a system prompt that
            lets it run only the xhs scraper; its stream-json output is folded into
            the chat record.
  "api":    an OpenAI-compatible API with the user's own key (see llm.py).

Chats live at data/chats/<id>.json (+ data/chats/<id>/ for pasted images).
A message's `parts` are, in order, the agent's text blocks and tool steps:
  {"type": "text", "text": ...}
  {"type": "search", "keyword": ..., "tool_id": ..., "status": running|done|error|limited, "session": ...}
  {"type": "read", "session": ..., "tool_id": ..., "status": ...}
"""
import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import paths, procutil, settings
from .paths import DATA_DIR

CHATS_DIR = DATA_DIR / "chats"
MODEL = os.environ.get("XHS_AGENT_MODEL")  # e.g. "sonnet" for faster answers
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
DEFAULT_IMAGE_PROMPT = "请看看这张图片，结合小红书上的信息帮我分析一下。"

_TEMPLATE = (Path(__file__).parent / "agent_prompt.md").read_text()
CLAUDE_TOOLS = """抓取命令（通过 Bash 运行，每次把 timeout 设为 600000）：

```
{XHS} research "<搜索关键词>" -q "<用户的原始问题>" [-n 笔记数，默认8] [-c 每篇评论数，默认15]
```

它会输出 `SESSION <id>` 以及每篇笔记的标题、作者、日期、赞/藏/评数、链接、正文和热门评论。
之前抓过的结果可以用 `{XHS} digest <id>` 重新读取，不必重新抓。"""


def system_prompt(tools_text):
    return _TEMPLATE.replace("{TOOLS}", tools_text).replace("{DATE}", time.strftime("%Y-%m-%d"))


_lock = threading.RLock()
_runs = {}  # chat id -> running turn (ClaudeRun or llm.ApiRun)


class ClaudeRun:
    def __init__(self, proc):
        self.proc = proc
        self.stopping = False

    def alive(self):
        return self.proc.poll() is None

    def stop(self):
        if self.alive():
            self.stopping = True
            procutil.kill_tree(self.proc)


# ---------- storage ----------

def _path(cid):
    if not re.fullmatch(r"[\w-]+", cid):
        raise ValueError("bad chat id")
    return CHATS_DIR / f"{cid}.json"


def image_dir(cid):
    return _path(cid).with_suffix("")


def load(cid):
    return json.loads(_path(cid).read_text())


def _save(chat):
    CHATS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(chat["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(chat, ensure_ascii=False, indent=1))
    tmp.replace(_path(chat["id"]))


def update(cid, fn):
    """Apply fn(chat, last_message) under the lock and persist."""
    with _lock:
        chat = load(cid)
        fn(chat, chat["messages"][-1])
        _save(chat)


def list_chats():
    if not CHATS_DIR.exists():
        return []
    out = []
    for f in sorted(CHATS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        c = json.loads(f.read_text())
        out.append({"id": c["id"], "title": c["title"], "updated": c.get("updated"),
                    "backend": c.get("backend", "claude"), "running": is_running(c["id"])})
    return out


def delete(cid):
    stop(cid)
    _path(cid).unlink(missing_ok=True)
    shutil.rmtree(image_dir(cid), ignore_errors=True)


def is_running(cid):
    r = _runs.get(cid)
    return r is not None and r.alive()


def stop(cid):
    r = _runs.get(cid)
    if r:
        r.stop()


# ---------- turns ----------

def send(cid, text, images=()):
    """Start a turn. Returns the chat id (creating a chat if cid is None).

    images: [{"media_type": "image/png", "data": <base64>}] — saved next to the chat
    for display and passed to the model as image input.
    """
    from . import llm  # avoid a circular import at module load

    with _lock:
        if cid:
            chat = load(cid)
            if is_running(cid):
                raise RuntimeError("上一个问题还在处理中")
        else:
            conf = settings.load()
            if conf["backend"] == "api" and not settings.api_ready(conf):
                raise RuntimeError("还没有配置大模型 API，请先点左下角「设置」填写 API Key")
            if conf["backend"] == "claude" and not settings.claude_available():
                raise RuntimeError("这台电脑上没有找到 Claude Code，请在「设置」里改用大模型 API")
            cid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
            title = text.strip().splitlines()[0][:30] if text.strip() else "图片分析"
            chat = {"id": cid, "title": title, "backend": conf["backend"], "claude_session": None,
                    "created": time.strftime("%Y-%m-%d %H:%M"), "messages": []}
        names = []
        for img in images:
            d = image_dir(cid)
            d.mkdir(parents=True, exist_ok=True)
            name = f"{len(chat['messages']):03d}_{len(names)}.{EXT[img['media_type']]}"
            (d / name).write_bytes(base64.b64decode(img["data"]))
            names.append(name)
        chat["messages"].append({"role": "user", "text": text, "images": names})
        chat["messages"].append({"role": "assistant", "parts": [], "draft": "", "status": "running",
                                 "started": time.time(), "model": _model_label(chat)})
        chat["updated"] = time.strftime("%Y-%m-%d %H:%M")
        _save(chat)

        if chat.get("backend", "claude") == "api":
            _runs[cid] = llm.start(cid)
        else:
            _runs[cid] = _start_claude(chat, text, images)
    return cid


def _model_label(chat):
    if chat.get("backend", "claude") == "claude":
        return f"Claude Code{' · ' + MODEL if MODEL else ''}"
    api = settings.load()["api"]
    name = next((p["name"] for p in settings.PROVIDERS if p["id"] == api.get("provider")), "")
    return f"{name.split('（')[0]} · {api.get('model')}" if name and api.get("provider") != "custom" else api.get("model", "")


def _claude_projects_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def _ensure_resumable(session_id, cwd):
    """Claude Code keeps sessions per working directory (~/.claude/projects/<cwd with every
    non-alphanumeric char turned into '-'>/<id>.jsonl), and --resume only looks in the
    current one. Chats made elsewhere (source runs in the repo, or copied over from them)
    would fail with error_during_execution, so copy the transcript across. False if the
    session isn't anywhere."""
    base = _claude_projects_dir()
    target = base / re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd)) / f"{session_id}.jsonl"
    if target.exists():
        return True
    found = next(base.glob(f"*/{session_id}.jsonl"), None)
    if not found:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(found, target)
    return True


def _transcript(chat, limit=6000):
    """Earlier turns as plain text, to seed a fresh session when the old one is gone."""
    lines = []
    for m in chat["messages"][:-2]:  # everything before the turn being started
        if m["role"] == "user":
            lines.append(f"用户：{m.get('text') or '（发送了图片）'}")
        else:
            answer = next((p["text"] for p in reversed(m.get("parts") or []) if p["type"] == "text"), "")
            if answer:
                lines.append(f"助手：{answer[:1500]}")
    text = "\n\n".join(lines)
    return text[-limit:]


def _user_images(cid, names):
    out = []
    for n in names or []:
        f = image_dir(cid) / n
        mime = next((k for k, v in EXT.items() if f.suffix == "." + v), "image/png")
        if f.exists():
            out.append({"media_type": mime, "data": base64.b64encode(f.read_bytes()).decode()})
    return out


def _start_claude(chat, text, images):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if chat.get("claude_session") and not _ensure_resumable(chat["claude_session"], DATA_DIR):
        chat["claude_session"] = None  # the old session is gone: carry the context over as text
    if not chat.get("claude_session") and len(chat["messages"]) > 2:
        history = _transcript(chat)
        if history:
            text = (f"（这是继续之前的对话。之前的对话记录如下，供参考：）\n\n{history}\n\n"
                    f"（以上是之前的对话。）用户现在说：{text or DEFAULT_IMAGE_PROMPT}")
    content = [{"type": "image", "source": {"type": "base64", "media_type": i["media_type"], "data": i["data"]}}
               for i in images]
    content.append({"type": "text", "text": text or DEFAULT_IMAGE_PROMPT})
    xhs = paths.xhs_script()
    cmd = [settings.find_claude() or "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--system-prompt", system_prompt(CLAUDE_TOOLS.replace("{XHS}", xhs)),
           "--tools", "Bash", "--allowedTools", f"Bash({xhs} research:*)", f"Bash({xhs} digest:*)",
           "--strict-mcp-config"]
    if MODEL:
        cmd += ["--model", MODEL]
    if chat.get("claude_session"):
        cmd += ["--resume", chat["claude_session"]]
    env = paths.child_env({"BASH_DEFAULT_TIMEOUT_MS": "600000", "BASH_MAX_TIMEOUT_MS": "900000"})
    env.pop("CLAUDECODE", None)
    # claude may be installed via node; make sure its own bin dir is on PATH.
    env["PATH"] = os.pathsep.join([str(Path(cmd[0]).parent), env.get("PATH", "")])
    proc = procutil.popen(cmd, cwd=DATA_DIR, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, bufsize=1)
    proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n")
    proc.stdin.close()
    threading.Thread(target=_pump, args=(chat["id"], proc), daemon=True).start()
    return ClaudeRun(proc)


# ---------- tool steps (shared by both backends) ----------

def apply_tool_output(step, out, is_error=False):
    """Fill a search/read step from the scraper CLI's output."""
    m = re.search(r"SESSION (\S+)", out)
    if m:
        step["session"] = m.group(1)
    n = re.search(r"笔记数: (\d+)", out)
    if n:
        step["count"] = int(n.group(1))
    lim = re.search(r"LIMITED: ([^\n]*?)(?: 请不要|$)", out, re.M)
    failed = is_error or "ERROR" in out[:300] or (not m and step["type"] == "search")
    if "NOT_LOGGED_IN" in out:
        step["status"], step["error"] = "login", "小红书未登录或登录已过期"
    elif lim:
        step["status"], step["error"] = "limited", lim.group(1)
    else:
        step["status"] = "error" if failed else "done"
        if failed:
            step["error"] = out[-300:]


def _tool_step(block):
    command = (block.get("input") or {}).get("command", "")
    try:
        args = shlex.split(command)
    except ValueError:
        args = command.split()
    step = {"tool_id": block.get("id"), "status": "running"}
    if "research" in args:
        i = args.index("research")
        step.update(type="search", keyword=args[i + 1] if i + 1 < len(args) else "")
    elif "digest" in args:
        i = args.index("digest")
        step.update(type="read", session=args[i + 1] if i + 1 < len(args) else "")
    else:
        step.update(type="tool", command=command[:120])
    return step


def _result_text(block):
    c = block.get("content")
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
    return str(c or "")


def claude_error(kind, text):
    """Readable message for Claude Code's own failures (not logged in, quota…)."""
    low = f"{kind} {text}".lower()
    if any(k in low for k in ("authentication", "not logged in", "/login", "invalid api key", "oauth")):
        return ("这台电脑上的 Claude Code 还没有登录（或登录已过期）。请打开终端运行 `claude`，按提示登录后再试；"
                "也可以在左下角「设置」里改用大模型 API。")
    if any(k in low for k in ("usage limit", "rate_limit", "rate limit", "limit reached", "overloaded")):
        return f"Claude 的用量额度暂时用完了或服务繁忙，请稍后再试，或者在「设置」里改用大模型 API。（{text}）"
    if any(k in low for k in ("billing", "credit", "subscription")):
        return f"Claude 账户的订阅或余额有问题，请检查 Claude 账户。（{text}）"
    return text


def _handle(chat, msg, ev):
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        chat["claude_session"] = ev.get("session_id")
    elif t == "stream_event":
        e = ev.get("event") or {}
        if e.get("type") == "content_block_delta" and (e.get("delta") or {}).get("type") == "text_delta":
            msg["draft"] += e["delta"]["text"]
    elif t == "assistant":
        if ev.get("error"):  # e.g. not logged in: the "reply" is really an error message
            text = " ".join(b.get("text", "") for b in (ev.get("message") or {}).get("content") or [])
            msg["claude_error"] = claude_error(ev["error"], text)
            return
        for b in (ev.get("message") or {}).get("content") or []:
            if b.get("type") == "text" and b.get("text", "").strip():
                msg["parts"].append({"type": "text", "text": b["text"]})
                msg["draft"] = ""
            elif b.get("type") == "tool_use":
                msg["draft"] = ""
                msg["parts"].append(_tool_step(b))
    elif t == "user":
        content = (ev.get("message") or {}).get("content")
        for b in content if isinstance(content, list) else []:
            if b.get("type") != "tool_result":
                continue
            for p in msg["parts"]:
                if p.get("tool_id") == b.get("tool_use_id"):
                    apply_tool_output(p, _result_text(b), b.get("is_error"))
    elif t == "result":
        msg["draft"] = ""
        if ev.get("session_id"):
            chat["claude_session"] = ev["session_id"]
        if ev.get("is_error") or ev.get("subtype") != "success":
            msg["status"] = "error"
            msg["error_kind"] = ev.get("subtype")
            msg["error"] = msg.pop("claude_error", None) or claude_error("", str(ev.get("result") or ev.get("subtype")))
        else:
            msg["status"] = "done"
            if not any(p["type"] == "text" for p in msg["parts"]) and ev.get("result"):
                msg["parts"].append({"type": "text", "text": ev["result"]})


def finish(cid, stopped=False, error=None):
    """Close out a turn: settle dangling steps, set final status and elapsed time."""
    def fn(chat, msg):
        msg["draft"] = ""
        for p in msg["parts"]:
            if p.get("status") == "running":
                p["status"] = "error"
        if msg["status"] == "running":
            msg["status"] = "stopped" if stopped else ("error" if error else "done")
            if error:
                msg["error"] = error
        msg["elapsed"] = round(time.time() - msg.get("started", time.time()))
    update(cid, fn)


def _pump(cid, proc):
    noise = []  # non-JSON output (stderr is merged into stdout)
    for line in proc.stdout:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            noise = (noise + [line.rstrip()])[-20:]
            continue
        if ev.get("type") == "stream_event":
            # Only text deltas matter (skip thinking / tool-input deltas).
            d = ((ev.get("event") or {}).get("delta") or {}).get("text")
            if not d:
                continue
        update(cid, lambda chat, msg: _handle(chat, msg, ev))
    proc.wait()
    stopped = getattr(_runs.get(cid), "stopping", False)
    if not stopped and _retry_without_resume(cid):
        return
    finish(cid, stopped=stopped, error=None if stopped else ("\n".join(noise).strip()[-500:] or f"exit {proc.returncode}"))


def _retry_without_resume(cid):
    """If resuming the Claude session failed before any output, start once more as a new
    session seeded with the conversation so far. True if a retry was started."""
    with _lock:
        chat = load(cid)
        msg = chat["messages"][-1]
        if not (chat.get("claude_session") and msg.get("error_kind") == "error_during_execution"
                and not msg.get("parts") and not msg.get("retried")):
            return False
        user = chat["messages"][-2]
        chat["claude_session"] = None
        msg.update(status="running", error=None, error_kind=None, retried=True, draft="")
        _save(chat)
        _runs[cid] = _start_claude(chat, user.get("text") or "", _user_images(cid, user.get("images")))
    return True


def recover():
    """Mark turns left running by a previous server process as interrupted."""
    if not CHATS_DIR.exists():
        return
    for f in CHATS_DIR.glob("*.json"):
        chat = json.loads(f.read_text())
        msg = chat["messages"][-1] if chat["messages"] else {}
        if msg.get("status") == "running":
            msg["status"] = "error"
            msg["error"] = "服务重启，本轮被中断"
            _save(chat)
