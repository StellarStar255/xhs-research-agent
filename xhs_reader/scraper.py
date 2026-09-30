"""Xiaohongshu scraping via a persistent (logged-in) Chrome profile.

Data is captured from the site's own XHR responses where possible (search list,
comments), and from window.__INITIAL_STATE__ for note details, falling back to
the DOM. Everything is paced with random delays to stay low-volume.
"""
import os
import random
import re
import time
from contextlib import contextmanager
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from .paths import DATA_DIR
from .persistence import atomic_json, file_lock, read_json

PROFILE_DIR = DATA_DIR / "profile"
LOCK_FILE = DATA_DIR / "browser.lock"

HOME = "https://www.xiaohongshu.com/explore"
SEARCH_API = "/search/notes"  # v1 on edith., v2 on so. (2026 layout)
COMMENT_API = "/api/sns/web/v2/comment/page"
ME_API = "/api/sns/web/v2/user/me"


# Fixed pacing & budgets. Viewing too many notes in a short time
# gets the account rate-limited by the site (error 300013 "访问频繁").
NOTE_DELAY = (6, 12)   # seconds between notes
HOURLY_CAP = 60
DAILY_CAP = 300
COOLDOWN_H = 3         # hours to pause after the site says we're too frequent
USAGE_FILE = DATA_DIR / "usage.json"        # timestamps of note-detail views
COOLDOWN_FILE = DATA_DIR / "cooldown.json"  # {"until": ts, "reason": ...}


class NotLoggedIn(RuntimeError):
    pass


class Limited(RuntimeError):
    """Rate-limited by the site, in cooldown, or out of budget. Carries partial notes."""

    def __init__(self, msg, notes=None):
        super().__init__(msg)
        self.notes = notes or []


def _hm(ts):
    return time.strftime("%H:%M" if time.time() + 86400 > ts else "%m-%d %H:%M", time.localtime(ts))


def _usage():
    try:
        stamps = read_json(USAGE_FILE)
    except FileNotFoundError:
        stamps = []
    except ValueError as e:
        raise RuntimeError("阅读额度记录损坏，请先恢复 usage.json；已暂停读取") from e
    if not isinstance(stamps, list) or any(not isinstance(t, (int, float)) for t in stamps):
        raise RuntimeError("阅读额度记录损坏，请先恢复 usage.json；已暂停读取")
    return [t for t in stamps if t > time.time() - 86400]


def _record_view():
    DATA_DIR.mkdir(exist_ok=True)
    atomic_json(USAGE_FILE, _usage() + [time.time()])


def limits():
    """Current cooldown and remaining note-view budget. Both windows roll: "hour" is the
    last 60 minutes and "day" the last 24 hours, not the clock hour or calendar day."""
    stamps, now = _usage(), time.time()
    hour_used = sum(t > now - 3600 for t in stamps)
    out = {"hour_left": max(0, HOURLY_CAP - hour_used), "day_left": max(0, DAILY_CAP - len(stamps)),
           "hour_cap": HOURLY_CAP, "day_cap": DAILY_CAP, "cooldown_until": None}
    try:
        cd = read_json(COOLDOWN_FILE)
        if cd["until"] > now:
            out["cooldown_until"] = cd["until"]
            out["cooldown_reason"] = cd.get("reason")
    except FileNotFoundError:
        pass
    except (ValueError, KeyError, TypeError) as e:
        raise RuntimeError("冷却记录损坏，请先恢复 cooldown.json；已暂停读取") from e
    # Each view frees its slot one hour / one day after it happened: *_next is when the
    # next slot comes back, *_full when every slot is back.
    in_hour = [t for t in stamps if t > now - 3600]
    if in_hour:
        out["hour_next"], out["hour_full"] = min(in_hour) + 3600, max(in_hour) + 3600
    if stamps:
        out["day_next"], out["day_full"] = min(stamps) + 86400, max(stamps) + 86400
    if out["hour_left"] == 0 and in_hour:
        out["hour_resets"] = out["hour_next"]
    if out["day_left"] == 0 and stamps:
        out["day_resets"] = out["day_next"]  # when the oldest counted view drops out
    return out


