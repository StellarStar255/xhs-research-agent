"""Let the agent look at a note's images (many notes put their content in the pictures).

Images come from Xiaohongshu's image CDN using the URLs saved at scrape time; no note
page is opened, so this doesn't use the page-view budget. The signed URLs expire after a
while, so this works best within the same research turn. Files are cached under
data/research/<session>/images/<note id>/.

Budgets: MAX_PER_NOTE images per call, MAX_PER_TURN per research turn. The turn is
identified by XHS_TURN_ID (set by the agent backends for the processes they start), so
repeated CLI calls within one turn share the budget.
"""
import os
import re
import time
import urllib.request

from . import store
from .paths import DATA_DIR
from .persistence import atomic_json, file_lock, read_json

MAX_PER_NOTE = 4
MAX_PER_TURN = 12
_TURN_FILE = DATA_DIR / "image_turns.json"
_EXT = {"image/webp": "webp", "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif"}
MIME = {v: k for k, v in _EXT.items()}


class ImageError(RuntimeError):
    pass


def _take_budget(turn_id, n):
    """Reserve up to n images for this turn; returns how many are allowed."""
    if not turn_id:
        return n
    with file_lock(_TURN_FILE.with_suffix(".lock")):
        return _reserve_budget(turn_id, n)


def _reserve_budget(turn_id, n):
    try:
        turns = read_json(_TURN_FILE)
    except FileNotFoundError:
        turns = {}
    except ValueError as e:
        raise ImageError("图片额度记录损坏，请先恢复 image_turns.json") from e
    if not isinstance(turns, dict) or any(
            not isinstance(v, dict) or not isinstance(v.get("at"), (int, float)) or
            not isinstance(v.get("used"), int) or v["used"] < 0 for v in turns.values()):
        raise ImageError("图片额度记录损坏，请先恢复 image_turns.json")
    now = time.time()
    turns = {k: v for k, v in turns.items() if now - v.get("at", 0) < 6 * 3600}  # forget old turns
    used = turns.get(turn_id, {}).get("used", 0)
    allowed = max(0, min(n, MAX_PER_TURN - used))
    turns[turn_id] = {"used": used + allowed, "at": now}
    _TURN_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(_TURN_FILE, turns)
    return allowed


def note_images(session, index, limit=MAX_PER_NOTE, turn_id=None):
    """Download (or reuse) up to `limit` images of note number `index` (1-based, as in the
    digest). Returns (title, [paths]). Raises ImageError with a message for the agent."""
    notes = store.read_notes(session)
    if not notes:
        raise ImageError(f"找不到 session {session} 的笔记。")
    if not 1 <= index <= len(notes):
        raise ImageError(f"session {session} 里只有 {len(notes)} 篇笔记，没有第 {index} 篇。")
    note = notes[index - 1]
    urls = [u for u in note.get("images") or [] if u]
    title = note.get("title") or f"第 {index} 篇"
    if not urls:
        raise ImageError(f"「{title}」没有图片。")
    want = min(len(urls), max(1, min(int(limit or MAX_PER_NOTE), MAX_PER_NOTE)))
    allowed = _take_budget(turn_id or os.environ.get("XHS_TURN_ID"), want)
    if allowed <= 0:
        raise ImageError(f"本轮已经看了 {MAX_PER_TURN} 张图片，达到上限。请用已有信息回答。")
    nid = str(note.get("id") or index)
    if not re.fullmatch(r"[\w-]+", nid):
        raise ImageError("无效的笔记 ID")
    folder = store.path(session) / "images" / nid
    folder.mkdir(parents=True, exist_ok=True)
    paths, expired = [], 0
    for i, url in enumerate(urls[:allowed], 1):
        cached = next(iter(sorted(folder.glob(f"{i}.*"))), None)
        if cached:
            paths.append(cached)
            continue
        try:
            req = urllib.request.Request(url.replace("http://", "https://", 1),
                                         headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.xiaohongshu.com/"})
            with urllib.request.urlopen(req, timeout=20) as r:
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
                if ctype not in _EXT:
                    expired += 1
                    continue
                dest = folder / f"{i}.{_EXT[ctype]}"
                dest.write_bytes(r.read())
                paths.append(dest)
        except Exception:
            expired += 1
    if not paths:
        raise ImageError(f"「{title}」的图片下载失败，图片链接可能已经过期；需要的话可以重新搜索这个话题。")
    return title, paths
