# 小红书调研助手 (xhs_reader)

> If your system prompt says you are 「小红书调研助手」 (the GUI chat agent), ignore this file
> and follow your system prompt.

Main product: a local chat GUI (`./start.sh` → http://localhost:8766). Two answer backends,
chosen in the GUI settings (data/settings.json) and fixed per chat:
- "claude": `claude -p` headless (xhs_reader/agent.py) with NO built-in tools (`--tools ""`; Read
  can't be confined to a folder) and only our MCP server (`--mcp-config`, `mcp__xhs`).
  XHS_AGENT_MODEL=sonnet for faster turns.
- "codex": `codex exec --json` (xhs_reader/codex_backend.py). Codex can't be limited to
  specific commands, so it gets no shell (--disable shell_tool etc., --ignore-user-config,
  read-only sandbox) and reaches the scraper only via our MCP server `xhs-cli mcp`
  (xhs_reader/mcp_server.py, tools auto-approved with default_tools_approval_mode="approve").
  Follow-ups: `codex exec resume <codex_thread>`.
- "api": any OpenAI-compatible API with the user's key (xhs_reader/llm.py), function tools
  wrapping the same CLI; per-turn search/note caps are enforced in code.
All backends share xhs_reader/agent_prompt.md ({TOOLS}/{DATE}) and xhs_reader/tools.py (TOOLS,
TOOLS_TEXT, ToolRunner with the per-turn budgets): search_xiaohongshu (`xhs find`: results list only,
no note opened, no page-view budget) → open_notes (`xhs open <sid> N…`: opens chosen notes, ≤6 per
call / 24 per turn; notes opened in the last 7 days come from the local cache for free) →
view_note_images; plus read_previous_notes. Settings → 联网搜索 (settings web_search, off by default)
adds Claude's WebSearch tool (never WebFetch) / Codex `web_search="live"` (otherwise "disabled": Codex
searches by default) and agent.WEB_TEXT to the prompt; the API backend has no web search. Image budgets are keyed by a turn id (XHS_TURN_ID). Chats live in
data/chats/*.json (+ data/chats/<id>/ images), scraped notes in data/research/<id>/.
Scraper pacing/budgets/cooldown live in scraper.py (`./xhs limits`); they're fixed constants on purpose
(no env overrides — the usage notice tells users not to get around them). XHS_DATA_DIR is for tests/dev.
No anti-detection, deliberately: don't hide automation (AutomationControlled / --enable-automation)
or spoof the user agent. Publicly distributing a tool that evades a site's protections is what
could make it a "专门工具" under 刑法285条第三款; v0.1.11 removed it after testing it isn't needed.

Entry point: `python -m xhs_reader` (xhs_reader/app.py) picks a free port (8765 belongs to
another app on the maintainer's Mac), reuses a running instance, opens the browser.
Packaging: packaging/*.spec (PyInstaller, two exes: windowed GUI + console `xhs-cli`),
packaging/macos_sign_notarize.sh, packaging/windows_installer.iss, .github/workflows/release.yml.
Paths/child processes go through xhs_reader/paths.py and procutil.py (cross-platform; never
os.kill(pid, 0) — it kills on Windows). Packaged data dir: ~/.xhs-research-agent.
Apps launched from Finder/Explorer get a bare environment: paths.child_env() passes the OS
system proxy as HTTP(S)_PROXY (Claude Code ignores the system proxy; without it Anthropic
answers 403 "Request not allowed" where it needs a proxy), and settings.find_claude() picks the
newest of several Claude Code installs (e.g. old ~/.local/bin vs Homebrew).
Updates: xhs_reader/updater.py checks GitHub releases/latest (XHS_UPDATE_API overrides, for tests),
verifies (macOS: TeamIdentifier 3QCL9WNFBB; SHA256SUMS.txt from the release) and hands off to a
detached helper that swaps the app after it quits. Release assets must keep their names
(`*-macos-arm64.dmg`, `*-windows-x64-setup.exe`, `SHA256SUMS.txt`) or old versions can't update.

## When the user asks *this* Claude Code session to research

1. Pick 1–3 good search keywords for the question (Chinese, the way users on XHS phrase things).
2. For each keyword:
   `./xhs find "<关键词>" -q "<用户的问题>"` prints `SESSION <id>` and the results list
   (no note opened). Then `./xhs open <id> 2 5 7` opens the 3–5 most relevant ones
   (headless, deliberately slow, ~10 s per note).
   Keep volume low (≤ ~24 notes per request): it's the user's real account, and viewing too
   many notes in a short time gets it rate-limited (300013 "访问频繁"). Budgets: 60 per rolling hour, 300 per rolling 24 h
   (the one observed block was ~100 views in 40 min with 2–5 s gaps, i.e. ~150/h).
   If it exits with `LIMITED:`, stop and tell the user — never retry around a cooldown.
3. `./xhs digest <id>` re-prints a session (list + opened notes).
4. Write the report in Chinese (optionally save as `data/research/<id>/report.md`).
   Structure: 核心结论 → 分主题要点（带具体推荐/数据）→ 争议与避坑（comments are often
   more honest than posts; flag 广告/营销号 suspicion）→ 参考笔记（markdown links with 赞/藏 counts）.
   With multiple keywords, write the full report into the first session and a one-line
   pointer to it in the others.
5. Reply in chat with a concise summary.

- Login: `./xhs login` opens Chrome for a QR scan
  (the user scans it; never ask for their password). `status` checks login.
- Only one browser process at a time (data/browser.lock holds the pid; others wait).
- The site changes often: search results come from the XHR `.../search/notes`, comments
  from `/api/sns/web/v2/comment/page`, note detail from `window.__INITIAL_STATE__`.
  Debug with a headless screenshot when something returns 0 results.
