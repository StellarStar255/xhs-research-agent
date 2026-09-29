"""Export a chat (or one question and answer) for sharing: a standalone HTML file, or a
long PNG rendered by the local Chrome (the same browser the scraper uses, but a fresh
headless instance without the logged-in profile — it never visits Xiaohongshu)."""
import base64
import html
import re
import time

from . import agent, store

CSS = """
* { box-sizing: border-box; }
body { margin: 0; background: #f7f6f4; color: #1d1d1f; font: 15px/1.75 -apple-system, "PingFang SC", "Microsoft YaHei", "Helvetica Neue", sans-serif; }
.page { max-width: 720px; margin: 0 auto; padding: 28px 22px 24px; }
header { border-bottom: 1px solid #e5e3df; padding-bottom: 14px; margin-bottom: 8px; }
.brand { color: #7a7a80; font-size: 12px; }
h1 { font-size: 21px; margin: 4px 0 2px; line-height: 1.4; }
.meta { color: #7a7a80; font-size: 12px; }
.q { display: flex; flex-direction: column; align-items: flex-end; margin: 22px 0 10px; gap: 6px; }
.q .bubble { background: #ff2442; color: #fff; border-radius: 16px 16px 4px 16px; padding: 8px 14px; max-width: 85%; white-space: pre-wrap; }
.q img { max-width: 240px; max-height: 200px; border-radius: 12px; }
.a { background: #fff; border: 1px solid #e5e3df; border-radius: 14px; padding: 14px 18px; }
.steps { color: #7a7a80; font-size: 12.5px; margin-bottom: 8px; }
.md { overflow-wrap: anywhere; }
.md > :first-child { margin-top: 0; } .md > :last-child { margin-bottom: 0; }
.md h1, .md h2, .md h3 { font-size: 16px; margin: 16px 0 6px; }
.md a { color: #ff2442; text-decoration: none; }
.md table { border-collapse: collapse; font-size: 13.5px; margin: 10px 0; }
.md td, .md th { border: 1px solid #e5e3df; padding: 5px 9px; text-align: left; vertical-align: top; }
.md blockquote { margin: 8px 0; padding: 2px 12px; border-left: 3px solid #e5e3df; color: #7a7a80; }
.md ul, .md ol { padding-left: 22px; }
.md hr { border: 0; border-top: 1px solid #e5e3df; margin: 16px 0; }
.src { margin-top: 22px; background: #fff; border: 1px solid #e5e3df; border-radius: 14px; padding: 12px 18px; }
.src h3 { font-size: 15px; margin: 0 0 4px; }
.src p { color: #7a7a80; font-size: 12.5px; margin: 0 0 6px; }
.src ol { margin: 0; padding-left: 22px; font-size: 14px; }
.src a { color: #ff2442; text-decoration: none; }
.src .u { color: #7a7a80; font-size: 12px; word-break: break-all; }
footer { color: #7a7a80; font-size: 12px; text-align: center; margin-top: 26px; line-height: 1.6; }
@media print {
  body { background: #fff; }
  .page { max-width: none; padding: 0; }
  .a { border-color: #ddd; }
  .q, .md tr, .md li, .md blockquote, .md img, .src li { break-inside: avoid; }
  .md h1, .md h2, .md h3 { break-after: avoid; }
}
"""

MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
HIDDEN = "某用户"


def _pairs(chat, index):
    """[(user message, assistant message)] for the export: one Q&A or the whole chat."""
    msgs = chat["messages"]
    if index is not None:
        if not (0 < index < len(msgs)) or msgs[index]["role"] != "assistant":
            raise ValueError("找不到这条回答")
        return [(msgs[index - 1], msgs[index])]
    return [(msgs[i - 1], msgs[i]) for i in range(1, len(msgs))
            if msgs[i]["role"] == "assistant" and msgs[i].get("status") != "running"]


# Nicknames that are also everyday words here; never replace them.
_COMMON = {"小红书", "薯队长", "用户", "作者", "博主", "官方", "客服", "网友", "楼主"}
_URL = re.compile(r"https?://[^\s)\]>\"']+")


def commenter_names(pairs):
    """Nicknames of the commenters in the notes these answers read, longest first.
    Keeps note authors (they're cited with their notes) and skips names that could be
    ordinary text: short or all-digit names, common words, and anything that also appears
    in the question or in the notes' titles and text."""
    sessions = {p["session"] for _, a in pairs for p in a.get("parts", []) if p.get("session")}
    names, authors, prose = set(), set(), [q.get("text", "") for q, _ in pairs]
    for sid in sessions:
        try:
            notes = store.read_notes(sid)
            prose.append(store.read_meta(sid).get("keyword", ""))
        except (OSError, ValueError):
            continue
        for n in notes:
            authors.add(n.get("author"))
            prose.append(f"{n.get('title', '')} {n.get('desc', '')}")
            for c in n.get("comments") or []:
                names.add(c.get("user"))
                names.update(r.get("user") for r in c.get("replies") or [])
    text = "\n".join(prose)
    keep = [n.strip() for n in names if n and n not in authors]
    return sorted({n for n in keep if len(n) >= 3 and not n.isdigit() and n not in _COMMON and n not in text},
                  key=len, reverse=True)


def _hide(text, names):
    """Replace nicknames, but never inside links (a note's URL must keep working)."""
    if not names:
        return text
    out, last = [], 0
    for m in _URL.finditer(text):
        out.append(_replace_names(text[last:m.start()], names))
        out.append(m.group(0))
        last = m.end()
    out.append(_replace_names(text[last:], names))
    return "".join(out)


