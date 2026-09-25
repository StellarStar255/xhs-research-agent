"""Which model backend answers chats, stored in data/settings.json (never committed).

backend "claude": the local Claude Code CLI (`claude -p`), needs a Claude subscription.
backend "api":    any OpenAI-compatible chat API (DeepSeek, 通义千问, Kimi, 智谱, OpenAI…)
                  with the user's own API key.
"""
import glob
import json
import os
import shutil
from pathlib import Path

from .scraper import DATA_DIR

SETTINGS_FILE = DATA_DIR / "settings.json"

# Model names are only defaults; providers rename models often, so the GUI can
# fetch the live list from <base_url>/models.
PROVIDERS = [
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com",
     "model": "deepseek-chat", "key_url": "https://platform.deepseek.com/api_keys",
     "note": "便宜，中文好；目前的对话模型不支持图片"},
    {"id": "qwen", "name": "通义千问（阿里云百炼）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "model": "qwen-plus", "key_url": "https://bailian.console.aliyun.com/",
     "note": "要分析图片请选带 vl 的模型，例如 qwen-vl-max"},
    {"id": "kimi", "name": "Kimi（月之暗面）", "base_url": "https://api.moonshot.cn/v1",
     "model": "moonshot-v1-32k", "key_url": "https://platform.moonshot.cn/console/api-keys", "note": ""},
    {"id": "glm", "name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "model": "glm-4-plus", "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
     "note": "要分析图片请选带 v 的模型，例如 glm-4v-plus"},
    {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1",
     "model": "gpt-4o", "key_url": "https://platform.openai.com/api-keys", "note": ""},
    {"id": "custom", "name": "自定义（OpenAI 兼容接口）", "base_url": "", "model": "", "key_url": "", "note": ""},
]


# Where Claude Code's `claude` usually lives, for when the server was started
# without it on PATH (e.g. not from a login shell).
_CLAUDE_CANDIDATES = [
    "~/.local/bin/claude", "~/.claude/local/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude",
    "~/.npm-global/bin/claude", "~/.bun/bin/claude", "~/.volta/bin/claude", "~/.yarn/bin/claude",
    "~/.nvm/versions/node/*/bin/claude", "~/Library/pnpm/claude", "~/.local/share/pnpm/claude",
    # Windows (native installer, npm)
    "~/.local/bin/claude.exe", "~/AppData/Roaming/npm/claude.cmd", "~/AppData/Local/Programs/claude/claude.exe",
]


def find_claude():
    """Path to this machine's Claude Code CLI, or None."""
    if os.environ.get("XHS_CLAUDE_PATH"):
        return os.environ["XHS_CLAUDE_PATH"]
    found = shutil.which("claude")
    if found:
        return found
    for pattern in _CLAUDE_CANDIDATES:
        for f in sorted(glob.glob(os.path.expanduser(pattern)), reverse=True):  # newest nvm version first
            if os.access(f, os.X_OK) and Path(f).is_file():
                return f
    return None


def claude_available():
    return find_claude() is not None


def load():
    try:
        s = json.loads(SETTINGS_FILE.read_text())
    except (FileNotFoundError, ValueError):
        s = {}
    s.setdefault("backend", "claude" if claude_available() else "api")
    api = s.setdefault("api", {})
    if not api.get("provider"):
        api.update(provider="deepseek", base_url=PROVIDERS[0]["base_url"], model=PROVIDERS[0]["model"])
    api.setdefault("api_key", "")
    return s


def save(s):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2))
    try:
        SETTINGS_FILE.chmod(0o600)  # holds an API key (no-op beyond read-only on Windows)
    except OSError:
        pass


def public(s=None):
    """Settings safe to send to the browser: the API key is masked."""
    s = s or load()
    api = dict(s["api"])
    key = api.pop("api_key", "")
    api["has_key"] = bool(key)
    api["key_hint"] = f"{key[:3]}…{key[-4:]}" if len(key) > 10 else ("已填写" if key else "")
    return {"backend": s["backend"], "api": api, "claude_available": claude_available(), "providers": PROVIDERS}


def api_ready(s=None):
    api = (s or load())["api"]
    return bool(api.get("base_url") and api.get("model") and api.get("api_key"))
