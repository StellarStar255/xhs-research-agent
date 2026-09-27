"""Minimal MCP server (JSON-RPC 2.0 over stdio) exposing the scraper to agent CLIs that
can't be limited to specific shell commands (Codex): `xhs-cli mcp`.

Tools and per-turn budgets come from tools.py (shared with the API backend). The agent
starts one server process per turn, so the counters here are per turn.
"""
import base64
import json
import os
import sys

from . import paths
from .images import MIME
from .tools import TOOLS, ToolRunner

PROTOCOL = "2025-06-18"
_runner = ToolRunner(os.environ.get("XHS_TURN_ID", ""))


def call_tool(name, args):
    """MCP content + isError for one tool call; images are returned inline."""
    r = _runner.call(name, args, str(args.get("question") or ""))
    content = [{"type": "text", "text": r["text"]}]
    for p in r["images"]:
        content.append({"type": "image", "data": base64.b64encode(p.read_bytes()).decode(),
                        "mimeType": MIME.get(p.suffix.lstrip("."), "image/webp")})
    return content, r["is_error"]


def launch_spec(turn_id):
    """(command, args, env) that starts this server, for the agent CLIs' MCP configs."""
    env = {"XHS_DATA_DIR": str(paths.DATA_DIR), "XHS_TURN_ID": turn_id}
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


def serve():
    paths.use_system_certificates()
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        method, rid = req.get("method"), req.get("id")
        if rid is None:  # notification (e.g. notifications/initialized)
            continue
        if method == "initialize":
            result = {"protocolVersion": req.get("params", {}).get("protocolVersion") or PROTOCOL,
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "xiaohongshu", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": _tools()}
        elif method == "tools/call":
            p = req.get("params") or {}
            content, is_error = call_tool(p.get("name") or "", p.get("arguments") or {})
            result = {"content": content, "isError": bool(is_error)}
        elif method == "ping":
            result = {}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": rid,
                              "error": {"code": -32601, "message": f"unknown method {method}"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}, ensure_ascii=False), flush=True)
