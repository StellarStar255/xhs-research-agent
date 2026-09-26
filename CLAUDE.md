# 小红书调研助手 (xhs_reader)

> If your system prompt says you are 「小红书调研助手」 (the GUI chat agent), ignore this file
> and follow your system prompt.

Main product: a local chat GUI (`./start.sh` → http://localhost:8766). Two answer backends,
chosen in the GUI settings (data/settings.json) and fixed per chat:
- "claude": `claude -p` headless (xhs_reader/agent.py), may only run `./xhs research|digest`.
  XHS_AGENT_MODEL=sonnet for faster turns.
- "codex": `codex exec --json` (xhs_reader/codex_backend.py). Codex can't be limited to
  specific commands, so it gets no shell (--disable shell_tool etc., --ignore-user-config,
  read-only sandbox) and reaches the scraper only via our MCP server `xhs-cli mcp`
  (xhs_reader/mcp_server.py, tools auto-approved with default_tools_approval_mode="approve").
  Follow-ups: `codex exec resume <codex_thread>`.
- "api": any OpenAI-compatible API with the user's key (xhs_reader/llm.py), function tools
  wrapping the same CLI; per-turn search/note caps are enforced in code.
Both share xhs_reader/agent_prompt.md ({TOOLS}/{DATE} placeholders). Chats live in
data/chats/*.json (+ data/chats/<id>/ images), scraped notes in data/research/<id>/.
Scraper pacing/budgets/cooldown live in scraper.py (`./xhs limits`).

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
   `./xhs search "<关键词>" -n 8 -c 20 -q "<用户的问题>"`
   It prints `SESSION <id>`. Runs headless and deliberately slow (~2–3 min for 8 notes).
   Keep volume low (≤ ~24 notes per request): it's the user's real account, and viewing too
   many notes in a short time gets it rate-limited (300013 "访问频繁"). Budgets: 40/h, 150/day.
   If it exits with `LIMITED:`, stop and tell the user — never retry around a cooldown.
3. `./xhs digest <id>` → read the notes & comments.
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
