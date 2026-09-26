"""Local chat GUI (FastAPI). Started by xhs_reader.app: `python -m xhs_reader` or the packaged app."""
import re
import subprocess
import threading
import time
from pathlib import Path

import markdown
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import agent, paths, procutil, scraper, settings, store, updater
app = FastAPI()
_status = {"at": 0, "value": None}
_login = {"proc": None, "state": "idle", "message": ""}


class Image(BaseModel):
    media_type: str
    data: str  # base64, no data: prefix


class ChatReq(BaseModel):
    message: str = ""
    chat_id: str | None = None
    images: list[Image] = []


class ApiConf(BaseModel):
    provider: str = "custom"
    base_url: str = ""
    model: str = ""
    api_key: str = ""  # empty = keep the saved key


class SettingsReq(BaseModel):
    backend: str
    api: ApiConf


def _merged(req: SettingsReq):
    s = settings.load()
    if req.backend not in ("claude", "codex", "api"):
        raise HTTPException(400, "未知的后端")
    s["backend"] = req.backend
    key = req.api.api_key.strip() or s["api"].get("api_key", "")
    s["api"] = {"provider": req.api.provider, "base_url": req.api.base_url.strip().rstrip("/"),
                "model": req.api.model.strip(), "api_key": key}
    return s


_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _md(text):
    # Models often start a list right after a line of text; Python-Markdown needs a
    # blank line there, otherwise the items run together into one paragraph.
    lines, out = text.split("\n"), []
    for line in lines:
        if _LIST_ITEM.match(line) and out and out[-1].strip() and not _LIST_ITEM.match(out[-1]) \
                and not out[-1].startswith(("  ", "\t", "|")):
            out.append("")
        out.append(line)
    return markdown.markdown("\n".join(out), extensions=["tables", "fenced_code", "sane_lists"])


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/icon.png")
def icon():
    return FileResponse(Path(__file__).parent / "static" / "icon.png")


@app.get("/api/chats")
def chats():
    return agent.list_chats()


def _require_notice():
    if not settings.notice_accepted():
        raise HTTPException(403, "请先阅读并同意「使用须知」")


@app.get("/api/notice")
def notice():
    return {"accepted": settings.notice_accepted(), "version": settings.NOTICE_VERSION}


@app.post("/api/notice/accept")
def notice_accept():
    settings.accept_notice()
    return notice()


@app.post("/api/chats")
def send(req: ChatReq):
    _require_notice()
    if not req.message.strip() and not req.images:
        raise HTTPException(400, "问题不能为空")
    if len(req.images) > 6:
        raise HTTPException(400, "一次最多 6 张图片")
    for img in req.images:
        if img.media_type not in agent.EXT:
            raise HTTPException(400, f"不支持的图片格式：{img.media_type}")
        if len(img.data) > 7_000_000:  # ~5MB decoded
            raise HTTPException(400, "图片太大（单张最多约 5MB）")
    try:
        return {"id": agent.send(req.chat_id, req.message.strip(), [i.model_dump() for i in req.images])}
    except FileNotFoundError:
        raise HTTPException(404, "对话不存在")
    except RuntimeError as e:
        raise HTTPException(409, str(e))


def _running_progress():
    """Last log line of the research session currently scraping, if any."""
    for m in store.list_sessions():
        if m["status"] == "running":
            log = store.read_meta(m["id"]).get("log") or []
            return {"session": m["id"], "line": log[-1] if log else "启动浏览器…"}
    return None


@app.get("/api/chats/{cid}")
def chat(cid: str):
    try:
        c = agent.load(cid)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404)
    progress = None
    for msg in c["messages"]:
        if msg["role"] != "assistant":
            continue
        for p in msg["parts"]:
            if p["type"] == "text":
                p["html"] = _md(p["text"])
            elif p["type"] == "search" and p["status"] == "running":
                progress = progress or _running_progress()
                p["progress"] = progress
        if msg.get("draft"):
            msg["draft_html"] = _md(msg["draft"])
    c["running"] = agent.is_running(cid)
    return c


@app.get("/api/chats/{cid}/images/{name}")
def image(cid: str, name: str):
    try:
        f = agent.image_dir(cid) / name
    except ValueError:
        raise HTTPException(404)
    if "/" in name or ".." in name or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f)


@app.post("/api/chats/{cid}/stop")
def stop(cid: str):
    agent.stop(cid)
    return {"ok": True}


@app.delete("/api/chats/{cid}")
def delete(cid: str):
    agent.delete(cid)
    return {"ok": True}


@app.get("/api/sessions/{sid}")
def session(sid: str):
    try:
        return {"meta": store.read_meta(sid), "notes": store.read_notes(sid)}
    except (FileNotFoundError, ValueError):
        raise HTTPException(404)


@app.get("/api/settings")
def get_settings():
    return settings.public()


@app.post("/api/settings")
def save_settings(req: SettingsReq):
    s = _merged(req)
    if s["backend"] == "api" and not settings.api_ready(s):
        raise HTTPException(400, "使用大模型 API 需要填写接口地址、模型名和 API Key")
    settings.save(s)
    return settings.public(s)


