import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import psutil


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
