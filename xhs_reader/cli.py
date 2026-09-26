"""CLI:  python -m xhs_reader.cli {login|status|search|digest} ...

`search` scrapes notes into a new research session and prints its id;
`digest` prints a compact text version of a session for summarisation.
"""
import argparse
import json
import sys

from . import scraper, store


LOGIN_MSG = "NOT_LOGGED_IN: 小红书还没有登录，或者登录已过期。请不要再重试搜索；告诉用户点击页面上的「扫码登录」按钮，用小红书 App 扫码登录后再问一次。"


def cmd_search(a):
    sid = a.session or store.new_session(a.keyword, a.question or "")
    def progress(msg):
        print(msg, flush=True)
        store.log(sid, msg)
    try:
        notes = scraper.search(a.keyword, limit=a.limit, detail=not a.no_detail,
                               max_comments=a.comments, headless=not a.headed, progress=progress)
        store.save_notes(sid, notes)
        store.update_meta(sid, status="done", count=len(notes))
        print(f"SESSION {sid}  ({len(notes)} notes) -> {store.path(sid)}")
    except scraper.Limited as e:
        store.save_notes(sid, e.notes)
        store.update_meta(sid, status="limited", count=len(e.notes), error=str(e))
        print(f"LIMITED: {e}  (saved {len(e.notes)} notes in {sid})", file=sys.stderr)
        sys.exit(2)
    except scraper.NotLoggedIn:
        store.update_meta(sid, status="error", error="未登录小红书")
        print(LOGIN_MSG, file=sys.stderr)
        sys.exit(3)
    except Exception as e:
        store.update_meta(sid, status="error", error=str(e))
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_research(a):
    """Search + digest in one step; this is the chat agent's tool."""
    sid = store.new_session(a.keyword, a.question or "")
    try:
        notes = scraper.search(a.keyword, limit=a.limit, max_comments=a.comments,
                               progress=lambda m: store.log(sid, m))
        store.save_notes(sid, notes)
        store.update_meta(sid, status="done", count=len(notes))
    except scraper.Limited as e:
        # Hand back whatever was read before the limit hit; the agent should answer
        # with it and must not retry.
        store.save_notes(sid, e.notes)
        store.update_meta(sid, status="limited", count=len(e.notes), error=str(e))
        print(f"LIMITED: {e} 请不要再重试抓取，用已有信息回答，并告诉用户什么时候可以再查。")
        if not e.notes:
            sys.exit(2)
        print(f"SESSION {sid}")
        a.session = sid
        cmd_digest(a)
        return
    except scraper.NotLoggedIn:
        store.update_meta(sid, status="error", error="未登录小红书")
        print(LOGIN_MSG)
        sys.exit(3)
    except Exception as e:
        store.update_meta(sid, status="error", error=str(e))
        print(f"SESSION {sid} ERROR: {e}")
        sys.exit(1)
    print(f"SESSION {sid}")
    a.session = sid
    cmd_digest(a)


def cmd_digest(a):
    meta = store.read_meta(a.session)
    notes = store.read_notes(a.session)
    print(f"# 关键词: {meta['keyword']}  笔记数: {len(notes)}  (session {a.session})\n")
    for i, n in enumerate(notes, 1):
        print(f"## [{i}] {n.get('title')}  | 作者 {n.get('author')} | {n.get('time', '')} {n.get('ip_location') or ''}")
        print(f"赞 {n.get('liked', 0)} 藏 {n.get('collected', 0)} 评 {n.get('comment_count', 0)} | {n.get('url')}")
        if n.get("tags"):
            print("标签: " + " ".join(n["tags"]))
        if n.get("desc"):
            print(n["desc"][: a.max_chars])
        for c in (n.get("comments") or [])[: a.max_comments]:
            print(f"  - 评论({c.get('likes', 0)}赞) {c.get('user')}: {c.get('content')}")
        print()


def main(argv=None):
    from .paths import use_system_certificates
    use_system_certificates()
    ap = argparse.ArgumentParser(prog="xhs_reader")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    sub.add_parser("status")
    s = sub.add_parser("search")
    s.add_argument("keyword")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.add_argument("-c", "--comments", type=int, default=20, help="max comments per note")
    s.add_argument("-q", "--question", help="the research question, stored with the session")
    s.add_argument("--session", help="reuse an existing (empty) session id")
    s.add_argument("--no-detail", action="store_true", help="only collect search cards")
    s.add_argument("--headed", action="store_true", help="show the browser window")
    r = sub.add_parser("research", help="search then print digest (for the chat agent)")
    r.add_argument("keyword")
    r.add_argument("-n", "--limit", type=int, default=8)
    r.add_argument("-c", "--comments", type=int, default=15)
    r.add_argument("-q", "--question")
    r.add_argument("--max-chars", type=int, default=1200)
    r.add_argument("--max-comments", type=int, default=8)
    d = sub.add_parser("digest")
    d.add_argument("session")
    d.add_argument("--max-chars", type=int, default=1500)
    d.add_argument("--max-comments", type=int, default=10)
    sub.add_parser("list")
    sub.add_parser("limits", help="show cooldown and remaining budget")
    sub.add_parser("selftest", help="check HTTPS and that the bundled driver can start the local browser")
    sub.add_parser("mcp", help="run the scraper as an MCP server on stdio (used by the Codex backend)")
    a = ap.parse_args(argv)

    if a.cmd == "login":
        me = scraper.login()
        print(f"已登录：{me.get('nickname')}")
    elif a.cmd == "status":
        print(json.dumps(scraper.status(), ensure_ascii=False))
    elif a.cmd == "search":
        cmd_search(a)
    elif a.cmd == "research":
        cmd_research(a)
    elif a.cmd == "digest":
        cmd_digest(a)
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
