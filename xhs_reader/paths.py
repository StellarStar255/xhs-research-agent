"""Where things live, for both a source checkout and a packaged (PyInstaller) app.

Source checkout: data in <repo>/data, the scraper CLI runs as `python -m xhs_reader.cli`.
Packaged app:    data in ~/.xhs-research-agent (the app bundle is read-only), the CLI is
                 the console executable `xhs-cli` shipped next to the GUI executable.
XHS_DATA_DIR overrides the data directory in both cases.
"""
import os
import sys
from pathlib import Path

APP_ID = "xhs-research-agent"
FROZEN = getattr(sys, "frozen", False)
PKG_DIR = Path(__file__).resolve().parent
ROOT = PKG_DIR.parent  # repo root (source) or the bundle's internal dir (frozen)


def _data_dir():
    if os.environ.get("XHS_DATA_DIR"):
        return Path(os.environ["XHS_DATA_DIR"]).expanduser()
    if FROZEN:
        # A dot-directory rather than ~/Library/Application Support: the Claude Code
        # backend needs a space-free path for its command permission rule.
        return Path.home() / f".{APP_ID}"
    return ROOT / "data"


DATA_DIR = _data_dir()


def cli_executable():
    """The packaged console CLI. A separate console-mode binary because a windowed
    Windows executable can't reliably write to the pipes we read its output from."""
    return Path(sys.executable).with_name("xhs-cli.exe" if sys.platform == "win32" else "xhs-cli")


def cli_command(*args):
    """argv that runs the scraper CLI with the given arguments."""
    if FROZEN:
        return [str(cli_executable()), *args]
    return [sys.executable, "-m", "xhs_reader.cli", *args]


def child_env(extra=None):
    """Environment for child processes: same data dir, UTF-8 output on every OS."""
    env = {**os.environ, "XHS_DATA_DIR": str(DATA_DIR), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if not FROZEN:  # children run with cwd=DATA_DIR; keep the package importable
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")]))
    env.update(extra or {})
    return env


def xhs_script():
    """A shell script that runs the CLI, for the Claude Code backend.

    Claude Code only gets permission to run commands starting with this path, so it
    must be a single space-free token. In a source checkout that's the repo's ./xhs;
    in the packaged app we write one into the data dir.
    """
    if not FROZEN:
        return (ROOT / "xhs").as_posix()
    script = DATA_DIR / "bin" / "xhs"
    exe = cli_executable().as_posix()
    body = f'#!/bin/sh\nXHS_DATA_DIR="{DATA_DIR.as_posix()}" PYTHONUTF8=1 exec "{exe}" "$@"\n'
    if not script.exists() or script.read_text() != body:
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(body)
        script.chmod(0o755)
    return script.as_posix()
