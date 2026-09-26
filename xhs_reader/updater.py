"""One-click updates from GitHub Releases (packaged app only).

check()  -> is a newer non-draft, non-prerelease release out?
start()  -> download the installer for this OS, verify it, then hand over to a small
            detached helper script that waits for this app to quit, installs the new
            version in place and relaunches it. The caller then shuts the app down.

Verification before anything is installed:
  macOS    the new .app must be signed by our Developer ID team (TEAM_ID); the DMG must
           also match SHA256SUMS.txt when the release has one.
  Windows  the installer must match SHA256SUMS.txt from the same release (the installer
           itself isn't code-signed), otherwise we refuse.
If an in-place install isn't possible (running from the DMG, App Translocation, no write
access), we fall back to opening the downloaded installer for a manual install.
"""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from . import __version__, paths, procutil

REPO = "StellarStar255/xhs-research-agent"
TEAM_ID = "3QCL9WNFBB"
API = os.environ.get("XHS_UPDATE_API", f"https://api.github.com/repos/{REPO}/releases/latest")
APP_NAME = "XHS Research Agent"
MAC, WINDOWS = sys.platform == "darwin", sys.platform == "win32"
CHECK_EVERY = 6 * 3600

_lock = threading.Lock()
_state = {"current": __version__, "latest": None, "available": False, "notes": "", "url": None,
          "checked_at": 0, "phase": "idle", "progress": 0, "error": None}
# phase: idle | checking | downloading | verifying | installing | manual | error


def supported():
    return paths.FROZEN and (MAC or WINDOWS)


def status():
    with _lock:
        return {**_state, "supported": supported()}


def _set(**kw):
    with _lock:
        _state.update(kw)


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": f"{APP_NAME}/{__version__}"})
    return urllib.request.urlopen(req, timeout=timeout)


def _asset_pattern():
    if MAC:
        return re.compile(r"-macos-arm64\.dmg$")
    return re.compile(r"-windows-x64-setup\.exe$")


def check():
    """Ask GitHub for the latest release. Never raises; errors land in status()."""
    _set(phase="checking", error=None)
    try:
        with _get(API) as r:
            rel = json.load(r)
        latest = rel["tag_name"].lstrip("v")
        assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
        pattern = _asset_pattern()
        installer = next((u for n, u in assets.items() if pattern.search(n)), None)
        _set(latest=latest, notes=rel.get("body") or "", url=rel.get("html_url"),
             available=_vtuple(latest) > _vtuple(__version__) and installer is not None,
             installer=installer, sums=assets.get("SHA256SUMS.txt"),
             installer_name=next((n for n in assets if pattern.search(n)), None),
             checked_at=time.time(), phase="idle")
    except Exception as e:
        _set(phase="idle", error=f"检查更新失败：{e}", checked_at=time.time())
    return status()


def check_periodically():
    """Background loop for the packaged app: check shortly after launch, then every few hours."""
    if not supported():
        return

    def loop():
        time.sleep(15)
        while True:
            if _state["phase"] in ("idle", "error"):
                check()
            time.sleep(CHECK_EVERY)
    threading.Thread(target=loop, daemon=True).start()


# ---------- download & verify ----------

def _download(url, dest):
    with _get(url, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as f:
            while chunk := r.read(1 << 16):
                f.write(chunk)
                done += len(chunk)
                if total:
                    _set(progress=round(done * 100 / total))


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _expected_sha(sums_url, name):
    if not sums_url:
        return None
    with _get(sums_url) as r:
        for line in r.read().decode().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == name:
                return parts[0].lower()
    return None


def _verify_mac_app(app):
    """The app must be validly signed by our team."""
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True,
                   capture_output=True)
    info = subprocess.run(["codesign", "-dv", str(app)], capture_output=True, text=True).stderr
    if f"TeamIdentifier={TEAM_ID}" not in info:
        raise RuntimeError("新版本的签名不是本项目的开发者，已拒绝安装")


# ---------- install ----------

def _current_app_bundle():
    """/…/XHS Research Agent.app for the running app (macOS)."""
    exe = Path(sys.executable).resolve()
    for p in exe.parents:
        if p.suffix == ".app":
            return p
    return None


def _can_replace_in_place(bundle):
    s = str(bundle)
    return bundle and "/AppTranslocation/" not in s and not s.startswith("/Volumes/") \
        and os.access(bundle.parent, os.W_OK)


