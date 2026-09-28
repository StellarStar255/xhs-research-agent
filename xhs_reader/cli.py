"""CLI:  python -m xhs_reader.cli {login|status|find|open|digest|...} ...

The agent's tools work in two phases, like a person browsing:
  `find`  searches and prints the results list (titles, authors, likes) — no note is
          opened, so it doesn't use the page-view budget;
  `open`  opens the chosen notes from that list (body + comments), one budget unit each
          unless opened in the last few days.
`digest` prints a session as text; `research` / `search` do find + open-the-top-N in one go.
"""
import argparse
import json
import sys

from . import scraper, store


LOGIN_MSG = "NOT_LOGGED_IN: 小红书还没有登录，或者登录已过期。请不要再重试搜索；告诉用户点击页面上的「扫码登录」按钮，用小红书 App 扫码登录后再问一次。"
LIMITED_HINT = "请不要再重试读取，用已有信息回答，并告诉用户什么时候可以再查。"


def _type(n):
    return "视频" if n.get("type") == "video" else "图文"


def print_digest(session, max_chars=1200, max_comments=8, only=None):
    """Session as text. Unopened notes (search cards) are one line; `only` limits the
    output to those note numbers."""
    meta = store.read_meta(session)
    notes = store.read_notes(session)
    opened = sum(1 for n in notes if n.get("opened", "comments" in n))
    print(f"# 关键词: {meta['keyword']}  笔记数: {len(notes) if only is None else len(only)}  "
          f"(session {session}，已打开 {opened} 篇)\n")
    for i, n in enumerate(notes, 1):
        if only is not None and i not in only:
            continue
        if not n.get("opened", "comments" in n):
            print(f"## [{i}] {n.get('title') or '(无标题)'}  | 作者 {n.get('author')} | {_type(n)} | "
                  f"赞 {n.get('liked', 0)} 藏 {n.get('collected', 0)} 评 {n.get('comment_count', 0)}（未打开）")
            continue
        cached = "（最近打开过，来自缓存）" if n.get("from_cache") else ""
        print(f"## [{i}] {n.get('title')}  | 作者 {n.get('author')} | {n.get('time', '')}{cached}")
        print(f"赞 {n.get('liked', 0)} 藏 {n.get('collected', 0)} 评 {n.get('comment_count', 0)} | {n.get('url')}")
        n_img = len(n.get("images") or [])
        if n_img:
            short = len((n.get("desc") or "").strip()) < 150
            print(f"图片 {n_img} 张" + ("（正文很短，内容可能在图片里）" if short and n.get("type") != "video" else ""))
        if n.get("tags"):
            print("标签: " + " ".join(n["tags"]))
        if n.get("desc"):
            print(n["desc"][:max_chars])
        for c in (n.get("comments") or [])[:max_comments]:
            print(f"  - 评论({c.get('likes', 0)}赞) {c.get('user')}: {c.get('content')}")
        print()


def _logger(sid, echo=False):
    def progress(msg):
        if echo:
            print(msg, flush=True)
        store.log(sid, msg)
    return progress


def cmd_find(a):
    """Phase 1: search results list, no note opened."""
    sid = store.new_session(a.keyword, a.question or "")
    try:
        cards = scraper.search_list(a.keyword, limit=a.limit, progress=_logger(sid))
    except scraper.NotLoggedIn:
        store.update_meta(sid, status="error", error="未登录小红书")
        print(LOGIN_MSG)
        sys.exit(3)
    except scraper.Limited as e:
        store.update_meta(sid, status="limited", count=0, error=str(e))
        print(f"LIMITED: {e} {LIMITED_HINT}")
        sys.exit(2)
    except Exception as e:
        store.update_meta(sid, status="error", error=str(e))
        print(f"SESSION {sid} ERROR: {e}")
        sys.exit(1)
    store.save_notes(sid, cards)
    store.update_meta(sid, status="done", count=len(cards))
    print(f"SESSION {sid}")
    print_digest(sid)
    print("以上笔记都还没打开。用 open_notes 挑与问题最相关的几篇打开（一般 3～5 篇）：同一作者最多 1 篇，"
          "要文字信息时优先图文，评价类问题至少挑 1 篇批评/避雷的，标题明显跑题的不要打开。")


def cmd_open(a):
    """Phase 2: open the chosen notes of a session and print them."""
    notes = store.read_notes(a.session)
    if not notes:
        print(f"ERROR: 找不到 session {a.session} 的笔记。")
        sys.exit(1)
    picks = []
    for i in a.notes:
        if 1 <= i <= len(notes) and i not in picks:
            picks.append(i)
    if not picks:
        print(f"ERROR: 笔记编号不对，这个 session 里是 1～{len(notes)}。")
        sys.exit(1)
    picks = picks[:a.max]
    store.update_meta(a.session, status="running")
    limited = None
    try:
        opened = scraper.open_notes([notes[i - 1] for i in picks], max_comments=a.comments,
                                    progress=_logger(a.session))
    except scraper.NotLoggedIn:
        store.update_meta(a.session, status="done")
        print(LOGIN_MSG)
        sys.exit(3)
    except scraper.Limited as e:
        opened, limited = e.notes, str(e)
    except Exception as e:
        store.update_meta(a.session, status="done")
        print(f"SESSION {a.session} ERROR: {e}")
        sys.exit(1)
    by_id = {n["id"]: n for n in opened}
    done = []
    for i in picks:
        if notes[i - 1]["id"] in by_id:
            notes[i - 1] = {**notes[i - 1], **by_id[notes[i - 1]["id"]], "opened": True}
            done.append(i)
    store.save_notes(a.session, notes)
    store.update_meta(a.session, status="done")
    if limited:
        print(f"LIMITED: {limited} {LIMITED_HINT}")
        if not done:
            sys.exit(2)
    print(f"SESSION {a.session}")
    print(f"已打开 {len(done)} 篇")
    print_digest(a.session, a.max_chars, a.max_comments, only=set(done))


