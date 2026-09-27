"""The agent's tools, shared by every backend (MCP server for Claude/Codex, and llm.py).

Two-phase browsing, like a person: search_xiaohongshu returns the results list without
opening any note (no page-view budget), then open_notes opens the few worth reading.
Per-turn budgets are enforced here, not left to the prompt.
"""
import re
import subprocess

from . import paths, procutil

MAX_SEARCHES = 3         # search_xiaohongshu calls per turn
MAX_OPEN = 24            # notes opened per turn
MAX_OPEN_PER_CALL = 6

TOOLS = [
    {"type": "function", "function": {
        "name": "search_xiaohongshu",
        "description": "在小红书搜索一个关键词，返回前约 20 篇笔记的列表（标题、作者、类型、赞/藏/评数）。"
                       "只看列表，不打开笔记，不占抓取额度。之后用 open_notes 挑值得读的打开。",
        "parameters": {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "搜索关键词，用小红书用户会用的口语化中文"},
        }, "required": ["keyword"]}}},
    {"type": "function", "function": {
        "name": "open_notes",
        "description": "打开搜索列表里挑中的笔记，读正文和热门评论（慢，每篇约 10 秒，每篇占 1 个抓取额度；"
                       "最近几天打开过的直接用缓存、不占额度）。",
        "parameters": {"type": "object", "properties": {
            "session": {"type": "string", "description": "搜索结果里给出的 SESSION id"},
            "notes": {"type": "array", "items": {"type": "integer"},
                      "description": f"要打开的笔记编号（列表里 [3] 这样的数字），每次最多 {MAX_OPEN_PER_CALL} 篇"},
        }, "required": ["session", "notes"]}}},
    {"type": "function", "function": {
        "name": "read_previous_notes",
        "description": "重新读取之前某次搜索的结果（列表和已打开的笔记），不访问小红书，很快。",
        "parameters": {"type": "object", "properties": {
            "session": {"type": "string", "description": "之前搜索结果里给出的 SESSION id"},
        }, "required": ["session"]}}},
    {"type": "function", "function": {
        "name": "view_note_images",
        "description": "查看某篇已打开笔记的图片（很多笔记的内容写在图片里，正文很短）。不打开笔记页面，不占抓取额度。"
                       "每次最多 4 张，每轮对话最多 12 张。",
        "parameters": {"type": "object", "properties": {
            "session": {"type": "string", "description": "搜索结果里给出的 SESSION id"},
            "note": {"type": "integer", "description": "笔记编号，也就是列表里 [3] 这样的数字"},
            "limit": {"type": "integer", "description": "要看几张，默认 4，最多 4"},
        }, "required": ["session", "note"]}}},
]

TOOLS_TEXT = f"""你有四个工具，像人刷小红书一样「先看列表，再挑着点开」：

- `search_xiaohongshu(keyword)`：搜索，返回约 20 篇笔记的列表（标题、作者、赞藏评）。不打开笔记，不占额度。
  每轮对话最多搜 {MAX_SEARCHES} 次。
- `open_notes(session, notes)`：从列表里挑与问题最相关、互动多的笔记打开，读正文和评论。每篇占 1 个额度，
  所以**一般挑 3～5 篇**，标题明显跑题、赞藏很少的不要打开。每次最多 {MAX_OPEN_PER_CALL} 篇，每轮合计最多 {MAX_OPEN} 篇。
- `read_previous_notes(session)`：重新读取之前某次搜索的结果，不访问小红书。
- `view_note_images(session, note, limit)`：查看已打开笔记的图片。结果里标着「正文很短，内容可能在图片里」、
  而且对回答很重要的笔记，就用它看图（比如价格表、清单、测评对比图）。每次最多 4 张、每轮最多 12 张。
  图片链接过一段时间会失效，所以要在打开后的同一轮里看。

工具返回里的笔记链接可以直接用于引用。"""


class ToolRunner:
    """Runs tool calls for one research turn, keeping that turn's budgets."""

    def __init__(self, turn_id="", on_proc=None):
        self.turn_id = turn_id
        self.on_proc = on_proc or (lambda p: None)  # lets a caller stop a running scrape
        self.searches = 0
        self.opened = 0

    def _cli(self, *args):
        p = procutil.popen(paths.cli_command(*args), cwd=paths.DATA_DIR,
                           env=paths.child_env({"XHS_TURN_ID": self.turn_id}),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.on_proc(p)
        try:
            out, _ = p.communicate(timeout=900)
        except subprocess.TimeoutExpired:
            procutil.kill_tree(p)
            return "ERROR: 抓取超时", True
        finally:
            self.on_proc(None)
        return out[-60000:], p.returncode != 0

    def call(self, name, args, question=""):
        """-> {"text": str, "is_error": bool, "images": [Path]}"""
        name = name.rsplit("__", 1)[-1]
        if name == "search_xiaohongshu":
            keyword = str(args.get("keyword", "")).strip()
            if self.searches >= MAX_SEARCHES:
                return self._result(f"本轮已经搜索了 {self.searches} 次，达到上限，不能再搜。请用已有信息回答。")
            if not keyword:
                return self._result("缺少 keyword 参数。", True)
            self.searches += 1
            out, failed = self._cli("find", keyword, "-q", question or keyword)
            return self._result(out, failed and "LIMITED" not in out and "NOT_LOGGED_IN" not in out)
        if name == "open_notes":
            session = str(args.get("session", "")).strip()
            try:
                wanted = [int(x) for x in (args.get("notes") or [])]
            except (TypeError, ValueError):
                return self._result("notes 参数要是笔记编号的列表，比如 [1, 3, 5]。", True)
            room = MAX_OPEN - self.opened
            if room <= 0:
                return self._result(f"本轮已经打开了 {self.opened} 篇笔记，达到上限。请用已有信息回答。")
            if not session or not wanted:
                return self._result("缺少 session 或 notes 参数。", True)
            wanted = wanted[:min(MAX_OPEN_PER_CALL, room)]
            out, failed = self._cli("open", session, *map(str, wanted), "--max", str(MAX_OPEN_PER_CALL))
            m = re.search(r"已打开 (\d+) 篇", out)
            self.opened += int(m.group(1)) if m else 0
            return self._result(out, failed and "LIMITED" not in out and "NOT_LOGGED_IN" not in out)
        if name == "read_previous_notes":
            session = str(args.get("session", "")).strip()
            if not session:
                return self._result("缺少 session 参数。", True)
            return self._result(*self._cli("digest", session))
        if name == "view_note_images":
            from . import images
            try:
                title, found = images.note_images(str(args.get("session", "")).strip(), int(args.get("note") or 0),
                                                  args.get("limit") or images.MAX_PER_NOTE, turn_id=self.turn_id)
            except (images.ImageError, ValueError, TypeError) as e:
                return self._result(str(e))
            return {"text": f"「{title}」的 {len(found)} 张图片：", "is_error": False, "images": found}
        return self._result(f"没有叫 {name} 的工具。", True)

    @staticmethod
    def _result(text, is_error=False):
        return {"text": text, "is_error": bool(is_error), "images": []}