def _start_cooldown(reason):
    until = time.time() + COOLDOWN_H * 3600
    atomic_json(COOLDOWN_FILE, {"until": until, "reason": reason})
    return until


def _check_blocked(page):
    """Raise Limited if the site is showing its rate-limit / captcha page."""
    url = page.url
    blocked = "website-login/error" in url or "website-login/captcha" in url
    if not blocked:
        try:
            text = page.evaluate("() => document.body ? document.body.innerText.slice(0, 400) : ''")
        except Exception:
            text = ""
        blocked = any(k in text for k in ("访问频繁", "安全限制", "300013", "请完成验证", "滑块"))
    if blocked:
        until = _start_cooldown("小红书提示访问过于频繁 (300013)")
        raise Limited(f"小红书提示访问过于频繁，已暂停读取，{_hm(until)} 之后再试。")


def _pause(lo=1.5, hi=3.5):
    time.sleep(random.uniform(lo, hi))


class NoBrowser(RuntimeError):
    pass


def _pick_browser(p):
    """(channel, user agent) of an installed Chrome, else Edge (always present on Windows).

    We drive the user's own browser rather than bundling Chromium, and don't disguise it:
    the user agent is whatever the browser reports (including "HeadlessChrome" when
    headless), and automation is visible to sites (navigator.webdriver).
    """
    wanted = os.environ.get("XHS_BROWSER_CHANNEL")
    for channel in [wanted] if wanted else ["chrome", "msedge"]:
        try:
            b = p.chromium.launch(channel=channel, headless=True)
        except PlaywrightError:
            continue
        ua = b.new_page().evaluate("navigator.userAgent")
        b.close()
        return channel, ua
    raise NoBrowser("没有找到 Google Chrome。请先安装 Chrome（https://www.google.com/chrome/）后再试。")


def browser_busy():
    """Check real lock ownership; a PID marker left by a crash is not ownership."""
    try:
        with file_lock(DATA_DIR / "browser.guard", timeout=0):
            return False
    except TimeoutError:
        return True


@contextmanager
def browser(headless=True, wait_s=900):
    """Only one process may use the Chrome profile; wait for our turn.

    headless=True runs per the "browser_window" setting: "background" is headless Chrome;
    "visible" shows a normal Chrome window on screen so the user can watch each step.
    Either way the site sees an automated browser; when it rate-limits us we stop and
    cool down rather than trying another way in.
    """
    from . import settings
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if headless and settings.load().get("browser_window") == "visible":
        headless = False
    with file_lock(DATA_DIR / "browser.guard", timeout=wait_s):
        # Keep the PID marker for status/UI compatibility. Ownership is the OS lock.
        LOCK_FILE.write_text(f"{os.getpid()} {time.time()}", encoding="utf-8")
        try:
            with _browser_context(headless) as ctx:
                yield ctx
        finally:
            LOCK_FILE.unlink(missing_ok=True)


@contextmanager
def _browser_context(headless):
    with sync_playwright() as p:
        channel, _ = _pick_browser(p)
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            channel=channel,
            headless=headless,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
            # Keep Chrome's sandbox on (Playwright adds --no-sandbox by default). No flags
            # that hide automation and no user-agent override: this is a plain, visible
            # automated Chrome using the user's own login.
            ignore_default_args=["--no-sandbox"],
        )
        try:
            yield ctx
        finally:
            ctx.close()


def _me(page):
    """Return the /user/me payload observed while loading the home page."""
    with page.expect_response(lambda r: ME_API in r.url, timeout=20000) as info:
        page.goto(HOME, wait_until="domcontentloaded")
    try:
        return info.value.json().get("data") or {}
    except Exception:
        return {}


def is_logged_in(page):
    data = _me(page)
    return bool(data) and not data.get("guest", True), data


