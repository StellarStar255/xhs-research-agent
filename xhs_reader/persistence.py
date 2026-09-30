"""Atomic UTF-8 JSON writes and process/thread locks shared by GUI and CLI."""
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from contextlib import contextmanager

_locks = {}
_guard = threading.Lock()
log = logging.getLogger(__name__)


@contextmanager
def file_lock(path, timeout=30):
    """Lock a persistent sidecar, never unlink it (other processes may be waiting)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _guard:
        lock = _locks.setdefault(str(path.resolve()), threading.Lock())
    deadline = time.monotonic() + timeout
    if not lock.acquire(timeout=max(0, timeout)):
        raise TimeoutError(f"等待文件锁超时：{path.name}")
    try:
        with path.open("a+b") as f:
            if os.name == "nt":
                import msvcrt
                if f.seek(0, 2) == 0:
                    f.write(b"\0")
                    f.flush()
                def acquire():
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                def release():
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                def acquire():
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                def release():
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            while True:
                try:
                    acquire()
                    break
                except (BlockingIOError, OSError) as e:
                    import errno
                    if e.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"等待文件锁超时：{path.name}") from e
                    time.sleep(0.05)
            try:
                yield
            finally:
                release()
    finally:
        lock.release()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def quarantine(path):
    """Keep damaged records for recovery without breaking every list/startup."""
    import uuid
    path = Path(path)
    target = path.with_name(f"{path.name}.corrupt-{uuid.uuid4().hex[:8]}")
    try:
        path.replace(target)
        log.warning("损坏的数据文件已隔离：%s", target)
    except FileNotFoundError:
        pass
