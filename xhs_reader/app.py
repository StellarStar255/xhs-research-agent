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


def _ping(port):
    """The /api/ping reply if one of our instances is on this port, else None."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=0.7) as r:
            info = json.load(r)
        return info if info.get("app") == paths.APP_ID else None
    except Exception:
        return None


def _is_ours(port):
    return _ping(port) is not None


def _vtuple(v):
    import re
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:3])


def _replace_older(port, info):
    """An older version is still running (e.g. the app was reinstalled while open):
    ask it to quit so this one takes over, instead of showing the old instance."""
    from . import __version__
    if _vtuple(info.get("version")) >= _vtuple(__version__):
        return False
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", method="POST"),
                               timeout=3).read()
    except Exception:
        return False
    for _ in range(50):  # wait for it to release the port
        time.sleep(0.2)
        if _is_free(port):
            return True
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
        info = _ping(port)
        if info:
            if _replace_older(port, info):
                return port, False
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


MAC, WINDOWS = sys.platform == "darwin", sys.platform == "win32"
ICON = paths.PKG_DIR / "static" / "icon.png"


TRAY_ICON = paths.PKG_DIR / "static" / "tray.png"


class MacShell:
    """macOS extras pywebview lacks.

    Window mode:  regular app: Dock icon (clicking it shows the window), badge with the
                  number of research turns in progress.
    Browser mode: menu-bar ("status bar") icon instead of a Dock icon, with the status,
                  打开页面 / 切换为独立窗口 / 设置 / 退出, and the running count beside it.
    Methods named *_main must run on the main (AppKit) thread; use call().
    """

    def __init__(self, actions, running_count):
        import AppKit
        import objc
        from PyObjCTools import AppHelper
        from webview.platforms.cocoa import BrowserView

        self.AppKit, self.AppHelper = AppKit, AppHelper
        self.actions, self.running_count = actions, running_count
        self.item = self.status_line = None
        self.mode = None
        shell = self

        def reopen(self_, app, has_visible_windows):  # Dock icon clicked
            shell.actions["reopen"]()
            return True
        objc.classAddMethods(BrowserView.AppDelegate, [objc.selector(
            reopen, selector=b"applicationShouldHandleReopen:hasVisibleWindows:", signature=b"Z@:@Z")])

        class XHSMenuTarget(AppKit.NSObject):
            def act_(self_, sender):
                shell.actions[sender.representedObject()]()
        self.target = XHSMenuTarget.alloc().init()
        threading.Thread(target=self._status_loop, daemon=True).start()

    def call(self, fn, *args):
        self.AppHelper.callAfter(fn, *args)

    def _build_item_main(self):
        AK = self.AppKit
        item = AK.NSStatusBar.systemStatusBar().statusItemWithLength_(AK.NSVariableStatusItemLength)
        img = AK.NSImage.alloc().initWithContentsOfFile_(str(TRAY_ICON))
        if img:
            img.setSize_((18, 18))
            img.setTemplate_(True)  # macOS tints it for light/dark menu bars
            item.button().setImage_(img)
        item.button().setImagePosition_(AK.NSImageLeft)
        item.button().setToolTip_("小红书调研助手")
        menu = AK.NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        self.status_line = menu.addItemWithTitle_action_keyEquivalent_("空闲", None, "")
        self.status_line.setEnabled_(False)
        menu.addItem_(AK.NSMenuItem.separatorItem())
        for title, key in [("打开页面", "open"), ("切换为独立窗口", "window"), ("设置 / 检查更新…", "settings"),
                           (None, None), ("退出小红书调研助手", "quit")]:
            if title is None:
                menu.addItem_(AK.NSMenuItem.separatorItem())
                continue
            mi = AK.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, "act:", "")
            mi.setTarget_(self.target)
            mi.setRepresentedObject_(key)
            menu.addItem_(mi)
        item.setMenu_(menu)
        self.item = item

    def apply_mode_main(self, mode):
        AK = self.AppKit
        self.mode = mode
        if mode == "browser":
            if not self.item:
                self._build_item_main()
            self.item.setVisible_(True)
            AK.NSApp.setActivationPolicy_(AK.NSApplicationActivationPolicyAccessory)  # no Dock icon
        else:
            if self.item:
                self.item.setVisible_(False)
            AK.NSApp.setActivationPolicy_(AK.NSApplicationActivationPolicyRegular)
            # Becoming a regular app again resets the Dock tile (a blank document icon
            # when running from source), so set the icon every time.
            self._set_dock_icon_main()
            AK.NSApp.activateIgnoringOtherApps_(True)

    def _set_dock_icon_main(self):
        AK = self.AppKit
        bundle_icon = AK.NSBundle.mainBundle().pathForResource_ofType_("icon", "icns") if paths.FROZEN else None
        img = AK.NSImage.alloc().initWithContentsOfFile_(bundle_icon or str(ICON))
        if img:
            AK.NSApp.setApplicationIconImage_(img)

    def confirm_quit_main(self):
        """True if it's fine to quit (asks only while research is running)."""
        if not self.running_count():
            return True
        AK = self.AppKit
        alert = AK.NSAlert.alloc().init()
        alert.setMessageText_("还有调研正在进行")
        alert.setInformativeText_("退出会停止它。确定要退出吗？")
        alert.addButtonWithTitle_("退出")
        alert.addButtonWithTitle_("取消")
        AK.NSApp.activateIgnoringOtherApps_(True)
        return alert.runModal() == AK.NSAlertFirstButtonReturn

    def _show_count_main(self, n):
        self.AppKit.NSApp.dockTile().setBadgeLabel_(str(n) if n and self.mode == "window" else None)
        if self.item:
            self.item.button().setTitle_(f" {n}" if n else "")
            self.status_line.setTitle_(f"正在调研（{n} 个）" if n else "空闲")

    def _status_loop(self):
        last = None
        while True:
            try:
                n = self.running_count()
            except Exception:
                n = 0
            if (n, self.mode) != last:
                last = (n, self.mode)
                self.call(self._show_count_main, n)
            time.sleep(1.5)