def _replace_names(text, names):
    # Only where the name is used as a name — quoted, @-mentioned or followed by what they
    # said — so a nickname that is also an ordinary phrase stays untouched elsewhere.
    for n in names:
        e = re.escape(n)
        text = re.sub(rf"(?<=[「『“\"@＠]){e}|{e}(?=[」』”\"]|\s*(?:说|：|:|表示|提到|觉得|认为|评论|回复|指出|补充|吐槽|（\d))",
                      HIDDEN, text)
    return text


_NOTE_LINK = re.compile(r"\[([^\]]+)\]\((https?://(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/([0-9a-zA-Z]+)[^)\s]*)\)")


def _sources(texts):
    """The Xiaohongshu notes the answers link to, in order, once each: [(title, url, id)]."""
    seen, out = set(), []
    for t in texts:
        for title, url, nid in _NOTE_LINK.findall(t):
            if nid not in seen:
                seen.add(nid)
                out.append((title, url, nid))
    return out


def _steps(msg):
    out = []
    for p in msg.get("parts", []):
        if p["type"] == "search" and p.get("keyword"):
            out.append(f"🔍 在小红书搜索「{p['keyword']}」" + (f"，找到 {p['count']} 篇" if p.get("count") else ""))
        elif p["type"] == "open" and p.get("status") == "done":
            out.append(f"📖 读了 {p.get('count') or len(p.get('notes') or [])} 篇笔记")
        elif p["type"] == "images" and p.get("status") == "done":
            out.append(f"🖼 看了 {p.get('count', 0)} 张笔记图片")
        elif p["type"] == "web" and p.get("query"):
            q = p["query"]
            out.append(f"🌐 打开网页 {re.sub(r'^https?://(www[.])?', '', q)[:60]}" if re.match(r"https?://", q)
                       else f"🌐 网页搜索「{q}」")
    return " · ".join(out)


def build_html(cid, index=None, hide_names=False):
    """(title, html) for the chat, or for the Q&A whose answer is messages[index]."""
    from .server import _md
    chat = agent.load(cid)
    pairs = _pairs(chat, index)
    if not pairs:
        raise ValueError("这个对话还没有可以导出的回答")
    names = commenter_names(pairs) if hide_names else []
    title = pairs[0][0].get("text", "").strip().splitlines()[0][:60] if index is not None and pairs[0][0].get("text") \
        else chat.get("title", "种草调研")
    blocks, answers = [], []
    for q, a in pairs:
        imgs = ""
        for name in q.get("images") or []:
            f = agent.image_dir(cid) / name
            if f.is_file():
                mime = MIME.get(f.suffix.lstrip(".").lower(), "image/png")
                imgs += f'<img src="data:{mime};base64,{base64.b64encode(f.read_bytes()).decode()}">'
        text = "\n\n".join(p["text"] for p in a.get("parts", []) if p["type"] == "text")
        answers.append(text)
        steps = _steps(a)
        blocks.append(
            f'<div class="q">{imgs}' + (f'<div class="bubble">{html.escape(q.get("text", ""))}</div>' if q.get("text") else "")
            + '</div><div class="a">' + (f'<div class="steps">{html.escape(steps)}</div>' if steps else "")
            + f'<div class="md">{_md(_hide(text, names))}</div></div>')
    date = time.strftime("%Y-%m-%d")
    sources = _sources(answers)
    src = "" if not sources else (
        '<section class="src"><h3>原笔记</h3><p>以上内容整理自下面这些小红书笔记，完整内容和评论请看原文。</p><ol>'
        + "".join(f'<li><a href="{html.escape(u)}">{html.escape(_hide(t, names))}</a><br>'
                  f'<span class="u">xiaohongshu.com/explore/{html.escape(i)}</span></li>' for t, u, i in sources)
        + "</ol></section>")
    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><div class="page"><header><div class="brand">调研笔记</div><h1>{html.escape(title)}</h1>
<div class="meta">{chat.get('created', date)}</div></header>
{''.join(blocks)}
{src}
<footer>内容整理自小红书用户公开的笔记和评论，仅供参考，以原笔记为准。<br>
由开源工具 xhs-research-agent 生成，与小红书官方无关 · {date}</footer>
</div></body></html>"""
    return title, re.sub(r'<a href="(https?://[^"]+)"', r'<a href="\1" target="_blank" rel="noopener"', page)


def _browser(p):
    from playwright.sync_api import Error as PlaywrightError
    for channel in ("chrome", "msedge"):
        try:
            return p.chromium.launch(channel=channel, headless=True)
        except PlaywrightError:
            continue
    raise RuntimeError("没有找到 Google Chrome，无法生成图片或 PDF；可以改用「网页文件」导出")


def render_pdf(page_html):
    """A4 pages with selectable text and clickable links (Chrome's print to PDF)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page()
            page.set_content(page_html, wait_until="load")
            return page.pdf(format="A4", print_background=True,
                            margin={"top": "16mm", "bottom": "16mm", "left": "14mm", "right": "14mm"})
        finally:
            browser.close()


def render_png(page_html):
    """A long screenshot of the page at phone-friendly width."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(viewport={"width": 720, "height": 800})
            page.set_content(page_html, wait_until="load")
            height = page.evaluate("document.documentElement.scrollHeight")
            scale = max(1, min(2, 30000 / max(height, 1)))  # keep very long chats within image size limits
            page = browser.new_page(viewport={"width": 720, "height": 800}, device_scale_factor=scale)
            page.set_content(page_html, wait_until="load")
            return page.screenshot(full_page=True, type="png")
        finally:
            browser.close()
