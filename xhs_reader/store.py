"""Research sessions live in data/research/<id>/{meta.json, notes.json, report.md}."""
import re
import time
import uuid

from .paths import DATA_DIR
from .persistence import atomic_json, file_lock, read_json, quarantine

RESEARCH_DIR = DATA_DIR / "research"


def new_session(keyword, question=""):
    slug = re.sub(r"[^\w一-鿿]+", "-", keyword).strip("-")[:30]
    sid = time.strftime("%Y%m%d-%H%M%S") + "_" + slug + "_" + uuid.uuid4().hex[:8]
    d = RESEARCH_DIR / sid
    d.mkdir(parents=True)
    write_meta(sid, {"id": sid, "keyword": keyword, "question": question,
                     "created": time.strftime("%Y-%m-%d %H:%M"), "status": "running", "log": []})
    return sid


def path(sid):
    d = (RESEARCH_DIR / sid).resolve()
    if d.parent != RESEARCH_DIR.resolve():
        raise ValueError("bad session id")
    return d


def read_meta(sid):
    return read_json(path(sid) / "meta.json")


def write_meta(sid, meta):
    atomic_json(path(sid) / "meta.json", meta)


def update_meta(sid, **kw):
    with file_lock(path(sid) / "meta.lock"):
        meta = read_meta(sid)
        meta.update(kw)
        write_meta(sid, meta)


def log(sid, line):
    with file_lock(path(sid) / "meta.lock"):
        meta = read_meta(sid)
        meta["log"] = (meta.get("log") or [])[-199:] + [line]
        write_meta(sid, meta)


def save_notes(sid, notes):
    from . import note_cache
    for n in notes:
        if n.get("opened", "comments" in n) and not n.get("from_cache"):
            n.setdefault("fetched_at", time.time())
    atomic_json(path(sid) / "notes.json", notes)
    note_cache.save(notes)


def read_notes(sid):
    f = path(sid) / "notes.json"
    if not f.exists():
        return []
    # Legacy opened notes have no fetch time. Freeze the old file time before a
    # subsequent write, rather than renewing every old note in that session.
    modified = f.stat().st_mtime
    notes = read_json(f)
    if not isinstance(notes, list) or any(not isinstance(n, dict) for n in notes):
        raise ValueError("bad notes record")
    for n in notes:
        if n.get("opened", "comments" in n):
            n.setdefault("fetched_at", modified)
    return notes


def read_report(sid):
    f = path(sid) / "report.md"
    return f.read_text(encoding="utf-8") if f.exists() else None


def list_sessions():
    if not RESEARCH_DIR.exists():
        return []
    out = []
    for d in sorted(RESEARCH_DIR.iterdir(), reverse=True):
        if (d / "meta.json").exists():
            try:
                m = read_meta(d.name)
                if not isinstance(m, dict) or not all(k in m for k in ("id", "status")):
                    raise ValueError("bad research metadata")
            except ValueError:
                quarantine(d / "meta.json")
                continue
            except OSError:
                continue
            m.pop("log", None)
            m["has_report"] = (d / "report.md").exists()
            out.append(m)
    return out