def cmd_search(a, echo=True):
    """find + open the top N (CLI convenience; also `research`)."""
    sid = a.session or store.new_session(a.keyword, a.question or "")
    progress = _logger(sid, echo)
    try:
        notes = scraper.search(a.keyword, limit=a.limit, max_comments=a.comments,
                               headless=not getattr(a, "headed", False), progress=progress)
    except scraper.Limited as e:
        store.save_notes(sid, e.notes)
        store.update_meta(sid, status="limited", count=len(e.notes), error=str(e))
        print(f"LIMITED: {e} {LIMITED_HINT}")
        sys.exit(2)
    except scraper.NotLoggedIn:
        store.update_meta(sid, status="error", error="未登录小红书")
        print(LOGIN_MSG)
        sys.exit(3)
    except Exception as e:
        store.update_meta(sid, status="error", error=str(e))
        print(f"SESSION {sid} ERROR: {e}")
        sys.exit(1)
    store.save_notes(sid, notes)
    store.update_meta(sid, status="done", count=len(notes))
    print(f"SESSION {sid}")
    if not echo:
        print_digest(sid, a.max_chars, a.max_comments)


def main(argv=None):
    from .paths import use_system_certificates
    use_system_certificates()
    ap = argparse.ArgumentParser(prog="xhs_reader")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    sub.add_parser("status")
    f = sub.add_parser("find", help="search; print the results list without opening any note")
    f.add_argument("keyword")
    f.add_argument("-n", "--limit", type=int, default=20)
    f.add_argument("-q", "--question")
    o = sub.add_parser("open", help="open chosen notes of a session (numbers from the list)")
    o.add_argument("session")
    o.add_argument("notes", type=int, nargs="+")
    o.add_argument("--max", type=int, default=6, help="at most this many notes per call")
    o.add_argument("-c", "--comments", type=int, default=15)
    o.add_argument("--max-chars", type=int, default=1200)
    o.add_argument("--max-comments", type=int, default=8)
    for name, n_default in (("search", 20), ("research", 8)):
        s = sub.add_parser(name, help="find + open the top N in one go")
        s.add_argument("keyword")
        s.add_argument("-n", "--limit", type=int, default=n_default)
        s.add_argument("-c", "--comments", type=int, default=15)
        s.add_argument("-q", "--question")
        s.add_argument("--session", help="reuse an existing (empty) session id")
        s.add_argument("--headed", action="store_true", help="show the browser window")
        s.add_argument("--max-chars", type=int, default=1200)
        s.add_argument("--max-comments", type=int, default=8)
    d = sub.add_parser("digest")
    d.add_argument("session")
    d.add_argument("--max-chars", type=int, default=1500)
    d.add_argument("--max-comments", type=int, default=10)
    sub.add_parser("list")
    sub.add_parser("limits", help="show cooldown and remaining budget")
    sub.add_parser("selftest", help="check HTTPS and that the bundled driver can start the local browser")
    sub.add_parser("mcp", help="run the scraper as an MCP server on stdio (used by the agent backends)")
    im = sub.add_parser("images", help="download a note's images (from a digest) and print their paths")
    im.add_argument("session")
    im.add_argument("note", type=int, help="note number as shown in the digest, e.g. 3 for [3]")
    im.add_argument("-n", "--limit", type=int, default=4)
    a = ap.parse_args(argv)

    if a.cmd == "login":
        me = scraper.login()
        print(f"已登录：{me.get('nickname')}")
    elif a.cmd == "status":
        print(json.dumps(scraper.status(), ensure_ascii=False))
    elif a.cmd == "find":
        cmd_find(a)
    elif a.cmd == "open":
        cmd_open(a)
    elif a.cmd == "search":
        cmd_search(a, echo=True)
    elif a.cmd == "research":
        cmd_search(a, echo=False)
    elif a.cmd == "digest":
        print_digest(a.session, a.max_chars, a.max_comments)
    elif a.cmd == "images":
        from . import images
        try:
            title, paths = images.note_images(a.session, a.note, a.limit)
        except images.ImageError as e:
            print(f"IMAGES_ERROR: {e}")
            sys.exit(1)
        print(f"IMAGES {len(paths)} 「{title}」")
        for p in paths:
            print(f"IMAGE {p}")
    elif a.cmd == "mcp":
        from .mcp_server import serve
        serve()
    elif a.cmd == "selftest":
        # HTTPS with the bundled Python (update checks, model APIs); github.com has no API rate limit.
        import urllib.request
        with urllib.request.urlopen(urllib.request.Request("https://github.com", method="HEAD"), timeout=20) as r:
            tls = r.status
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            channel, ua = scraper._pick_browser(p)
        print(json.dumps({"ok": True, "https": tls, "browser": channel, "user_agent": ua}, ensure_ascii=False))
    elif a.cmd == "limits":
        print(json.dumps(scraper.limits(), ensure_ascii=False))
    elif a.cmd == "list":
        for m in store.list_sessions():
            print(f"{m['id']}  {m['status']}  {m.get('count', '-')} notes  report={'Y' if m['has_report'] else 'N'}")


if __name__ == "__main__":
    main()
