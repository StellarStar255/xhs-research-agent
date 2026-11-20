"""Exercise an in-app update with real installers on a disposable Windows CI runner."""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright


def sha256(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def wait_for(description, probe, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if value := probe():
                return value
        except OSError:
            pass  # launcher or installer is still starting
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for {description}")


def check(previous, setup, expected_exe, root):
    previous_setup, = previous.glob("*-windows-x64-setup.exe")
    checksums = {name.lstrip("*"): sha for sha, name in (
        line.split(maxsplit=1) for line in (previous / "SHA256SUMS.txt").read_text().splitlines()
    )}
    if sha256(previous_setup) != checksums[previous_setup.name]:
        raise RuntimeError("Previous published installer failed SHA-256 verification")

    install_dir = root / "installed"
    data = root / "data"
    logs = root / "previous-install.log"
    command = subprocess.list2cmdline([str(previous_setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
    command += f' /DIR="{install_dir}" /LOG="{logs}"'
    result = subprocess.run(command, timeout=180)
    if result.returncode:
        raise RuntimeError(f"Previous installer exited with {result.returncode}\n"
                           + logs.read_text(encoding="utf-8-sig", errors="replace"))

    exe = install_dir / "XHS Research Agent.exe"
    previous_hash, expected_hash = sha256(exe), sha256(expected_exe)
    if previous_hash == expected_hash:
        raise RuntimeError("The old and new binaries are identical")
    settings = {"backend": "api", "notice_accepted": 1,
                "api": {"provider": "custom", "base_url": "http://127.0.0.1:1",
                        "model": "fixture", "api_key": "fixture-only"}}
    record = {"id": "upgrade-fixture", "title": "Upgrade fixture", "backend": "api", "messages": []}
    (data / "chats").mkdir(parents=True)
    (data / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    (data / "chats/upgrade-fixture.json").write_text(json.dumps(record), encoding="utf-8")

    # Advertise a higher version in the local fixture so a same-version development
    # build can exercise the updater without creating a public release or tag.
    version = "999.0.0"
    asset_hash = sha256(setup)
    asset_size = setup.stat().st_size

    class ReleaseFixture(BaseHTTPRequestHandler):
        def do_GET(self):
            base = f"http://127.0.0.1:{self.server.server_port}"
            if self.path == "/latest":
                output = json.dumps({"tag_name": f"v{version}", "assets": [
                    {"name": setup.name, "browser_download_url": base + "/installer"},
                    {"name": "SHA256SUMS.txt", "browser_download_url": base + "/checksums"},
                ]}).encode()
            elif self.path == "/checksums":
                output = f"{asset_hash}  {setup.name}\n".encode("ascii")
            elif self.path == "/installer":
                self.send_response(200)
                self.send_header("Content-Length", str(asset_size))
                self.end_headers()
                with setup.open("rb") as file:
                    shutil.copyfileobj(file, self.wfile)
                return
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(output)))
            self.end_headers()
            self.wfile.write(output)

    with socket.socket() as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        port = port_probe.getsockname()[1]
    with socket.socket() as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        debug_port = port_probe.getsockname()[1]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    debug_url = f"http://127.0.0.1:{debug_port}"

    def gui_target(previous_target=None):
        with opener.open(debug_url + "/json/list", timeout=3) as response:
            return next((page["id"] for page in json.load(response)
                         if page.get("url", "").startswith(f"http://localhost:{port}/")
                         and page["id"] != previous_target), None)

    def api(path, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
        with opener.open(req, timeout=3) as response:
            return json.load(response)

    work = data / "updates" / version
    with ThreadingHTTPServer(("127.0.0.1", 0), ReleaseFixture) as fixture:
        thread = threading.Thread(target=fixture.serve_forever, daemon=True)
        thread.start()
        env = dict(os.environ, XHS_DATA_DIR=str(data), XHS_PORT=str(port),
                   WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f"--remote-debugging-port={debug_port}",
                   XHS_UPDATE_API=f"http://127.0.0.1:{fixture.server_port}/latest")
        env.pop("XHS_NO_BROWSER", None)
        original = subprocess.Popen([str(exe)], env=env)
        try:
            wait_for("the previous installed app", lambda: api("/api/ping"), 30)
            previous_target = wait_for("the previous WebView2 window", gui_target, 45)
            api("/api/update/check", "POST")
            wait_for("the update to become available", lambda: api("/api/update").get("available"), 15)
            api("/api/update/start", "POST")
            if original.wait(timeout=90):
                raise RuntimeError("Previous app exited with an error during update")

            # Opening the EXE before the installer finishes can cause Windows
            # Restart Manager to close this test process along with the app.
            new = wait_for("the updated app to relaunch on the same port", lambda: api("/api/ping"), 150)
            if sha256(exe) != expected_hash:
                raise RuntimeError("Relaunch used the old binary or a different build")
            assert new["app"] == "xhs-research-agent"
            assert api("/api/chats/upgrade-fixture")["title"] == record["title"]
            assert api("/api/notice")["accepted"]
            wait_for("WebView2 on first launch after the update", lambda: gui_target(previous_target), 45)
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(debug_url, timeout=10000)
                page = next(page for context in browser.contexts for page in context.pages
                            if page.url.startswith(f"http://localhost:{port}/"))
                page.locator("#gear").click(timeout=15000)
                page.locator("#modal.show #sSave").wait_for(state="visible", timeout=20000)
            print(f"PASS: real Windows installer preserved data and Settings opened on first relaunch on port {port}")
        finally:
            try:
                api("/api/shutdown", "POST")
            except OSError:
                pass
            if original.poll() is None:
                original.terminate()
            original.wait(timeout=10)
            fixture.shutdown()
            thread.join()
            for log in (work / "install-helper.log", work / "install-error.txt"):
                if log.exists():
                    print(log.read_text(encoding="utf-8-sig", errors="replace"))


if __name__ == "__main__":
    if sys.platform != "win32" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Use a disposable Windows GitHub Actions runner for this test.")
    # Keep logs until the disposable runner exits, including on a failed upgrade.
    test_root = Path(tempfile.mkdtemp(prefix="xhs-installer-upgrade-", dir=os.environ["RUNNER_TEMP"]))
    previous, setup, expected_exe = (Path(arg).resolve() for arg in sys.argv[1:])
    check(previous, setup, expected_exe, test_root)
