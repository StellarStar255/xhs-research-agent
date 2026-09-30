"""Indexed note copies with a fetch time independent of research-file modification."""
import json
import sqlite3
import time

from .paths import DATA_DIR
from .persistence import read_json


def _put(db, notes, fallback):
    for note in notes:
        if not isinstance(note, dict) or not note.get("id") or not note.get("opened", "comments" in note):
            continue
        fetched = note.get("fetched_at", fallback)
        if not isinstance(fetched, (int, float)):
            continue
        note = {**note, "fetched_at": fetched}
        db.execute("INSERT INTO notes VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                   "fetched_at=excluded.fetched_at, body=excluded.body WHERE excluded.fetched_at > notes.fetched_at",
                   (note["id"], fetched, json.dumps(note, ensure_ascii=False)))


def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA_DIR / "note_cache.sqlite3", timeout=30)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, fetched_at REAL, body TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY)")
        with db:
            # Serialize the one-time legacy import with writers in other CLI processes.
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM metadata WHERE key='imported'").fetchone():
                for f in (DATA_DIR / "research").glob("*/notes.json"):
                    try:
                        notes = read_json(f)
                        if isinstance(notes, list):
                            _put(db, notes, f.stat().st_mtime)
                    except (OSError, ValueError):
                        continue
                db.execute("INSERT INTO metadata VALUES ('imported')")
        return db
    except Exception:
        db.close()
        raise


def save(notes):
    db = _connect()
    try:
        with db:
            _put(db, notes, time.time())
    finally:
        db.close()


def lookup_many(ids, days=7):
    ids = list(dict.fromkeys(ids))
    if not ids:
        return {}
    db = _connect()
    try:
        out = {}
        for start in range(0, len(ids), 400):
            batch = ids[start:start + 400]
            rows = db.execute(f"SELECT id, body FROM notes WHERE fetched_at > ? AND id IN ({','.join('?' for _ in batch)})",
                              [time.time() - days * 86400, *batch])
            out.update((nid, json.loads(body)) for nid, body in rows)
        return out
    finally:
        db.close()
