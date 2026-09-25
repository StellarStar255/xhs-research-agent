"""Cross-platform process helpers (process groups and os.killpg are POSIX-only)."""
import subprocess
import sys

import psutil

WINDOWS = sys.platform == "win32"


def popen(cmd, **kw):
    """Popen in its own process group/session, so the whole tree can be stopped."""
    if WINDOWS:
        kw["creationflags"] = kw.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP \
            | subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    if kw.get("text"):
        kw.setdefault("encoding", "utf-8")
        kw.setdefault("errors", "replace")
    return subprocess.Popen(cmd, **kw)


def kill_tree(proc, timeout=5):
    """Terminate a process and everything it started (e.g. Chrome under the scraper)."""
    try:
        parent = psutil.Process(proc.pid)
        procs = parent.children(recursive=True) + [parent]
    except psutil.NoSuchProcess:
        return
    for p in procs:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass


def pid_alive(pid):
    # Not os.kill(pid, 0): on Windows that *terminates* the process.
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
