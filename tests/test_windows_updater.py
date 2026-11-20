import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
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
        self.name = "XHS-Research-Agent-9.9.9-windows-x64-setup.exe"
        self.release = {"installer_name": self.name, "sums": "sums", "installer": "setup"}
        self.work = updater._update_directory("9.9.9")
        self.content = b"complete installer"
        self.digest = hashlib.sha256(self.content).hexdigest()

    def response(self, content, size=None):
        response = io.BytesIO(content)
        response.headers = {"Content-Length": str(len(content) if size is None else size)}
        return response

    def test_missing_or_malformed_windows_checksum_stops_before_download(self):
        for data in (b"", f"invalid *{self.name}\n".encode()):
            with self.subTest(data=data), patch.object(updater, "_get", return_value=self.response(data)) as fetch:
                with self.assertRaisesRegex(RuntimeError, "有效的校验文件"):
                    updater._prepare_installer(self.release, self.work)
                fetch.assert_called_once_with("sums")
                self.assertFalse(self.work.exists())

    def test_incomplete_or_mismatched_download_leaves_no_partial_executable(self):
        for content, size, message in ((b"short", len(self.content), "下载不完整"),
                                       (b"wrong", 5, "SHA256")):
            responses = [self.response(f"{self.digest} *{self.name}\n".encode()),
                         self.response(content, size)]
            with self.subTest(message=message), patch.object(updater, "_get", side_effect=responses):
                with self.assertRaisesRegex(RuntimeError, message):
                    updater._prepare_installer(self.release, self.work)
                self.assertEqual(list(self.work.iterdir()), [])

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
        self.assertEqual(list(self.work.iterdir()), [dest])

    def test_release_check_ignores_path_like_assets_and_prereleases(self):
        asset = {"name": self.name, "browser_download_url": "setup"}
        for assets, prerelease in (([{**asset, "name": "../" + self.name}], False),
                                   ([asset], True), ([asset], False)):
            payload = json.dumps({"tag_name": "v9.9.9", "assets": assets,
                                  "prerelease": prerelease}).encode()
            with self.subTest(assets=assets, prerelease=prerelease), \
                 patch.object(updater, "_get", return_value=self.response(payload)):
                self.assertEqual(updater.check()["available"], assets == [asset] and not prerelease)


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
import hashlib
import io
import json
