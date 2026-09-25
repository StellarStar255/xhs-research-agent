"""Xiaohongshu scraping via a persistent (logged-in) Chrome profile.

Data is captured from the site's own XHR responses where possible (search list,
comments), and from window.__INITIAL_STATE__ for note details, falling back to
the DOM. Everything is paced with random delays to stay low-volume.
"""
import json
import os
import random
import re
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
# XHS_DATA_DIR: separate profile/chats/settings (e.g. a second account, or tests).
DATA_DIR = Path(os.environ.get("XHS_DATA_DIR") or ROOT / "data").expanduser()
PROFILE_DIR = DATA_DIR / "profile"
LOCK_FILE = DATA_DIR / "browser.lock"

HOME = "https://www.xiaohongshu.com/explore"
SEARCH_API = "/search/notes"  # v1 on edith., v2 on so. (2026 layout)
COMMENT_API = "/api/sns/web/v2/comment/page"
ME_API = "/api/sns/web/v2/user/me"


# Pacing & budgets (env-overridable). The site rate-limits note-detail page
# views (error 300013 "访问频繁"); ~100 views in 40 min was enough to trip it.
NOTE_DELAY = (float(os.environ.get("XHS_DELAY_MIN", 6)), float(os.environ.get("XHS_DELAY_MAX", 12)))
HOURLY_CAP = int(os.environ.get("XHS_HOURLY_CAP", 40))
DAILY_CAP = int(os.environ.get("XHS_DAILY_CAP", 150))
COOLDOWN_H = float(os.environ.get("XHS_COOLDOWN_HOURS", 3))
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
        stamps = json.loads(USAGE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        stamps = []
    return [t for t in stamps if t > time.time() - 86400]


def _record_view():
    DATA_DIR.mkdir(exist_ok=True)
    USAGE_FILE.write_text(json.dumps(_usage() + [time.time()]))


def limits():
    """Current cooldown and remaining note-view budget."""
    stamps, now = _usage(), time.time()
    hour_used = sum(t > now - 3600 for t in stamps)
    out = {"hour_left": max(0, HOURLY_CAP - hour_used), "day_left": max(0, DAILY_CAP - len(stamps)),
           "hour_cap": HOURLY_CAP, "day_cap": DAILY_CAP, "cooldown_until": None}
    try:
        cd = json.loads(COOLDOWN_FILE.read_text())
        if cd["until"] > now:
            out["cooldown_until"] = cd["until"]
            out["cooldown_reason"] = cd.get("reason")
    except (FileNotFoundError, ValueError, KeyError):
        pass
    if out["hour_left"] == 0 and stamps:
        out["hour_resets"] = min(t for t in stamps if t > now - 3600) + 3600
    return out


def _start_cooldown(reason):
    until = time.time() + COOLDOWN_H * 3600
    COOLDOWN_FILE.write_text(json.dumps({"until": until, "reason": reason}, ensure_ascii=False))
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
        until = _start_cooldown("小红书风控：访问频繁 (300013)")
        raise Limited(f"触发了小红书风控（访问频繁），已自动暂停抓取到 {_hm(until)}。")


def _pause(lo=1.5, hi=3.5):
    time.sleep(random.uniform(lo, hi))


def _normal_ua(p):
    b = p.chromium.launch(channel="chrome", headless=True)
    ua = b.new_page().evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
    b.close()
    return ua


def _lock_holder_alive():
    try:
        pid = int(LOCK_FILE.read_text().split()[0])
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ValueError, IndexError, ProcessLookupError):
        return False
    except PermissionError:
        return True


@contextmanager
def browser(headless=True, wait_s=900):
    """Only one process may use the Chrome profile; wait for our turn."""
    DATA_DIR.mkdir(exist_ok=True)
    deadline = time.time() + wait_s
    while LOCK_FILE.exists() and _lock_holder_alive():
        if time.time() > deadline:
            raise RuntimeError("浏览器正被另一个任务占用，请稍后再试")
        time.sleep(2)
    LOCK_FILE.write_text(f"{os.getpid()} {time.time()}")
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                channel="chrome",
                headless=headless,
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
                # Headless Chrome advertises "HeadlessChrome" in its UA; look like normal Chrome.
                user_agent=None if not headless else _normal_ua(p),
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--no-sandbox", "--enable-automation"],
            )
            try:
                yield ctx
            finally:
                ctx.close()
    finally:
        LOCK_FILE.unlink(missing_ok=True)


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
    url = f"https://www.xiaohongshu.com/explore/{nid}?xsec_token={quote(token)}&xsec_source=pc_search"
    page.goto(url, wait_until="domcontentloaded")
    _record_view()
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
        "url": url,
        "type": note.get("type") or card.get("type"),
        "title": title,
        "desc": desc,
        "author": user.get("nickname") or user.get("nick_name") or dom["nickname"],
        "author_id": user.get("userId") or user.get("user_id"),
        "time": time.strftime("%Y-%m-%d", time.localtime(ts / 1000)) if ts else dom["date"],
        "ip_location": note.get("ipLocation"),
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
                "ip_location": c.get("ip_location"),
                "replies": [
                    {"user": (s.get("user_info") or {}).get("nickname"), "content": s.get("content")}
                    for s in c.get("sub_comments") or []
                ],
            }
            for c in comments[:max_comments]
        ],
    }


def search(keyword, limit=20, detail=True, max_comments=20, headless=True, progress=print):
    lim = limits()
    if lim["cooldown_until"]:
        raise Limited(f"小红书风控冷却中，{_hm(lim['cooldown_until'])} 之后才能再抓取。")
    budget = min(lim["hour_left"], lim["day_left"]) if detail else limit
    if budget <= 0:
        when = _hm(lim["hour_resets"]) if lim["day_left"] and lim.get("hour_resets") else "明天"
        raise Limited(f"已达到抓取上限（每小时 {HOURLY_CAP} 篇 / 每天 {DAILY_CAP} 篇），{when} 之后可以继续。")
    if budget < limit:
        progress(f"额度只剩 {budget} 篇，本次只抓 {budget} 篇")
        limit = budget
    with browser(headless=headless) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ok, _ = is_logged_in(page)
        if not ok:
            raise NotLoggedIn("尚未登录小红书，请先运行 login")
        _pause()
        progress(f"搜索「{keyword}」……")
        items = _search_items(page, keyword, limit)
        progress(f"找到 {len(items)} 篇笔记")
        notes = []
        next_break = random.randint(4, 6)
        for i, it in enumerate(items, 1):
            card = it.get("note_card", {})
            if not detail:
                notes.append({
                    "id": it["id"],
                    "url": f"https://www.xiaohongshu.com/explore/{it['id']}?xsec_token={quote(it.get('xsec_token', ''))}&xsec_source=pc_search",
                    "type": card.get("type"),
                    "title": card.get("display_title", ""),
                    "author": (card.get("user") or {}).get("nickname") or (card.get("user") or {}).get("nick_name"),
                    "liked": _to_int((card.get("interact_info") or {}).get("liked_count")),
                    "cover": (card.get("cover") or {}).get("url_default"),
                })
                continue
            progress(f"[{i}/{len(items)}] {card.get('display_title') or it['id']}")
            try:
                notes.append(_note_detail(page, it, max_comments))
            except Limited as e:
                e.notes = notes
                raise
            except Exception as e:
                progress(f"  跳过：{e}")
            if i == len(items):
                break
            if i == next_break:
                progress("稍作休息，避免访问过快……")
                _pause(20, 40)
                next_break += random.randint(4, 6)
            else:
                _pause(*NOTE_DELAY)
        return notes
