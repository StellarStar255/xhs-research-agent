"""Local chat GUI (FastAPI). Started by xhs_reader.app: `python -m xhs_reader` or the packaged app."""
import re
import subprocess
import threading
import time
from pathlib import Path

import markdown
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import agent, paths, procutil, remote, scraper, settings, store, updater
app = FastAPI()

# What a phone (remote client, see remote.py) may not do: these stay on the computer.
REMOTE_DENY_POST = ("/api/shutdown", "/api/settings", "/api/update/start", "/api/ui", "/api/browser-window",
                    "/api/web-search", "/api/login", "/api/export/reveal")
LOCKED_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>种草调研助手</title><body style="font:16px/1.7 -apple-system,sans-serif;padding:40px 24px;text-align:center">
<h2>需要扫码连接</h2><p>请在电脑上打开「设置 → 手机访问」，用手机扫描那里的二维码。</p></body>"""


_LOOPBACK = {"localhost", "127.0.0.1", "[::1]"}


def _hostname(host):
    host = host.lower()
    if host.startswith("["):
        return host[:host.find("]") + 1]
    return host.rsplit(":", 1)[0]


@app.middleware("http")
async def remote_guard(request: Request, call_next):
    # Other websites open in the user's browser can send requests to this local server:
    # - DNS rebinding reaches it under a foreign host name → the loopback server only
    #   answers to localhost / 127.0.0.1;
    # - cross-site requests (CSRF) → no state change unless the request comes from our
    #   own pages. Clients that send no Origin (curl, the app's own helpers) aren't
    #   browsers acting for another site, so they pass.
    host = request.headers.get("host", "")
    if remote.main_port is not None and not remote.is_remote(request.scope) and _hostname(host) not in _LOOPBACK:
        return JSONResponse({"detail": "不允许的访问地址"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (
                origin is not None and origin != f"{request.url.scheme}://{host}"):
            return JSONResponse({"detail": "拒绝来自其他网站的请求"}, status_code=403)
    if not remote.is_remote(request.scope):
        return await call_next(request)
    from_query = request.query_params.get("k")
    if not (remote.key_ok(from_query) or remote.key_ok(request.cookies.get(remote.COOKIE))):
        return HTMLResponse(LOCKED_PAGE, status_code=401)
    path = request.url.path
    if path.startswith("/api/mobile") or (request.method != "GET" and path.startswith(REMOTE_DENY_POST)):
        return JSONResponse({"detail": "这个操作只能在电脑上进行"}, status_code=403)
    response = await call_next(request)
    if remote.key_ok(from_query):
        response.set_cookie(remote.COOKIE, from_query, max_age=400 * 86400, httponly=True, samesite="lax")
    return response


@app.get("/api/client")
def client(request: Request):
    return {"remote": remote.is_remote(request.scope)}


@app.get("/manifest.webmanifest")
def manifest():
    # No start_url: a home-screen shortcut opens the address it was added from (with its key).
    return JSONResponse({"name": "种草调研助手", "short_name": "调研助手", "display": "standalone",
                         "background_color": "#161617", "theme_color": "#ff2442",
                         "icons": [{"src": "/icon.png", "sizes": "256x256", "type": "image/png"}]},
                        media_type="application/manifest+json")


class MobileReq(BaseModel):
    enabled: bool


@app.get("/api/mobile")
def mobile_status():
    m = remote.conf()
    out = {"enabled": m["enabled"], "port": remote.running_port(), "url": None, "qr": None}
    if m["enabled"]:
        out["url"] = remote.url()
        out["qr"] = remote.qr_svg(out["url"]) if out["url"] else None
        if not out["url"]:
            out["error"] = "没有找到这台电脑的局域网地址，请确认已连接 Wi-Fi 或网线"
    return out


@app.post("/api/mobile")
def mobile_set(req: MobileReq):
    try:
        remote.set_enabled(req.enabled)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return mobile_status()


@app.post("/api/mobile/reset")
def mobile_reset():
    remote.reset_key()
    return mobile_status()
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
    claude_model: str = ""


def _merged(req: SettingsReq):
    s = settings.load()
    if req.backend not in ("claude", "codex", "api"):
        raise HTTPException(400, "未知的后端")
    s["backend"] = req.backend
    if req.claude_model not in settings.CLAUDE_MODELS:
        raise HTTPException(400, "未知的 Claude 模型")
    s["claude_model"] = req.claude_model
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


@app.get("/favicon.ico")
def favicon():
    return FileResponse(Path(__file__).parent / "static" / "favicon.ico", media_type="image/x-icon")


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
        cid, queued = agent.submit(req.chat_id, req.message.strip(), [i.model_dump() for i in req.images])
        return {"id": cid, "queued": queued}
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
            elif p["type"] in ("search", "open") and p["status"] == "running":
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


class ExportReq(BaseModel):
    format: str = "png"            # "png" (long image), "pdf" or "html" (standalone web page)
    index: int | None = None       # an assistant message: export just that Q&A; None = whole chat
    hide_names: bool = False       # replace commenters' nicknames with 某用户
    save: bool = False             # save into ~/Downloads (the native window can't download files)


_last_export = {"path": None}


@app.post("/api/chats/{cid}/export")
def export_chat(cid: str, req: ExportReq, request: Request):
    from urllib.parse import quote
    from fastapi.responses import Response
    from . import export
    if req.format not in ("png", "pdf", "html"):
        raise HTTPException(400, "未知的导出格式")
    try:
        title, page = export.build_html(cid, req.index, req.hide_names)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(404, str(e) if isinstance(e, ValueError) else "找不到这个对话")
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", title).strip("_")[:40] or "种草调研"
    if req.format == "html":
        body, mime, ext = page.encode("utf-8"), "text/html; charset=utf-8", "html"
    else:
        try:
            if req.format == "pdf":
                body, mime, ext = export.render_pdf(page), "application/pdf", "pdf"
            else:
                body, mime, ext = export.render_png(page), "image/png", "png"
        except Exception as e:
            raise HTTPException(500, f"生成{'PDF' if req.format == 'pdf' else '图片'}失败：{e}")
    if req.save and not remote.is_remote(request.scope):
        folder = Path.home() / "Downloads"
        folder.mkdir(exist_ok=True)
        f, n = folder / f"{name}.{ext}", 1
        while f.exists():
            n += 1
            f = folder / f"{name} ({n}).{ext}"
        f.write_bytes(body)
        _last_export["path"] = f
        return {"saved": str(f), "name": f.name}
    return Response(body, media_type=mime,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.{ext}"})


@app.post("/api/export/reveal")
def reveal_export():
    """Show the last saved export in Finder / Explorer."""
    import sys
    f = _last_export["path"]
    if not f or not f.exists():
        raise HTTPException(404, "文件不存在")
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(f)])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(f)])
    else:
        subprocess.Popen(["xdg-open", str(f.parent)])
    return {"ok": True}


class RenameReq(BaseModel):
    title: str


@app.post("/api/chats/{cid}/rename")
def rename_chat(cid: str, req: RenameReq):
    title = " ".join(req.title.split())[:60]
    if not title:
        raise HTTPException(400, "名称不能为空")
    try:
        agent.rename(cid, title)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "找不到这个对话")
    return {"title": title}


@app.delete("/api/chats/{cid}/queue/{qid}")
def unqueue(cid: str, qid: str):
    try:
        if not agent.unqueue(cid, qid):
            raise HTTPException(404, "这条消息已经发出了")
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "找不到这个对话")
    return {"ok": True}


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


_SAFE_NAME = re.compile(r"^[\w.-]+$")


@app.get("/api/sessions/{sid}/notes/{index}/images")
def note_images(sid: str, index: int):
    """The images the assistant looked at for one note (downloaded by view_note_images)."""
    try:
        notes = store.read_notes(sid)
        folder = store.path(sid) / "images"
    except (FileNotFoundError, ValueError):
        raise HTTPException(404)
    if not 1 <= index <= len(notes):
        raise HTTPException(404)
    n = notes[index - 1]
    nid = str(n.get("id") or index)
    files = sorted((folder / nid).glob("*"), key=lambda f: (len(f.stem), f.stem)) if _SAFE_NAME.match(nid) else []
    return {"title": n.get("title") or "", "url": n.get("url") or "", "author": n.get("author") or "",
            "images": [f"/api/sessions/{sid}/image/{nid}/{f.name}" for f in files if f.is_file()]}


@app.get("/api/sessions/{sid}/image/{nid}/{name}")
def note_image(sid: str, nid: str, name: str):
    if not (_SAFE_NAME.match(nid) and _SAFE_NAME.match(name)):
        raise HTTPException(404)
    try:
        f = store.path(sid) / "images" / nid / name
    except ValueError:
        raise HTTPException(404)
    if not f.is_file():
        raise HTTPException(404)
    return FileResponse(f)


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
    # Nothing touches Xiaohongshu before the user has accepted the usage notice.
    if not settings.notice_accepted():
        return {"logged_in": None, "needs_notice": True}
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
    if req.mode not in ("background", "visible"):
        raise HTTPException(400, "未知的浏览器窗口设置")
    s = settings.load()
    s["browser_window"] = req.mode
    settings.save(s)
    return get_browser_window()


class WebSearchReq(BaseModel):
    enabled: bool


@app.post("/api/web-search")
def post_web_search(req: WebSearchReq):
    s = settings.load()
    s["web_search"] = req.enabled
    settings.save(s)
    return {"enabled": s["web_search"]}


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