def _mac_install(dmg, work):
    bundle = _current_app_bundle()
    if not _can_replace_in_place(bundle):
        subprocess.Popen(["open", str(dmg)])  # let the user drag it to Applications
        _set(phase="manual", error=None)
        return False
    # Check the new app before we hand over: mount, verify signature, unmount.
    mnt = Path(tempfile.mkdtemp(prefix="xhs-mnt-"))
    subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mnt), str(dmg)],
                   check=True, capture_output=True)
    try:
        _verify_mac_app(mnt / f"{APP_NAME}.app")
    finally:
        subprocess.run(["hdiutil", "detach", "-quiet", str(mnt)], capture_output=True)
    q = shlex.quote
    script = work / "install.sh"
    script.write_text(f"""#!/bin/bash
# Installed by 小红书调研助手's updater: wait for the app to quit, swap in the new version, relaunch.
exec >>{q(str(paths.DATA_DIR / "update.log"))} 2>&1
set -u
APP={q(str(bundle))}; DMG={q(str(dmg))}; PID={os.getpid()}
echo "== $(date) updating $APP"
for i in $(seq 1 120); do kill -0 $PID 2>/dev/null || break; sleep 0.5; done
MNT=$(mktemp -d)
hdiutil attach -nobrowse -readonly -mountpoint "$MNT" "$DMG" || {{ open "$APP"; exit 1; }}
rm -rf "$APP.new"
ditto "$MNT/{APP_NAME}.app" "$APP.new"
hdiutil detach -quiet "$MNT"
if codesign --verify --deep --strict "$APP.new" && codesign -dv "$APP.new" 2>&1 | grep -q "TeamIdentifier={TEAM_ID}"; then
  mv "$APP" "$APP.old" && mv "$APP.new" "$APP" && rm -rf "$APP.old"
  echo "updated"
else
  echo "signature check failed; keeping the current version"; rm -rf "$APP.new"
fi
open "$APP"
""")
    script.chmod(0o755)
    procutil.popen(["/bin/bash", str(script)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, close_fds=True)
    return True


def _windows_install(setup, work):
    exe = Path(sys.executable)
    ps = work / "install.ps1"
    ps.write_text(f"""# Installed by the updater: wait for the app to quit, run the installer silently, relaunch.
Start-Transcript -Append -Path '{paths.DATA_DIR / "update.log"}' | Out-Null
Wait-Process -Id {os.getpid()} -Timeout 60 -ErrorAction SilentlyContinue
Start-Process -Wait -FilePath '{setup}' -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/CLOSEAPPLICATIONS'
Start-Process -FilePath '{exe}'
""", encoding="utf-8-sig")
    subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                      "-File", str(ps)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    return True


def start(on_ready_to_quit):
    """Download + verify + hand over to the installer helper, in the background.
    Calls on_ready_to_quit() once the helper is waiting for us to exit."""
    if not supported():
        raise RuntimeError("只有安装版支持自动更新；源码运行请用 git pull")
    st = status()
    if not st["available"]:
        raise RuntimeError("没有可用的新版本")
    if st["phase"] in ("downloading", "verifying", "installing"):
        return

    def run():
        try:
            work = paths.DATA_DIR / "updates" / st["latest"]
            work.mkdir(parents=True, exist_ok=True)
            dest = work / st["installer_name"]
            _set(phase="downloading", progress=0, error=None)
            _download(st["installer"], dest)
            _set(phase="verifying")
            expected = _expected_sha(st.get("sums"), st["installer_name"])
            if expected and _sha256(dest) != expected:
                raise RuntimeError("下载的文件校验失败（SHA256 不一致），已删除，请稍后重试")
            if WINDOWS and not expected:
                raise RuntimeError("这个版本没有提供校验文件，为了安全不自动安装，请到 Release 页面手动下载")
            _set(phase="installing")
            if (_mac_install if MAC else _windows_install)(dest, work):
                on_ready_to_quit()
        except Exception as e:
            _set(phase="error", error=str(e))
            try:
                if "dest" in locals() and "校验失败" in str(e):
                    dest.unlink(missing_ok=True)
            except OSError:
                pass
    threading.Thread(target=run, daemon=True).start()
