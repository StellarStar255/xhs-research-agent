"""Entry point for `python -m xhs_reader` and the packaged app.

  <app>             start the chat GUI (or, if it's already running, just open it)
  <app> cli ...     run the scraper CLI (used by the agent backends)
"""
import json
import logging
import os
import socket
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


def serve():
    port, running = _pick_port()
    url = f"http://localhost:{port}"
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

    def open_when_ready():
        for _ in range(100):
            if _is_ours(port):
                webbrowser.open(url)
                return
            time.sleep(0.2)
    if not no_browser:
        threading.Thread(target=open_when_ready, daemon=True).start()
    print(f"小红书调研助手：{url}", flush=True)
    server.server.run()


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
