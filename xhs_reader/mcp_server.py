"""Minimal MCP server (JSON-RPC 2.0 over stdio) exposing the scraper to agent CLIs that
can't be limited to specific shell commands (Codex): `xhs-cli mcp`.

Tools and per-turn budgets come from tools.py (shared with the API backend). The agent
starts one server process per turn, so the counters here are per turn.
"""
import base64
import json
import os
import sys
from pathlib import Path

from . import paths
from .images import MIME
from .tools import TOOLS, ToolRunner

PROTOCOL = "2025-06-18"
_runner = ToolRunner(os.environ.get("XHS_TURN_ID", ""), source=os.environ.get("XHS_RESEARCH_SOURCE", "auto"))


def call_tool(name, args):
    """MCP content + isError for one tool call; images are returned inline."""
    r = _runner.call(name, args, str(args.get("question") or ""))
    content = [{"type": "text", "text": r["text"]}]
    for p in r["images"]:
        content.append({"type": "image", "data": base64.b64encode(p.read_bytes()).decode(),
                        "mimeType": MIME.get(p.suffix.lstrip("."), "image/webp")})
    return content, r["is_error"]


def _record_tool_result(name, content, is_error):
    # agy acknowledges MCP completion over ACP without including its output.
    # The launcher may provide a private per-turn file for rendering progress.
    path = os.environ.get("XHS_MCP_RESULTS")
    if path:
        record = {"name": name, "text": " ".join(p["text"] for p in content if p["type"] == "text"),
                  "images": sum(p["type"] == "image" for p in content), "error": bool(is_error)}
        try:
            with Path(path).open("ab") as file:
                file.write((json.dumps(record) + "\n").encode("utf-8"))
        except OSError:
            pass


def launch_spec(turn_id, source="auto"):
    """(command, args, env) that starts this server, for the agent CLIs' MCP configs."""
    env = {"XHS_DATA_DIR": str(paths.DATA_DIR), "XHS_TURN_ID": turn_id}
    if source != "auto":
        env["XHS_RESEARCH_SOURCE"] = source
    if paths.FROZEN:
        return str(paths.cli_executable()), ["mcp"], env
    env["PYTHONPATH"] = str(paths.ROOT)
    return sys.executable, ["-m", "xhs_reader.cli", "mcp"], env


def _tools():
    out = []
    for t in TOOLS:
        f = t["function"]
        out.append({"name": f["name"], "description": f["description"], "inputSchema": f["parameters"]})
    return out


def _reply(request_id, **payload):
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, **payload}, ensure_ascii=False), flush=True)


def serve():
    # MCP uses UTF-8 even when redirected Windows stdio defaults to an ANSI code page.
    sys.stdout.reconfigure(encoding="utf-8")
    paths.use_system_certificates()
    for line in sys.stdin.buffer:
        try:
            req = json.loads(line)
        except ValueError:
            _reply(None, error={"code": -32700, "message": "Parse error"})
            continue
        if not isinstance(req, dict):
            _reply(None, error={"code": -32600, "message": "Invalid request"})
            continue
        method, rid = req.get("method"), req.get("id")
        if rid is None:  # notification (e.g. notifications/initialized)
            continue
        params = req.get("params", {})
        if not isinstance(params, dict):
            _reply(rid, error={"code": -32602, "message": "Invalid params"})
            continue
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "xiaohongshu", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": _tools()}
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(args, dict):
                _reply(rid, error={"code": -32602, "message": "Invalid tool name or arguments"})
                continue
            content, is_error = call_tool(name, args)
            _record_tool_result(name, content, is_error)
            result = {"content": content, "isError": bool(is_error)}
        elif method == "ping":
            result = {}
        else:
            _reply(rid, error={"code": -32601, "message": f"unknown method {method}"})
            continue
        _reply(rid, result=result)
