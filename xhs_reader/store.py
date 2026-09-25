"""Research sessions live in data/research/<id>/{meta.json, notes.json, report.md}."""
import json
import re
import time
from pathlib import Path

from .scraper import DATA_DIR

RESEARCH_DIR = DATA_DIR / "research"


def new_session(keyword, question=""):
    slug = re.sub(r"[^\w一-鿿]+", "-", keyword).strip("-")[:30]
    sid = time.strftime("%Y%m%d-%H%M%S") + "_" + slug
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
    return json.loads((path(sid) / "meta.json").read_text())


def write_meta(sid, meta):
    (path(sid) / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))


def update_meta(sid, **kw):
    meta = read_meta(sid)
    meta.update(kw)
    write_meta(sid, meta)


def log(sid, line):
    meta = read_meta(sid)
    meta["log"] = (meta.get("log") or [])[-200:] + [line]
    write_meta(sid, meta)


def save_notes(sid, notes):
    (path(sid) / "notes.json").write_text(json.dumps(notes, ensure_ascii=False, indent=2))


def read_notes(sid):
    f = path(sid) / "notes.json"
    return json.loads(f.read_text()) if f.exists() else []


def read_report(sid):
    f = path(sid) / "report.md"
    return f.read_text() if f.exists() else None


def list_sessions():
    if not RESEARCH_DIR.exists():
        return []
    out = []
    for d in sorted(RESEARCH_DIR.iterdir(), reverse=True):
        if (d / "meta.json").exists():
            m = json.loads((d / "meta.json").read_text())
            m.pop("log", None)
            m["has_report"] = (d / "report.md").exists()
            out.append(m)
    return out
