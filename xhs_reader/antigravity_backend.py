"""Use the local, signed-in Antigravity CLI through ACP over stdio."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time

from . import agent, paths, procutil, settings
from .mcp_server import launch_spec
from .tools import TOOLS, TOOLS_TEXT

AGENT = "xhs-research"
TOOL_NAMES = {tool["function"]["name"] for tool in TOOLS}


def _server_name(turn_id):
    return "xhs_" + turn_id[:16]


def _write_workspace(root, turn_id):
    config = root / ".agents"
    agents = config / "agents"
    agents.mkdir(parents=True)
    command, args, env = launch_spec(turn_id)
    # Eager tools carry their schemas directly. Lazy tools require filesystem
    # access to the CLI's schema cache and the generic call_mcp_tool wrapper.
    mcp = {"mcpServers": {_server_name(turn_id): {
        "command": command, "args": args, "env": env, "timeoutSeconds": 900,
        "tools": {name: {"eager": True} for name in sorted(TOOL_NAMES)},
    }}}
    (config / "mcp_config.json").write_text(json.dumps(mcp), encoding="utf-8")
    (agents / f"{AGENT}.md").write_text(
        f"---\nname: {AGENT}\ndescription: Xiaohongshu research assistant\n"
        "mainAgent: true\nsubagent: false\nexcludeDefaultComponents: true\n"
        'commandExecutionPolicy: "off"\n---\n'
        + agent.system_prompt(TOOLS_TEXT), encoding="utf-8")


def start(chat, text, images, turn_id):
    executable = settings.find_antigravity()
    if not executable:
        raise RuntimeError("未检测到 Antigravity CLI（agy），请先安装并在终端登录")
    prompt = text or agent.DEFAULT_IMAGE_PROMPT
    if context := agent._transcript(chat):
        prompt = f"此前的对话：\n{context}\n\n当前问题：\n{prompt}"
    content = [{"type": "text", "text": prompt}]
    content.extend({"type": "image", "mimeType": image["media_type"], "data": image["data"]}
                   for image in images or [])
    workspace = tempfile.TemporaryDirectory(prefix="xhs-agy-")
    try:
        root = Path(workspace.name)
        _write_workspace(root, turn_id)
        env = paths.child_env()
        env["PATH"] = os.pathsep.join((str(Path(executable).parent), env.get("PATH", "")))
        proc = procutil.popen([executable, "acp", "--agent", AGENT, "--sandbox"], cwd=root, env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, bufsize=1)
    except BaseException:
        workspace.cleanup()
        raise

    def pump(cid, proc, run):
        _pump(cid, proc, run, workspace, content, turn_id)

    return agent.ClaudeRun(proc, pump, chat["id"])


def _error(text):
    if re.search(r"authentication required|not (?:signed|logged) in|unauthenticated|unauthorized|\b401\b",
                 text, re.I):
        return "Antigravity CLI 尚未登录或登录已过期。请在终端运行 agy，以 Google 账号登录后再试。"
    return (text.strip() or "Antigravity CLI 未返回完整结果")[-500:]


def _pump(cid, proc, run, workspace, content, turn_id):
    errors = []

    def drain():
        for line in proc.stderr:
            errors.append(line.strip())
            del errors[:-8]

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timeout = threading.Event()

    def expire():
        timeout.set()
        procutil.kill_tree(proc)

    timer = threading.Timer(900, expire)
    timer.daemon = True
    timer.start()
    error = None
    acp = Conversation(cid, proc, turn_id)
    try:
        acp.ask(workspace.name, content)
    except Exception as exc:
        reader.join(timeout=0.2)
        detail = "\n".join([str(exc), *errors])
        if not run.stopping:
            error = "Antigravity CLI 等待超时，请重试。" if timeout.is_set() else _error(detail)
    finally:
        timer.cancel()
        try:
            try:
                proc.stdin.close()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                procutil.kill_tree(proc)
                proc.wait()
            proc.stdout.close()
            reader.join(timeout=1)
            if not reader.is_alive():
                proc.stderr.close()
            acp.flush_text()
        finally:
            try:
                workspace.cleanup()
            finally:
                agent.finish(cid, stopped=run.stopping, error=error, run=run)


class Conversation:
    def __init__(self, cid, proc, turn_id):
        self.cid = cid
        self.proc = proc
        self.server = _server_name(turn_id)
        self.session = None
        self.sequence = 0
        self.tool_names = {prefix + name: name for name in TOOL_NAMES for prefix in (
            f"mcp__{self.server}__", f"mcp_{self.server}_", f"{self.server}/")}
        self.pending = {}
        self.draft = ""
        self.flushed = 0.0

    def send(self, **payload):
        print(json.dumps({"jsonrpc": "2.0", **payload}), file=self.proc.stdin, flush=True)

    def request(self, method, **params):
        self.sequence += 1
        rid = self.sequence
        self.send(id=rid, method=method, params=params)
        for line in self.proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if "method" in event:
                self.handle(event)
            elif event.get("id") == rid:
                if error := event.get("error"):
                    raise RuntimeError(error.get("message") or str(error))
                return event["result"]
        raise RuntimeError("Antigravity CLI 连接中断，请确认已安装支持 ACP 的版本")

    def ask(self, cwd, content):
        hello = self.request("initialize", protocolVersion=1, clientCapabilities={},
                             clientInfo={"name": "xhs-research-agent", "version": "1"})
        if hello.get("protocolVersion") != 1:
            raise RuntimeError("Antigravity CLI 的 ACP 协议版本不受支持")
        session = self.request("session/new", cwd=cwd, mcpServers=[])
        self.session = session["sessionId"]
        result = self.request("session/prompt", sessionId=self.session, prompt=content)
        if result.get("stopReason") != "end_turn":
            raise RuntimeError(f"Antigravity CLI 提前结束：{result.get('stopReason', 'unknown')}")

    def handle(self, event):
        method, params = event["method"], event.get("params") or {}
        if method == "session/request_permission":
            call = params.get("toolCall") or {}
            name = self.tool_name(call) or self.pending.get(call.get("toolCallId"))
            choice = next((o for o in params.get("options", []) if o.get("kind") ==
                           ("allow_once" if name else "reject_once")), None)
            outcome = {"outcome": "cancelled"}
            if choice and params.get("sessionId") == self.session:
                outcome = {"outcome": "selected", "optionId": choice["optionId"]}
            self.send(id=event["id"], result={"outcome": outcome})
        elif "id" in event:
            self.send(id=event["id"], error={"code": -32601, "message": "Method not supported"})
        elif method == "session/update" and params.get("sessionId") == self.session:
            self.update(params["update"])

    def tool_name(self, call):
        return self.tool_names.get(call.get("name")) or self.tool_names.get(call.get("title"))

    def flush_text(self):
        if self.draft:
            text = self.draft

            def commit(chat, msg):
                if text.strip():
                    msg["parts"].append({"type": "text", "text": text})
                msg["draft"] = ""

            agent.update(self.cid, commit)
        self.draft = ""

    def update(self, event):
        kind = event.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = event.get("content") or {}
            if content.get("type") == "text":
                self.draft += content["text"]
                if time.monotonic() - self.flushed >= 0.3:
                    agent.update(self.cid, lambda c, m: m.update(draft=self.draft))
                    self.flushed = time.monotonic()
        elif kind in ("tool_call", "tool_call_update"):
            self.update_tool(event)

    def update_tool(self, event):
        tid = event["toolCallId"]
        name = self.tool_name(event) or self.pending.get(tid)
        if not name:
            return
        self.flush_text()
        self.pending[tid] = name
        args = event.get("rawInput") or {}
        status = event.get("status")

        def apply(chat, msg):
            part = next((p for p in msg["parts"] if p.get("tool_id") == tid), None)
            if part is None:
                part = agent.tool_step(name, args if isinstance(args, dict) else {}, tid)
                msg["parts"].append(part)
            if status in ("completed", "failed"):
                blocks = [item["content"] for item in event.get("content") or []
                          if item.get("type") == "content" and isinstance(item.get("content"), dict)]
                raw = event.get("rawOutput")
                if not blocks and isinstance(raw, dict):
                    blocks = raw.get("content") or []
                text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                if not text and isinstance(raw, str):
                    text = raw
                failed = status == "failed" or (isinstance(raw, dict) and raw.get("isError"))
                if name == "view_note_images":
                    num = sum(b.get("type") == "image" for b in blocks)
                    agent.apply_images_output(part, text, num, failed)
                else:
                    agent.apply_tool_output(part, text, failed)

        agent.update(self.cid, apply)
