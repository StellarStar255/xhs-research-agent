"""Data usage and explicitly selected cleanup. Chat and research originals stay intact."""
import sqlite3
import shutil
from contextlib import closing
from . import agent, paths, store


def _files(root):
    if not root.exists() or root.is_symlink():
        return
    for f in root.rglob("*"):
        if f.is_file() and not f.is_symlink() and not any((root / p).is_symlink() for p in f.relative_to(root).parents):
            yield f


def usage():
    categories = {key: {"bytes": 0, "files": 0} for key in ("chats", "chat_images", "research", "trash", "note_cache")}
    def add(key, files):
        for f in files:
            try:
                categories[key]["bytes"] += f.stat().st_size
                categories[key]["files"] += 1
            except FileNotFoundError:
                continue
    add("chats", (p for p in agent.CHATS_DIR.glob("*.json") if not p.is_symlink()))
    if agent.CHATS_DIR.exists():
        add("chat_images", (f for d in agent.CHATS_DIR.iterdir() if d.is_dir() and d.name != ".trash" and not d.is_symlink() for f in _files(d)))
    add("research", _files(store.RESEARCH_DIR))
    add("trash", _files(agent.CHATS_DIR / ".trash"))
    add("note_cache", (p for p in paths.DATA_DIR.glob("note_cache.sqlite3*") if p.is_file() and not p.is_symlink()))
    return {"categories": categories, "total_bytes": sum(c["bytes"] for c in categories.values())}


def cleanup(categories):
    if not categories or not set(categories) <= {"trash", "note_cache"}:
        raise ValueError("请选择回收站或笔记缓存")
    with agent._lock:
        if any(agent.is_running(cid) for cid in list(agent._runs)):
            raise RuntimeError("请先停止任务或等待回答结束，再清理")
        if "trash" in categories and (agent.CHATS_DIR / ".trash").is_symlink():
            raise ValueError("回收站路径是符号链接，无法清理")
        before = usage()["total_bytes"]
        if "note_cache" in categories:
            f = paths.DATA_DIR / "note_cache.sqlite3"
            if f.exists():
                if f.is_symlink():
                    raise ValueError("缓存路径是符号链接，无法清理")
                # Keep the database and its import marker; let SQLite serialize writers.
                with closing(sqlite3.connect(f, timeout=5)) as db:
                    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notes'").fetchone()
                    if exists:
                        db.execute("DELETE FROM notes")
                        db.commit()
                        db.execute("VACUUM")
        if "trash" in categories:
            root = agent.CHATS_DIR / ".trash"
            if root.exists():
                for entry in root.iterdir():
                    if entry.is_symlink() or entry.is_file():
                        entry.unlink()
                    elif entry.is_dir():
                        shutil.rmtree(entry)
        after = usage()
        return {**after, "freed_bytes": max(0, before - after["total_bytes"])}
