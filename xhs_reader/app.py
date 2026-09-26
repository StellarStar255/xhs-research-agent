"""Entry point for `python -m xhs_reader` and the packaged app.

  <app>             start the chat GUI (or, if it's already running, just open it)
  <app> cli ...     run the scraper CLI (used by the agent backends)
"""
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

from . import paths

DEFAULT_PORT = 8766


def _is_ours(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=0.7) as r:
            return json.load(r).get("app") == paths.APP_ID
    except Exception:
        return False


def _is_free(port):
    with socket.socket() as s:
        if sys.platform != "win32":  # match uvicorn, so a just-closed port counts as free
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pick_port():
    """(port, already_running). Skips ports other programs are using."""
    first = int(os.environ.get("XHS_PORT", DEFAULT_PORT))
    for port in range(first, first + 20):
        if _is_ours(port):
            return port, True
        if _is_free(port):
            return port, False
    raise SystemExit("找不到可用的端口")


def _log_to_file():
    """A double-clicked app has no console (on Windows sys.stdout is None)."""
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    f = open(paths.DATA_DIR / "app.log", "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = f


def _wait_ready(port, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_ours(port):
            return True
        time.sleep(0.2)
    return False


LOADING = """<html><body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
font:15px -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#7a7a80;background:#f7f6f4">
正在启动小红书调研助手…</body></html>"""


def _run_window(url, port):
    """Show the UI in a native window (Dock/taskbar icon, app menu). Blocks until the
    window closes. Returns False if no native webview is available on this machine."""
    try:
        import webview
        from webview.menu import Menu, MenuAction, MenuSeparator
    except Exception:
        return False
    from . import agent, server

    def js(code):
        return lambda: window.evaluate_js(code)

    def open_data_dir():
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(paths.DATA_DIR)])
        elif sys.platform == "win32":
            os.startfile(paths.DATA_DIR)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(paths.DATA_DIR)])

    menu = [Menu("小红书调研助手", [
        MenuAction("新对话", js("newChat()")),
        MenuAction("设置…", js("openSettings()")),
        MenuAction("扫码登录小红书…", js("startLogin()")),
        MenuSeparator(),
        MenuAction("在浏览器中打开", lambda: webbrowser.open(url)),
        MenuAction("打开数据文件夹", open_data_dir),
    ])]
    window = webview.create_window(
        "小红书调研助手", html=LOADING, width=1240, height=840, min_size=(860, 600),
        text_select=True,  # pywebview disables selection by default; answers must be copyable
        menu=menu, background_color="#f7f6f4",
        localization={"global.quitConfirmation": "还有调研正在进行，退出会停止它。确定要退出吗？",
                      "global.quit": "退出", "global.cancel": "取消"},
    )

    def on_closing():
        # Ask only when something is running (pywebview then shows its own dialog).
        window.confirm_close = any(c["running"] for c in agent.list_chats())

    window.events.closing += on_closing
    server.on_shutdown = window.destroy  # the page's 退出 button closes the window too

    def load_when_ready():
        if _wait_ready(port):
            window.load_url(url)
        else:
            window.load_html("<p style='font-family:sans-serif;padding:40px'>启动失败，请查看数据文件夹里的 app.log。</p>")

    try:
        webview.start(load_when_ready, private_mode=False, storage_path=str(paths.DATA_DIR / "webview"))
    except Exception:
        logging.exception("native window failed; falling back to the browser")
        return False
    return True


def serve():
    port, running = _pick_port()
    url = f"http://localhost:{port}"
    ui = os.environ.get("XHS_UI", "window")      # "window" (native) or "browser"
    no_browser = os.environ.get("XHS_NO_BROWSER")  # for tests / headless use
    if running:
        if not no_browser:
            webbrowser.open(url)
        return

    import uvicorn
    from . import agent, server

    agent.recover()
    config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning",
                            log_config=None if paths.FROZEN else uvicorn.config.LOGGING_CONFIG)
    server.server = uvicorn.Server(config)
    print(f"小红书调研助手：{url}", flush=True)

    if no_browser:
        server.server.run()
        return

    # The GUI toolkit needs the main thread, so the web server runs in the background.
    thread = threading.Thread(target=server.server.run, daemon=True)
    thread.start()
    if ui == "window" and _run_window(url, port):
        server.on_shutdown = None  # the window is already gone
        server.shutdown()  # window closed: stop running turns and the server
        thread.join(timeout=10)
        return
    # Browser fallback (no native webview, or XHS_UI=browser).
    if _wait_ready(port):
        webbrowser.open(url)
    thread.join()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["cli"]:
        from . import cli
        return cli.main(argv[1:])
    if paths.FROZEN:
        _log_to_file()
        logging.basicConfig(level=logging.WARNING)
    serve()


if __name__ == "__main__":
    main()
