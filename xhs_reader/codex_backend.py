"""Chat backend using the local OpenAI Codex CLI (`codex exec --json`), e.g. signed in with
a ChatGPT account.

Codex can't be limited to specific shell commands the way Claude Code can, so it gets
no shell at all: the scraper is exposed as an MCP server (`xhs-cli mcp`, see
mcp_server.py) whose tools are auto-approved, the sandbox is read-only, the user's own
Codex config (other MCP servers, plugins) is ignored, and agentic features are off.
Follow-up turns continue the Codex thread with `codex exec resume <thread_id>`.
"""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from . import agent, paths, procutil, settings
from .llm import TOOLS_TEXT

# Everything besides our MCP tools that could act on the machine or the web.
DISABLED_FEATURES = ["shell_tool", "browser_use", "browser_use_external", "computer_use", "in_app_browser",
                     "apps", "plugins", "image_generation", "multi_agent", "goals"]


_known_features = {}  # codex path -> set of feature names it knows


def _features_to_disable(codex):
    """Only pass --disable for features this Codex version has (unknown names are an error)."""
    if codex not in _known_features:
        try:
            out = subprocess.run([codex, "features", "list"], capture_output=True, text=True, timeout=20).stdout
            _known_features[codex] = {line.split()[0] for line in out.splitlines() if line.strip()}
        except Exception:
            _known_features[codex] = set()
    return [f for f in DISABLED_FEATURES if f in _known_features[codex]]


def _toml(value):
    # JSON strings/arrays are valid TOML basic strings/arrays (ensure_ascii → \\uXXXX escapes).
    return json.dumps(value)


class CodexRun:
    def __init__(self, proc):
        self.proc, self.stopping = proc, False

    def alive(self):
        return self.proc.poll() is None

    def stop(self):
        if self.alive():
            self.stopping = True
            procutil.kill_tree(self.proc)


def _mcp_config():
    if paths.FROZEN:
        command, args = str(paths.cli_executable()), ["mcp"]
        env = {"XHS_DATA_DIR": str(paths.DATA_DIR)}
    else:
        command, args = sys.executable, ["-m", "xhs_reader.cli", "mcp"]
        env = {"XHS_DATA_DIR": str(paths.DATA_DIR), "PYTHONPATH": str(paths.ROOT)}
    env_toml = "{" + ", ".join(f"{k}={_toml(v)}" for k, v in env.items()) + "}"
    return ["-c", f"mcp_servers.xhs.command={_toml(command)}",
            "-c", f"mcp_servers.xhs.args={_toml(args)}",
            "-c", f"mcp_servers.xhs.env={env_toml}",
            "-c", 'mcp_servers.xhs.default_tools_approval_mode="approve"',
            "-c", "mcp_servers.xhs.startup_timeout_sec=60",
            "-c", "mcp_servers.xhs.tool_timeout_sec=900"]


def start(chat, text, image_names):
    codex = settings.find_codex() or "codex"
    thread = chat.get("codex_thread")
    cmd = [codex, "exec"] + (["resume", thread] if thread else []) + [
        "--json", "--skip-git-repo-check", "--ignore-user-config",
        "-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"',
        "-c", f"developer_instructions={_toml(agent.system_prompt(TOOLS_TEXT))}",
    ]
    for feature in _features_to_disable(codex):
        cmd += ["--disable", feature]
    cmd += _mcp_config()
    for name in image_names or []:
        cmd += ["-i", str(agent.image_dir(chat["id"]) / name)]
    cmd.append(text or agent.DEFAULT_IMAGE_PROMPT)

    env = paths.child_env()
    env["PATH"] = os.pathsep.join([str(Path(codex).parent), env.get("PATH", "")])
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    proc = procutil.popen(cmd, cwd=paths.DATA_DIR, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, bufsize=1)
    threading.Thread(target=_pump, args=(chat["id"], proc), daemon=True).start()
    return CodexRun(proc)


def codex_error(text):
    low = (text or "").lower()
    if any(k in low for k in ("not logged in", "401", "unauthorized", "codex login", "please log in", "authentication")):
        return "这台电脑上的 Codex 还没有登录（或登录已过期）。请打开终端运行 `codex login` 登录后再试；也可以在「设置」里换别的模型。"
    if any(k in low for k in ("403", "stream disconnected", "network", "timed out", "connection", "proxy")):
        return (f"连不上 OpenAI 的服务（{text.strip()[:80]}）。如果你平时要通过代理才能使用 Codex，"
                "请确认代理软件已开启「系统代理」；也可以在「设置」里换别的模型。")
    if any(k in low for k in ("usage limit", "rate limit", "quota")):
        return f"Codex 的用量额度暂时用完了，请稍后再试。（{text.strip()[:120]}）"
    return (text or "Codex 运行失败").strip()[:500]


def _tool_text(item):
    res = item.get("result") or {}
    parts = res.get("content") or []
    return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict)), bool(res.get("isError"))


def _handle(chat, msg, ev):
    t = ev.get("type")
    item = ev.get("item") or {}
    kind = item.get("type")
    if t == "thread.started":
        chat["codex_thread"] = ev.get("thread_id")
    elif kind == "agent_message" and t == "item.completed" and (item.get("text") or "").strip():
        msg["parts"].append({"type": "text", "text": item["text"]})
    elif kind == "mcp_tool_call":
        args = item.get("arguments") or {}
        if t == "item.started":
            if item.get("tool") == "search_xiaohongshu":
                msg["parts"].append({"type": "search", "keyword": str(args.get("keyword", "")),
                                     "tool_id": item.get("id"), "status": "running"})
            else:
                msg["parts"].append({"type": "read", "session": str(args.get("session", "")),
                                     "tool_id": item.get("id"), "status": "running"})
        elif t == "item.completed":
            step = next((p for p in msg["parts"] if p.get("tool_id") == item.get("id")), None)
            if step:
                if item.get("status") == "completed":
                    text, is_error = _tool_text(item)
                    if step["type"] == "search":
                        agent.apply_tool_output(step, text, is_error)
                    else:
                        step["status"] = "error" if is_error else "done"
                else:
                    step["status"], step["error"] = "error", str((item.get("error") or {}).get("message", ""))
    elif t == "turn.completed":
        u = ev.get("usage") or {}
        msg["usage"] = {"prompt": u.get("input_tokens", 0), "completion": u.get("output_tokens", 0)}
        msg["status"] = "done"
    elif t in ("turn.failed", "error"):
        err = ev.get("error") or {}
        msg["status"] = "error"
        msg["error"] = codex_error(err.get("message") if isinstance(err, dict) else str(ev.get("message") or err))


def _pump(cid, proc):
    stderr_tail = []

    def drain():  # keep stderr from filling up; remember the tail for error messages
        for line in proc.stderr:
            stderr_tail.append(line.rstrip())
            del stderr_tail[:-30]
    threading.Thread(target=drain, daemon=True).start()

    for line in proc.stdout:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        agent.update(cid, lambda chat, msg: _handle(chat, msg, ev))
    proc.wait()
    run = agent._runs.get(cid)
    stopped = getattr(run, "stopping", False)
    err = None
    if not stopped and proc.returncode:
        useful = [ln for ln in stderr_tail if "codex_models_manager" not in ln]  # benign model-list noise
        err = codex_error("\n".join(useful[-5:]) or f"exit {proc.returncode}")
    agent.finish(cid, stopped=stopped, error=err)