def login(timeout_s=300):
    """Open a visible window and wait for the user to scan the QR code."""
    with browser(headless=False) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ok, me = is_logged_in(page)
        if ok:
            return me
        print("请在弹出的浏览器窗口中用小红书 App 扫码登录……", flush=True)
        # Only listen passively here: navigating would regenerate the QR code.
        # After a successful scan the site itself re-fetches user/me.
        found = {}

        def on_response(resp):
            if ME_API in resp.url:
                try:
                    data = resp.json().get("data") or {}
                    if data and not data.get("guest", True):
                        found.update(data)
                except Exception:
                    pass

        ctx.on("response", on_response)
        deadline = time.time() + timeout_s
        while time.time() < deadline and not found:
            page.wait_for_timeout(2000)
            try:
                me = page.evaluate("""() => {
                  const un = v => (v && typeof v === 'object' && '_value' in v) ? v._value : v;
                  const u = window.__INITIAL_STATE__?.user;
                  return un(u?.loggedIn) ? JSON.parse(JSON.stringify(un(u.userInfo) || {})) : null;
                }""")
                if me is not None:
                    found.update(me or {"nickname": ""})
            except Exception:
                pass  # page mid-navigation
        if found:
            page.wait_for_timeout(3000)
            return found
        raise NotLoggedIn("登录超时")


def status():
    with browser(headless=True) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ok, me = is_logged_in(page)
        return {"logged_in": ok, "nickname": me.get("nickname"), "user_id": me.get("user_id")}


def _to_int(v):
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace(",", "")
    m = re.match(r"([\d.]+)\s*([万wW千kK]?)", s)
    if not m:
        return 0
    n = float(m.group(1))
    return int(n * {"万": 1e4, "w": 1e4, "W": 1e4, "千": 1e3, "k": 1e3, "K": 1e3}.get(m.group(2), 1))


