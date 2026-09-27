"""Minimal MCP server (JSON-RPC 2.0 over stdio) exposing the scraper to agent CLIs that
can't be limited to specific shell commands (Codex): `xhs-cli mcp`.

Tools mirror the API backend's (llm.py) and share its per-turn budget. The agent starts
one server process per turn, so the counters here are per turn.
"""
import json
import re
import subprocess
import sys

from . import paths, procutil
from .llm import DEFAULT_NOTES, MAX_NOTES, MAX_NOTES_PER_SEARCH, MAX_SEARCHES, TOOLS

PROTOCOL = "2025-06-18"
_budget = {"searches": 0, "notes": 0}


def _run_cli(*args):
    p = procutil.popen(paths.cli_command(*args), cwd=paths.DATA_DIR, env=paths.child_env(),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = p.communicate(timeout=900)
    except subprocess.TimeoutExpired:
        procutil.kill_tree(p)
        return "ERROR: 抓取超时", True
    return out, p.returncode != 0


def call_tool(name, args):
    """(text, is_error) for one tool call, applying the per-turn search budget."""
    if name == "search_xiaohongshu":
        keyword = str(args.get("keyword", "")).strip()
        room = MAX_NOTES - _budget["notes"]
        if _budget["searches"] >= MAX_SEARCHES or room <= 0:
            return (f"本轮已经搜索了 {_budget['searches']} 次、{_budget['notes']} 篇笔记，达到上限，不能再搜。"
                    "请用已有信息回答。"), False
        if not keyword:
            return "缺少 keyword 参数。", True
        try:
            limit = int(args.get("limit") or DEFAULT_NOTES)
        except (TypeError, ValueError):
            limit = DEFAULT_NOTES
        limit = max(1, min(limit, MAX_NOTES_PER_SEARCH, room))
        out, failed = _run_cli("research", keyword, "-q", str(args.get("question") or keyword), "-n", str(limit))
        _budget["searches"] += 1
        m = re.search(r"笔记数: (\d+)", out)
        _budget["notes"] += int(m.group(1)) if m else 0
        return out[-60000:], failed and "LIMITED" not in out and "NOT_LOGGED_IN" not in out
    if name == "read_previous_notes":
        sid = str(args.get("session", "")).strip()
        if not sid:
            return "缺少 session 参数。", True
        out, failed = _run_cli("digest", sid)
        return out[-60000:], failed
    return f"没有叫 {name} 的工具。", True


def view_images(args):
    """MCP content for view_note_images: a text line plus the images themselves."""
    import base64
    from . import images
    try:
        title, paths = images.note_images(str(args.get("session", "")).strip(), int(args.get("note") or 0),
                                          args.get("limit") or images.MAX_PER_NOTE)
    except (images.ImageError, ValueError, TypeError) as e:
        return [{"type": "text", "text": str(e)}], False
    content = [{"type": "text", "text": f"「{title}」的 {len(paths)} 张图片："}]
    for p in paths:
        content.append({"type": "image", "data": base64.b64encode(p.read_bytes()).decode(),
                        "mimeType": images.MIME.get(p.suffix.lstrip("."), "image/webp")})
    return content, False


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
            if p.get("name") == "view_note_images":
                content, is_error = view_images(p.get("arguments") or {})
            else:
                text, is_error = call_tool(p.get("name"), p.get("arguments") or {})
                content = [{"type": "text", "text": text}]
            result = {"content": content, "isError": bool(is_error)}
        elif method == "ping":
            result = {}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": rid,
                              "error": {"code": -32601, "message": f"unknown method {method}"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}, ensure_ascii=False), flush=True)
