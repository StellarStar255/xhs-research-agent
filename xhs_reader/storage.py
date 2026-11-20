"""Data usage and explicitly selected cleanup. Chat and research originals stay intact."""
import sqlite3
import shutil
from contextlib import closing
from . import agent, paths, store


def usage():
    categories = {key: {"bytes": 0, "files": 0} for key in ("chats", "chat_images", "research", "trash", "note_cache")}
    def add(key, files):
        for f in files:
            try:
                categories[key]["bytes"] += f.stat().st_size
                categories[key]["files"] += 1
            except FileNotFoundError:
                continue
    if agent.CHATS_DIR.is_dir() and not paths.is_redirected(agent.CHATS_DIR):
        add("chats", (p for p in agent.CHATS_DIR.glob("*.json") if not paths.is_redirected(p) and p.is_file()))
        add("chat_images", (f for d in agent.CHATS_DIR.iterdir() if d.name != ".trash" and d.is_dir()
                            for f in paths.iter_local_files(d)))
        add("trash", paths.iter_local_files(agent.CHATS_DIR / ".trash"))
    add("research", paths.iter_local_files(store.RESEARCH_DIR))
    add("note_cache", (p for p in paths.DATA_DIR.glob("note_cache.sqlite3*")
                       if not paths.is_redirected(p) and p.is_file()))
    return {"categories": categories, "total_bytes": sum(c["bytes"] for c in categories.values())}


def cleanup(categories):
    if not categories or not set(categories) <= {"trash", "note_cache"}:
        raise ValueError("请选择回收站或笔记缓存")
    with agent._lock:
        if any(agent.is_running(cid) for cid in list(agent._runs)):
            raise RuntimeError("请先停止任务或等待回答结束，再清理")
        root = agent.trash_root() if "trash" in categories else None
        cache = paths.DATA_DIR / "note_cache.sqlite3"
        if "note_cache" in categories and any(
                paths.is_redirected(cache.with_name(cache.name + suffix))
                for suffix in ("", "-wal", "-shm", "-journal")):
            raise ValueError("缓存路径是符号链接或目录联接，无法清理")
        before = usage()["total_bytes"]
        if "note_cache" in categories and cache.exists():
            # Keep the database and its import marker; let SQLite serialize writers.
            with closing(sqlite3.connect(cache, timeout=5)) as db:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notes'").fetchone()
                if exists:
                    db.execute("DELETE FROM notes")
                    db.commit()
                    db.execute("VACUUM")
        if root and root.exists():
            for entry in root.iterdir():
                if entry.is_symlink():
                    entry.unlink()
                elif paths.is_redirected(entry):
                    entry.rmdir()
                elif entry.is_file():
                    entry.unlink()
                elif entry.is_dir():
                    shutil.rmtree(entry)
        after = usage()
        return {**after, "freed_bytes": max(0, before - after["total_bytes"])}
