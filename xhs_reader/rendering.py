"""Render model Markdown into a deliberately small, inert HTML vocabulary."""
from functools import lru_cache
from html import escape
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

import markdown

_TAGS = set("p br hr strong em del blockquote pre code ul ol li h1 h2 h3 h4 h5 h6 table thead tbody tr th td a".split())
_VOID = {"br", "hr"}
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


class _SafeHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []

    def handle_starttag(self, tag, attrs):
        if tag not in _TAGS:
            return
        attributes = ""
        if tag == "a":
            href = dict(attrs).get("href") or ""
            # Decode entities first (HTMLParser does this), then reject all controls.
            if not re.search(r"[\x00-\x20\x7f]", href):
                try:
                    parsed = urlsplit(href)
                    if parsed.scheme.lower() in ("https", "http") and parsed.hostname:
                        attributes = f' href="{escape(href, quote=True)}" target="_blank" rel="noopener noreferrer"'
                except ValueError:
                    pass
        self.out.append(f"<{tag}{attributes}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _TAGS and tag not in _VOID:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        self.out.append(escape(data))


@lru_cache(maxsize=256)
def render_markdown(text):
    lines, out = text.split("\n"), []
    for line in lines:
        if _LIST_ITEM.match(line) and out and out[-1].strip() and not _LIST_ITEM.match(out[-1]) \
                and not out[-1].startswith(("  ", "\t", "|")):
            out.append("")
        out.append(line)
    parser = _SafeHTML()
    parser.feed(markdown.markdown("\n".join(out), extensions=["tables", "fenced_code", "sane_lists"]))
    parser.close()
    return "".join(parser.out)