@app.post("/api/settings/test")
def test_settings(req: SettingsReq):
    from . import llm
    s = _merged(req)
    if not settings.api_ready(s):
        raise HTTPException(400, "请先填写接口地址、模型名和 API Key")
    try:
        return {"ok": True, "reply": llm.test(s)}
    except Exception as e:
        return {"ok": False, "error": llm.explain(e)}


@app.post("/api/settings/models")
def models(req: SettingsReq):
    from . import llm
    s = _merged(req)
    if not (s["api"]["base_url"] and s["api"]["api_key"]):
        raise HTTPException(400, "请先填写接口地址和 API Key")
    try:
        return {"ok": True, "models": llm.list_models(s)}
    except Exception as e:
        return {"ok": False, "error": llm.explain(e)}


@app.get("/api/status")
def status(refresh: bool = False):
    lim = scraper.limits()
    return {**_login_status(refresh), "limits": lim}


def _login_status(refresh):
    # Checking login launches a browser, so cache it; skip while scraping or cooling down.
    if scraper.LOCK_FILE.exists() or scraper.limits()["cooldown_until"]:
        return _status["value"] or {"busy": True}
    if refresh or not _status["value"] or time.time() - _status["at"] > 600:
        try:
            _status["value"] = scraper.status()
        except Exception as e:
            _status["value"] = {"logged_in": False, "error": str(e)}
        _status["at"] = time.time()
    return _status["value"]


@app.post("/api/login")
def login():
    """Open a visible Chrome window with the QR code; poll /api/login/status for the result."""
    _require_notice()
    p = _login["proc"]
    if p and p.poll() is None:
        return login_status()
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _login.update(state="waiting", message="", proc=procutil.popen(
        paths.cli_command("login"), cwd=paths.DATA_DIR, env=paths.child_env(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
    return login_status()


@app.get("/api/login/status")
def login_status():
    p = _login["proc"]
    if p and _login["state"] == "waiting" and p.poll() is not None:
        out = p.stdout.read()
        m = re.search(r"已登录：(.*)", out)
        if p.returncode == 0 and m:
            _login.update(state="done", message=m.group(1).strip())
            _status.update(value={"logged_in": True, "nickname": m.group(1).strip()}, at=time.time())
        else:
            timeout = "登录超时" in out
            _login.update(state="failed", message="扫码超时（5 分钟），请再试一次" if timeout
                          else "登录没有完成：" + (out.strip().splitlines() or ["未知错误"])[-1][:200])
    return {"state": _login["state"], "message": _login["message"]}


@app.get("/api/ping")
def ping():
    """Lets a second launch find this already-running instance (and its version)."""
    from . import __version__
    return {"app": paths.APP_ID, "version": __version__}


server = None       # the uvicorn.Server, set by xhs_reader.app
on_shutdown = None  # e.g. closes the native window
set_ui = None       # set by xhs_reader.app when running with a native shell: set_ui("window"|"browser")


class UiReq(BaseModel):
    mode: str


@app.get("/api/update")
def update_status():
    st = updater.status()
    st["notes_html"] = _md(st["notes"]) if st.get("notes") else ""
    return st


@app.post("/api/update/check")
def update_check():
    updater.check()
    return update_status()


@app.post("/api/update/start")
def update_start():
    """Download, verify, then quit so the helper can install and relaunch."""
    try:
        updater.start(on_ready_to_quit=lambda: (time.sleep(1.5), shutdown()))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return update_status()


class BrowserWindowReq(BaseModel):
    mode: str


@app.get("/api/browser-window")
def get_browser_window():
    import sys
    return {"mode": settings.load()["browser_window"], "platform": sys.platform}


@app.post("/api/browser-window")
def post_browser_window(req: BrowserWindowReq):
    if req.mode not in ("background", "offscreen"):
        raise HTTPException(400, "未知的抓取方式")
    s = settings.load()
    s["browser_window"] = req.mode
    settings.save(s)
    return get_browser_window()


@app.get("/api/ui")
def get_ui():
    return {"native": set_ui is not None, "mode": settings.load()["ui"]}


@app.post("/api/ui")
def post_ui(req: UiReq):
    if req.mode not in ("window", "browser"):
        raise HTTPException(400, "未知的打开方式")
    s = settings.load()
    s["ui"] = req.mode
    settings.save(s)
    if set_ui:
        set_ui(req.mode)
    return get_ui()


@app.post("/api/shutdown")
def shutdown():
    """Stop running turns and the server (the page's 退出 button, or the window closing)."""
    for cid in [c["id"] for c in agent.list_chats() if c["running"]]:
        agent.stop(cid)
    if server:
        server.should_exit = True
    if on_shutdown:
        threading.Thread(target=on_shutdown, daemon=True).start()  # don't block the response
    return {"ok": True}


if __name__ == "__main__":
    from .app import main
    main()