class WinTray:
    """Windows notification-area ("tray") icon, so the app can keep running with its window
    closed: browser mode shows no window at all, and in window mode closing the window
    hides it here. Menu: status, 打开页面 / 显示窗口 / 设置, 退出; left-click = default."""

    def __init__(self, actions, running_count):
        import pystray
        from PIL import Image

        self.running_count, self.notified = running_count, False
        M = pystray.MenuItem
        menu = pystray.Menu(
            M(lambda item: self._status(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            M("打开", lambda: actions["default"](), default=True, visible=False),
            M("打开页面", lambda: actions["open"]()),
            M("显示窗口", lambda: actions["window"]()),
            M("设置 / 检查更新…", lambda: actions["settings"]()),
            pystray.Menu.SEPARATOR,
            M("退出小红书调研助手", lambda: actions["quit"]()),
        )
        self.icon = pystray.Icon("xhs-research-agent", Image.open(ICON), "小红书调研助手", menu)
        self.icon.run_detached()
        threading.Thread(target=self._status_loop, daemon=True).start()

    def _status(self):
        n = self.running_count()
        return f"正在调研（{n} 个）" if n else "空闲"

    def _status_loop(self):
        last = None
        while True:
            try:
                n = self.running_count()
            except Exception:
                n = 0
            if n != last:
                last = n
                self.icon.title = f"小红书调研助手 · 正在调研（{n} 个）" if n else "小红书调研助手"
                self.icon.update_menu()
            time.sleep(2)

    def notify_hidden(self):
        if not self.notified:
            self.notified = True
            try:
                self.icon.notify("已最小化到托盘。点图标可以重新打开，右键可以退出。", "小红书调研助手")
            except Exception:
                pass

    def stop(self):
        try:
            self.icon.stop()
        except Exception:
            pass


def _run_window(url, port):
    """Run as a native app: Dock/taskbar icon and app menu, with the UI either in its own
    window or in the browser (settings "ui"). Blocks until the app quits. Returns False
    if no native webview is available on this machine."""
    try:
        import webview
        from webview.menu import Menu, MenuAction, MenuSeparator
    except Exception:
        return False
    from . import agent, server, settings

    state = {"mode": settings.load()["ui"]}

    def js(code):
        return lambda: window.evaluate_js(code)

    def open_data_dir():
        if MAC:
            subprocess.Popen(["open", str(paths.DATA_DIR)])
        elif WINDOWS:
            os.startfile(paths.DATA_DIR)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(paths.DATA_DIR)])

    def show_window():
        window.show()
        if WINDOWS:
            window.restore()

    def tuck_away():
        # In browser mode the window stays alive (it keeps the app running and owns the menu),
        # just out of the way: hidden when a menu-bar/tray icon keeps the app reachable
        # (macOS, Windows with tray), minimized otherwise.
        window.hide() if (MAC or tray) else window.minimize()

    def set_ui(mode):
        state["mode"] = mode
        if shell:
            shell.call(shell.apply_mode_main, mode)
        if mode == "browser":
            tuck_away()
            webbrowser.open(url)
        else:
            show_window()

    def switch_mode(mode):  # from a menu: remember it, like the settings panel does
        s = settings.load()
        s["ui"] = mode
        settings.save(s)
        set_ui(mode)

    def open_settings():
        if state["mode"] == "browser":
            webbrowser.open(url + "/#settings")
        else:
            show_window()
            window.evaluate_js("openSettings()")

    def quit_app():  # menu-bar 退出 (runs on the main thread)
        if shell.confirm_quit_main():
            threading.Thread(target=server.shutdown, daemon=True).start()

    shell = None
    if MAC:
        try:
            shell = MacShell({
                "reopen": lambda: show_window() if state["mode"] == "window" else webbrowser.open(url),
                "open": lambda: webbrowser.open(url),
                "window": lambda: switch_mode("window"),
                "settings": open_settings,
                "quit": quit_app,
            }, lambda: sum(c["running"] for c in agent.list_chats()))
        except Exception:
            logging.exception("macOS Dock/menu-bar integration unavailable")

    menu = [Menu("小红书调研助手", [
        MenuAction("新对话", js("newChat()")),
        MenuAction("设置…", js("openSettings()")),
        MenuAction("扫码登录小红书…", js("startLogin()")),
        MenuAction("检查更新…", js("openSettings()")),
        MenuSeparator(),
        MenuAction("切换为独立窗口", lambda: switch_mode("window")),
        MenuAction("切换为浏览器", lambda: switch_mode("browser")),
        MenuAction("在浏览器中打开", lambda: webbrowser.open(url)),
        MenuAction("打开数据文件夹", open_data_dir),
    ])]
    def quit_now():
        # Real quit: set the flag first, because destroying the window fires `closing`,
        # which otherwise hides to the tray on Windows.
        state["quitting"] = True
        if tray:
            tray.stop()
        window.destroy()

    def tray_quit():
        running = any(c["running"] for c in agent.list_chats())
        if running and not window.create_confirmation_dialog("还有调研正在进行", "退出会停止它。确定要退出吗？"):
            return
        state["quitting"] = True
        threading.Thread(target=server.shutdown, daemon=True).start()

    tray = None
    if WINDOWS:
        try:
            tray = WinTray({
                "default": lambda: show_window() if state["mode"] == "window" else webbrowser.open(url),
                "open": lambda: webbrowser.open(url),
                "window": show_window,
                "settings": open_settings,
                "quit": tray_quit,
            }, lambda: sum(c["running"] for c in agent.list_chats()))
        except Exception:
            logging.exception("tray icon unavailable; closing the window will quit")

    browser_mode = state["mode"] == "browser"
    window = webview.create_window(
        "小红书调研助手", html=LOADING, width=1240, height=840, min_size=(860, 600),
        hidden=browser_mode and bool(MAC or tray), minimized=browser_mode and not (MAC or tray),
        text_select=True,  # pywebview disables selection by default; answers must be copyable
        menu=menu, background_color="#f7f6f4",
        localization={"global.quitConfirmation": "还有调研正在进行，退出会停止它。确定要退出吗？",
                      "global.quit": "退出", "global.cancel": "取消"},
    )

    def on_closing():
        if tray and not state.get("quitting"):
            window.hide()  # Windows: closing the window keeps the app running in the tray
            tray.notify_hidden()
            return False
        # Ask only when something is running (pywebview then shows its own dialog).
        window.confirm_close = any(c["running"] for c in agent.list_chats()) and not state.get("quitting")

    window.events.closing += on_closing
    server.on_shutdown = quit_now  # the page's 退出 button (and tray 退出) quit the app
    server.set_ui = set_ui

    def load_when_ready():
        if shell:
            shell.call(shell.apply_mode_main, state["mode"])
        if _wait_ready(port):
            window.load_url(url)
            if state["mode"] == "browser":
                webbrowser.open(url)
        else:
            window.load_html("<p style='font-family:sans-serif;padding:40px'>启动失败，请查看数据文件夹里的 app.log。</p>")
            show_window()

    # Windows: no icon. pywebview loads it with System.Drawing.Icon, which only takes .ico
    # and throws on our PNG on the GUI thread, killing the process (0xE0434352) where the
    # except below can't catch it. Without one it uses the exe's own (icon.ico) icon.
    icon = str(ICON) if ICON.exists() and not WINDOWS else None
    try:
        webview.start(load_when_ready, private_mode=False, storage_path=str(paths.DATA_DIR / "webview"),
                      icon=icon)
    except Exception:
        logging.exception("native window failed; falling back to the browser")
        server.set_ui = None
        return False
    return True


def serve():
    port, running = _pick_port()
    url = f"http://localhost:{port}"
    ui = os.environ.get("XHS_UI", "native")      # "browser" = no native app shell at all
    no_browser = os.environ.get("XHS_NO_BROWSER")  # for tests / headless use
    if running:
        if not no_browser:
            webbrowser.open(url)
        return

    import uvicorn
    from . import agent, server

    agent.recover()
    from . import updater
    updater.check_periodically()
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
    if ui != "browser" and _run_window(url, port):
        server.on_shutdown = server.set_ui = None  # the window is already gone
        server.shutdown()  # window closed: stop running turns and the server
        thread.join(timeout=10)
        return
    # Browser fallback (no native webview, or XHS_UI=browser).
    if _wait_ready(port):
        webbrowser.open(url)
    thread.join()


def main(argv=None):
    paths.use_system_certificates()
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
