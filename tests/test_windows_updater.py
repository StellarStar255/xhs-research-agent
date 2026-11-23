import hashlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

import psutil

from xhs_reader import updater


class UpdateDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(updater.paths, "DATA_DIR", self.tmp))
        self.enterContext(patch.object(updater, "WINDOWS", True))
        self.enterContext(patch.object(updater, "MAC", False))
        self.enterContext(patch.object(updater, "_state", {"phase": "idle"}))
        self.enterContext(patch.object(updater.time, "sleep"))
        self.name = "XHS-Research-Agent-9.9.9-windows-x64-setup.exe"
        self.release = {"installer_name": self.name, "sums": "sums", "installer": "setup"}
        self.work = updater._update_directory("9.9.9")
        self.content = b"complete installer"
        self.digest = hashlib.sha256(self.content).hexdigest()

    def response(self, content, size=None, content_range=None):
        response = io.BytesIO(content)
        response.headers = {"Content-Length": str(len(content) if size is None else size)}
        response.status = 206 if content_range else 200
        if content_range:
            response.headers["Content-Range"] = content_range
        return response

    def checksum(self):
        return self.response(f"{self.digest} *{self.name}\n".encode())

    def partial(self):
        return self.work / f"download-{self.digest}.part"

    def test_missing_or_malformed_windows_checksum_stops_before_download(self):
        for data in (b"", f"invalid *{self.name}\n".encode()):
            with self.subTest(data=data), patch.object(updater, "_get", return_value=self.response(data)) as fetch:
                with self.assertRaisesRegex(RuntimeError, "有效的校验文件"):
                    updater._prepare_installer(self.release, self.work)
                fetch.assert_called_once_with("sums")
                self.assertFalse(self.work.exists())

    def test_incomplete_download_can_resume_after_retry_exhaustion(self):
        split = 5
        self.release["installer_size"] = len(self.content)
        with patch.object(updater, "_get", side_effect=[
            self.checksum(), self.response(self.content[:split], len(self.content)),
            *[TimeoutError()] * (updater.DOWNLOAD_ATTEMPTS - 1),
        ]):
            with self.assertRaisesRegex(RuntimeError, "已保留下载进度"):
                updater._prepare_installer(self.release, self.work)
        self.assertFalse((self.work / self.name).exists())
        self.assertEqual(self.partial().read_bytes(), self.content[:split])
        total = len(self.content)
        with patch.object(updater, "_get", side_effect=[
            self.checksum(), self.response(self.content[split:], content_range=f"bytes {split}-{total - 1}/{total}"),
        ]) as fetch:
            dest = updater._prepare_installer(self.release, self.work)
            self.assertEqual(fetch.call_args.kwargs["headers"]["Range"], f"bytes={split}-")
        self.assertEqual(dest.read_bytes(), self.content)
        self.assertFalse(self.partial().exists())

    def test_wrong_digest_discards_partial_and_never_promotes_it(self):
        wrong = b"x" * len(self.content)
        with patch.object(updater, "_get", side_effect=[self.checksum(), self.response(wrong), self.response(wrong)]):
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                updater._prepare_installer(self.release, self.work)
        self.assertFalse(self.partial().exists())
        self.assertFalse((self.work / self.name).exists())

    def test_bad_range_or_unsatisfiable_range_restarts_from_zero(self):
        total = len(self.content)
        for bad in (self.response(self.content[4:], content_range=f"bytes 4-{total - 1}/{total}"),
                    HTTPError("setup", 416, "Range", {}, None)):
            self.work.mkdir(parents=True, exist_ok=True)
            self.partial().write_bytes(self.content[:5])
            with patch.object(updater, "_get", side_effect=[self.checksum(), bad, self.response(self.content)]) as fetch:
                dest = updater._prepare_installer(self.release, self.work)
            self.assertEqual(fetch.call_args_list[1].kwargs["headers"]["Range"], "bytes=5-")
            self.assertNotIn("Range", fetch.call_args.kwargs["headers"])
            self.assertEqual(dest.read_bytes(), self.content)
            dest.unlink()

    def test_server_ignoring_range_replaces_partial(self):
        self.work.mkdir(parents=True)
        self.partial().write_bytes(b"stale")
        with patch.object(updater, "_get", side_effect=[self.checksum(), self.response(self.content)]) as fetch:
            dest = updater._prepare_installer(self.release, self.work)
        self.assertEqual(fetch.call_args.kwargs["headers"]["Range"], "bytes=5-")
        self.assertEqual(dest.read_bytes(), self.content)

    def test_real_http_redirect_and_interrupted_response_resume(self):
        content = b"installer content\n" * 16000
        split = 121337
        total = len(content)
        requests = []
        checksums = f"{hashlib.sha256(content).hexdigest()} *{self.name}\n".encode()

        class Fixture(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/setup":
                    self.send_response(302)
                    self.send_header("Location", "/asset")
                    self.end_headers()
                    return
                if self.path == "/sums":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(checksums)
                    return
                requested = self.headers.get("Range")
                requests.append(requested)
                self.send_response(206 if requested else 200)
                self.send_header("Content-Length", str(total - split if requested else total))
                if requested:
                    self.send_header("Content-Range", f"bytes {split}-{total - 1}/{total}")
                self.end_headers()
                self.wfile.write(content[split:] if requested else content[:split])

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Fixture) as fixture:
            thread = threading.Thread(target=fixture.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{fixture.server_port}"
                release = {**self.release, "sums": base + "/sums", "installer": base + "/setup",
                           "installer_size": total}
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with patch.object(urllib.request, "urlopen", side_effect=opener.open):
                    dest = updater._prepare_installer(release, self.work)
                self.assertEqual(requests, [None, f"bytes={split}-"])
                self.assertEqual(dest.read_bytes(), content)
            finally:
                fixture.shutdown()
                thread.join()

    def test_successful_retry_replaces_installer_only_after_verification(self):
        self.work.mkdir(parents=True)
        dest = self.work / self.name
        dest.write_bytes(b"previous verified installer")

        def fetch(url, **kwargs):
            self.assertEqual(dest.read_bytes(), b"previous verified installer")
            if url == "sums":
                return self.response(f"{self.digest.upper()} *{self.name}\n".encode())
            self.assertEqual(url, "setup")
            return self.response(self.content)

        with patch.object(updater, "_get", side_effect=fetch):
            self.assertEqual(updater._prepare_installer(self.release, self.work), dest)
        self.assertEqual(dest.read_bytes(), self.content)
        self.assertFalse(self.partial().exists())

    def test_release_check_ignores_path_like_assets_and_prereleases(self):
        asset = {"name": self.name, "browser_download_url": "setup", "size": len(self.content)}
        for assets, prerelease in (([{**asset, "name": "../" + self.name}], False),
                                   ([asset], True), ([asset], False)):
            payload = json.dumps({"tag_name": "v9.9.9", "assets": assets,
                                  "prerelease": prerelease}).encode()
            with self.subTest(assets=assets, prerelease=prerelease), \
                 patch.object(updater, "_get", return_value=self.response(payload)):
                state = updater.check()
                self.assertEqual(state["available"], assets == [asset] and not prerelease)
                self.assertEqual(state["installer_size"], len(self.content) if assets == [asset] else 0)


@unittest.skipUnless(sys.platform == "win32", "runs the Windows PowerShell update helper")
class WindowsUpdaterTests(unittest.TestCase):
    def test_helper_waits_for_exit_and_relaunches_after_installer(self):
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                install_dir = root / "User's installed app"
                install_dir.mkdir()
                work = root / "User’s update"
                work.mkdir()
                setup = work / "fake setup.cmd"
                setup.write_text(
                    f'@echo off\n> "%~dp0install-args.txt" echo %*\nexit /b {exit_code}\n',
                    encoding="ascii",
                )
                exe = install_dir / "fake app.cmd"
                exe.write_text(
                    '@echo off\ncd /d "%SystemRoot%"\n'
                    '> "%~dp0relaunched.txt" echo %PYINSTALLER_RESET_ENVIRONMENT%\n',
                    encoding="ascii",
                )
                # The helper must outlive this separate process and only install once it exits.
                command = """
import sys
import time
from pathlib import Path
from xhs_reader import updater
setup, exe = map(Path, sys.argv[1:])
sys.executable = str(exe)
helper = updater._windows_install(setup, setup.parent)
time.sleep(0.5)
assert not (setup.parent / 'install-args.txt').exists(), 'setup ran before app exit'
print(helper.pid)
"""
                parent = subprocess.run(
                    [sys.executable, "-c", command, str(setup), str(exe)],
                    check=True, capture_output=True, timeout=30,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                try:
                    psutil.Process(int(parent.stdout)).wait(timeout=15)
                except psutil.NoSuchProcess:
                    pass
                log = work / "install-helper.log"
                args = (work / "install-args.txt").read_text(errors="replace")
                self.assertIn(f'/DIR="{install_dir}"', args)
                self.assertIn("inno-setup.log", args)
                helper_log = log.read_text(errors="replace")
                self.assertIn(f"Installer exit code: {exit_code}", helper_log)
                self.assertIn("App relaunched.", helper_log)
                failure = work / "install-error.txt"
                if exit_code:
                    self.assertIn(f"Installer exit code: {exit_code}", failure.read_text(encoding="utf-8"))
                else:
                    self.assertFalse(failure.exists())
                marker = install_dir / "relaunched.txt"
                deadline = time.monotonic() + 15
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertEqual(marker.read_text().strip(), "1")