def _search_items(page, keyword, limit):
    items, seen = [], set()

    def on_response(resp):
        if SEARCH_API in resp.url and resp.request.method == "POST":
            try:
                for it in resp.json().get("data", {}).get("items", []) or []:
                    if it.get("model_type") == "note" and it["id"] not in seen:
                        seen.add(it["id"])
                        items.append(it)
            except Exception:
                pass

    page.on("response", on_response)
    try:
        # Type into the site's own search box like a person; deep-linking to the
        # results page tends to hang. Fall back to the URL if no box is found.
        if HOME not in page.url:
            page.goto(HOME, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
        box = None
        for sel in ("#search-input-in-feeds", "#search-input", "textarea[placeholder]", "input[placeholder*='搜索']"):
            cands = page.locator(sel)
            box = next((cands.nth(i) for i in range(cands.count()) if cands.nth(i).is_visible()), None)
            if box:
                break
        if box:
            box.focus()  # click() can be intercepted by overlapping search widgets
            page.keyboard.type(keyword, delay=random.randint(80, 160))
            page.wait_for_timeout(600)
            page.keyboard.press("Enter")
        else:
            page.goto(f"https://www.xiaohongshu.com/search_result?keyword={quote(keyword)}&source=web_explore_feed",
                      wait_until="commit")
        page.wait_for_timeout(5000)
        _check_blocked(page)
        stale = 0
        while len(items) < limit and stale < 4:
            before = len(items)
            page.mouse.wheel(0, 3000)
            _pause(1.5, 2.5)
            stale = stale + 1 if len(items) == before else 0
    finally:
        page.remove_listener("response", on_response)
    return items[:limit]


_STATE_JS = """
(id) => {
  const unwrap = v => (v && typeof v === 'object' && ('_value' in v || '_rawValue' in v)) ? (v._rawValue ?? v._value) : v;
  try {
    const st = window.__INITIAL_STATE__;
    const map = unwrap(unwrap(st.note).noteDetailMap);
    const entry = map[id] || Object.values(map)[0];
    return JSON.parse(JSON.stringify(unwrap(entry).note));
  } catch (e) { return null; }
}
"""

_DOM_JS = """
() => ({
  title: document.querySelector('#detail-title')?.innerText || '',
  desc: document.querySelector('#detail-desc')?.innerText || '',
  nickname: document.querySelector('.author .username, .author-wrapper .username')?.innerText || '',
  date: document.querySelector('.date')?.innerText || '',
})
"""


def _note_detail(page, item, max_comments):
    nid, token = item["id"], item.get("xsec_token", "")
    comments = []

    def on_response(resp):
        if COMMENT_API in resp.url:
            try:
                comments.extend(resp.json().get("data", {}).get("comments", []) or [])
            except Exception:
                pass

    page.on("response", on_response)
    try:
        url = f"https://www.xiaohongshu.com/explore/{nid}?xsec_token={quote(token)}&xsec_source=pc_search"
        _record_view()
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(random.randint(2500, 4000))
        _check_blocked(page)
        # Scroll through the note like a reader; this also loads comments.
        scroller = page.locator(".note-scroller")
        for _ in range(random.randint(1, 3)):
            if scroller.count():
                scroller.first.evaluate(f"el => el.scrollBy(0, {random.randint(300, 900)})")
            page.wait_for_timeout(random.randint(700, 1600))
        if max_comments > 20:
            scroller = page.locator(".note-scroller")
            for _ in range(max_comments // 10):
                if len(comments) >= max_comments or scroller.count() == 0:
                    break
                scroller.first.evaluate("el => el.scrollBy(0, 2000)")
                page.wait_for_timeout(1200)
    finally:
        page.remove_listener("response", on_response)

    note = page.evaluate(_STATE_JS, nid) or {}
    dom = page.evaluate(_DOM_JS)
    if not note and not dom["desc"] and not dom["title"]:
        _check_blocked(page)  # an empty page is usually a soft block
    card = item.get("note_card", {})
    inter = note.get("interactInfo") or {}
    user = note.get("user") or card.get("user") or {}
    ts = note.get("time")
    desc = (note.get("desc") or dom["desc"] or "").replace("[话题]#", "")
    title = note.get("title") or dom["title"] or card.get("display_title", "")
    if not title.strip():
        title = next((ln.strip() for ln in desc.splitlines() if ln.strip()), "")[:40]

    return {
        "id": nid,
        "fetched_at": time.time(),
        "url": url,
        "type": note.get("type") or card.get("type"),
        "title": title,
        "desc": desc,
        "author": user.get("nickname") or user.get("nick_name") or dom["nickname"],
        "author_id": user.get("userId") or user.get("user_id"),
        "time": time.strftime("%Y-%m-%d", time.localtime(ts / 1000)) if ts else dom["date"],
        "tags": [t.get("name") for t in note.get("tagList") or [] if t.get("name")],
        "liked": _to_int(inter.get("likedCount") or card.get("interact_info", {}).get("liked_count")),
        "collected": _to_int(inter.get("collectedCount")),
        "comment_count": _to_int(inter.get("commentCount")),
        "shared": _to_int(inter.get("shareCount")),
        "cover": (card.get("cover") or {}).get("url_default") or (card.get("cover") or {}).get("url"),
        "images": [i.get("urlDefault") or i.get("url") for i in note.get("imageList") or [] if i],
        "comments": [
            {
                "user": (c.get("user_info") or {}).get("nickname"),
                "content": c.get("content"),
                "likes": _to_int(c.get("like_count")),
                "replies": [
                    {"user": (s.get("user_info") or {}).get("nickname"), "content": s.get("content")}
                    for s in c.get("sub_comments") or []
                ],
            }
            for c in comments[:max_comments]
        ],
    }


def _card(it):
    """A search-result card, stored as-is in notes.json until the note is opened."""
    card = it.get("note_card") or {}
    user = card.get("user") or {}
    inter = card.get("interact_info") or {}
    return {
        "id": it["id"],
        "xsec_token": it.get("xsec_token", ""),
        "url": f"https://www.xiaohongshu.com/explore/{it['id']}?xsec_token={quote(it.get('xsec_token', ''))}&xsec_source=pc_search",
        "type": card.get("type"),
        "title": card.get("display_title", ""),
        "author": user.get("nickname") or user.get("nick_name"),
        "liked": _to_int(inter.get("liked_count")),
        "collected": _to_int(inter.get("collected_count")),
        "comment_count": _to_int(inter.get("comment_count")),
        "cover": (card.get("cover") or {}).get("url_default"),
        "opened": False,
    }


def _as_item(card):
    """Rebuild the search-API shape _note_detail expects from a stored card."""
    return {"id": card["id"], "xsec_token": card.get("xsec_token", ""), "note_card": {
        "display_title": card.get("title", ""), "type": card.get("type"),
        "user": {"nickname": card.get("author")}, "interact_info": {"liked_count": card.get("liked")},
        "cover": {"url_default": card.get("cover")}}}


def _check_cooldown():
    lim = limits()
    if lim["cooldown_until"]:
        raise Limited(f"小红书提示访问过于频繁，已暂停读取，{_hm(lim['cooldown_until'])} 之后再试。")
    return lim


def search_list(keyword, limit=20, headless=True, progress=print):
    """Phase 1: the search results list only (titles, authors, likes). No note page is
    opened, so this doesn't use the page-view budget."""
    _check_cooldown()
    with browser(headless=headless) as ctx:
        _check_cooldown()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ok, _ = is_logged_in(page)
        if not ok:
            raise NotLoggedIn("尚未登录小红书，请先运行 login")
        _pause()
        progress(f"搜索「{keyword}」……")
        cards = [_card(it) for it in _search_items(page, keyword, limit)]
        progress(f"找到 {len(cards)} 篇笔记")
        return cards


CACHE_DAYS = 7


def cached_detail(note_id):
    """An opened copy of this note from the last CACHE_DAYS days, if any."""
    from .note_cache import lookup_many
    return lookup_many([note_id], CACHE_DAYS).get(note_id)


def open_notes(cards, max_comments=20, headless=True, progress=print):
    """Phase 2: open the chosen notes (body + comments). Each note not in the recent cache
    costs one page view against the hourly/daily budget. Returns the opened notes."""
    _check_cooldown()
    from .note_cache import lookup_many
    hits = lookup_many([c["id"] for c in cards], CACHE_DAYS)
    opened, todo = [], []
    for c in cards:
        hit = hits.get(c["id"])
        if hit:
            opened.append({**hit, "opened": True, "from_cache": True})
        else:
            todo.append(c)
    if opened:
        progress(f"{len(opened)} 篇最近打开过，直接用缓存")
    if not todo:
        return opened
    with browser(headless=headless) as ctx:
        lim = _check_cooldown()
        return _open_in_browser(ctx, todo, opened, max_comments, progress, lim)


def _open_in_browser(ctx, todo, opened, max_comments, progress, lim):
    budget = min(lim["hour_left"], lim["day_left"])
    if budget <= 0:
        resets = lim.get("day_resets") if not lim["day_left"] else lim.get("hour_resets")
        when = _hm(resets) if resets else "稍后"
        raise Limited(f"已达到本应用主动设定的阅读上限（1 小时内最多 {HOURLY_CAP} 篇、24 小时内最多 {DAILY_CAP} 篇，"
                      f"为了减少对小红书的访问压力、保护账号；不是小红书的限制），{when} 之后可以继续。", opened)
    if budget < len(todo):
        progress(f"额度只剩 {budget} 篇，本次只打开 {budget} 篇")
        todo = todo[:budget]
    if todo:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ok, _ = is_logged_in(page)
        if not ok:
            raise NotLoggedIn("尚未登录小红书，请先运行 login")
        next_break = random.randint(4, 6)
        for i, c in enumerate(todo, 1):
            if i == next_break:  # a longer rest every few notes
                progress("稍作休息……")
                _pause(20, 40)
                next_break += random.randint(4, 6)
            elif i > 1:
                _pause(*NOTE_DELAY)
            progress(f"[{i}/{len(todo)}] {c.get('title') or c['id']}")
            current = _check_cooldown()
            if min(current["hour_left"], current["day_left"]) <= 0:
                raise Limited("已达到本应用主动设定的阅读上限，请稍后再试。", opened)
            try:
                opened.append({**_note_detail(page, _as_item(c), max_comments), "opened": True})
            except Limited as e:
                e.notes = opened
                raise
            except Exception as e:
                progress(f"  跳过：{e}")
    return opened


def search(keyword, limit=20, max_comments=20, headless=True, progress=print):
    """List + open the top `limit` results in one go (CLI `search` / `research`)."""
    cards = search_list(keyword, limit=limit, headless=headless, progress=progress)
    return open_notes(cards, max_comments=max_comments, headless=headless, progress=progress)
