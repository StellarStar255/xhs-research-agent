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
import http.client
import json
import os
import re
import shlex
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from urllib.error import HTTPError, URLError
from pathlib import Path

from . import __version__, paths, procutil
from .persistence import file_lock

REPO = "StellarStar255/xhs-research-agent"
TEAM_ID = "3QCL9WNFBB"
API = os.environ.get("XHS_UPDATE_API", f"https://api.github.com/repos/{REPO}/releases/latest")
APP_NAME = "XHS Research Agent"
MAC, WINDOWS = sys.platform == "darwin", sys.platform == "win32"
CHECK_EVERY = 6 * 3600
BUSY_PHASES = ("checking", "downloading", "verifying", "installing")
DOWNLOAD_ATTEMPTS = 4

_lock = threading.Lock()
_state = {"current": __version__, "latest": None, "available": False, "notes": "", "url": None,
          "checked_at": 0, "phase": "idle", "progress": 0, "error": None}
# phase: idle | checking | downloading | verifying | installing | manual | error


def supported():
    return paths.FROZEN and (MAC or WINDOWS)


def status():
    with _lock:
        return _status_locked()


def _status_locked():
    return {**_state, "supported": supported()}


def _set(**kw):
    with _lock:
        _state.update(kw)


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def _get(url, timeout=20, headers=None):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": f"{APP_NAME}/{__version__}", **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


def _asset_pattern():
    if MAC:
        return re.compile(r"-macos-arm64\.dmg$")
    return re.compile(r"-windows-x64-setup\.exe$")


def _update_directory(version):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("发布版本号无效")
    root = paths.DATA_DIR / "updates"
    work = root / version
    if paths.is_redirected(root) or paths.is_redirected(work):
        raise ValueError("更新目录是符号链接或目录联接，无法安装")
    return work


def check():
    """Ask GitHub for the latest release. Never raises; errors land in status()."""
    with _lock:
        if _state["phase"] in BUSY_PHASES:
            return _status_locked()
        _state.update(phase="checking", error=None)
    try:
        with _get(API) as r:
            rel = json.load(r)
        latest = rel["tag_name"].removeprefix("v")
        work = _update_directory(latest)
        assets = {a["name"]: a for a in rel.get("assets", [])}
        pattern = _asset_pattern()
        name = next((n for n in assets if re.fullmatch(r"[A-Za-z0-9._-]+", n) and pattern.search(n)), None)
        asset = assets.get(name, {})
        installer = asset.get("browser_download_url")
        size = asset.get("size")
        available = (_vtuple(latest) > _vtuple(__version__) and bool(installer)
                     and not rel.get("draft") and not rel.get("prerelease"))
        error = None
        if WINDOWS and available:
            try:
                error = (work / "install-error.txt").read_text(
                    encoding="utf-8-sig").strip()
            except OSError:
                pass
        _set(latest=latest, notes=rel.get("body") or "", url=rel.get("html_url"),
             available=available, error=error,
             installer=installer, sums=assets.get("SHA256SUMS.txt", {}).get("browser_download_url"),
             installer_name=name, installer_size=size if type(size) is int and size > 0 else 0,
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

class _IncompleteDownload(Exception):
    pass


class _InvalidRange(_IncompleteDownload):
    pass


def _download_segment(url, dest, offset, expected_size):
    headers = {"Accept": "application/octet-stream", "Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    with _get(url, timeout=45, headers=headers) as response:
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise _IncompleteDownload("服务器返回了意外的内容编码")
        length = response.headers.get("Content-Length")
        if length is not None and not re.fullmatch(r"[0-9]+", length):
            raise _IncompleteDownload("响应长度无效")
        length = int(length) if length is not None else None
        if response.status == 206:
            match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)",
                                 response.headers.get("Content-Range", ""))
            if not match:
                raise _InvalidRange("续传范围无效")
            start, end, total = map(int, match.groups())
            if (start != offset or not start <= end < total
                    or (expected_size and total != expected_size)
                    or (length is not None and length != end - start + 1)):
                raise _InvalidRange("续传范围与安装包不一致")
            stop = end + 1
        elif response.status == 200:
            # A server may ignore Range. Its response starts at zero.
            offset = 0
            if expected_size and length is not None and length != expected_size:
                raise _IncompleteDownload("响应长度与发布的安装包不一致")
            total = stop = expected_size or length
        else:
            raise _IncompleteDownload(f"意外的下载响应 HTTP {response.status}")
        done = offset
        _set(progress=min(99, done * 100 // total) if total else None)
        with dest.open("ab" if offset else "wb") as file:
            while chunk := response.read1(1 << 16):
                done += len(chunk)
                if stop is not None and done > stop:
                    raise _InvalidRange("服务器发送了超出声明范围的数据")
                file.write(chunk)
                if total:
                    _set(progress=min(99, done * 100 // total))
        if done == 0 or (total is not None and done != total):
            raise _IncompleteDownload("连接在下载完成前结束")


def _download(url, dest, expected_size=0, keep_partial=True):
    for attempt in range(DOWNLOAD_ATTEMPTS):
        if attempt:
            _set(download_retry=attempt)
            time.sleep(min(2 ** (attempt - 1), 4))
        offset = dest.stat().st_size if dest.exists() else 0
        if expected_size and offset == expected_size:
            return
        if expected_size and offset > expected_size:
            dest.unlink()
            offset = 0
        try:
            _download_segment(url, dest, offset, expected_size)
            return
        except HTTPError as exc:
            exc.close()
            if exc.code == 416 and offset:
                dest.unlink(missing_ok=True)
            elif exc.code not in (408, 429, 500, 502, 503, 504):
                raise RuntimeError(f"下载安装包失败（HTTP {exc.code}）") from exc
        except _InvalidRange:
            dest.unlink(missing_ok=True)
        except (_IncompleteDownload, URLError, TimeoutError, ConnectionError, ssl.SSLError,
                http.client.HTTPException):
            pass
    saved = "已保留下载进度。" if keep_partial and dest.exists() and dest.stat().st_size else ""
    raise RuntimeError(f"安装包下载不完整。{saved}请检查网络后再次点击「立即更新」。")


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
            if len(parts) == 2 and re.fullmatch(r"[a-fA-F0-9]{64}", parts[0]) and parts[1].lstrip("*") == name:
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
# Installed by 种草调研助手's updater: wait for the app to quit, swap in the new version, relaunch.
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
    work = work.resolve()
    exe = Path(sys.executable).resolve()
    log = work / "install-helper.log"
    ready = work / "install-ready"
    failed = work / "install-error.txt"
    ready.unlink(missing_ok=True)
    failed.unlink(missing_ok=True)

    def literal(value):
        # In PowerShell single-quoted literals, smart quotes also act as delimiters.
        return "'" + re.sub(r"['‘’]", lambda match: match[0] * 2, str(value)) + "'"

    ps = work / "install.ps1"
    ps.write_text(f"""$ErrorActionPreference = 'Stop'
$exe = {literal(exe)}
$failure = {literal(failed)}
$errorLog = {literal(log)}
$appExited = $false
function Save-Failure($message) {{
    Write-Output $message
    [IO.File]::WriteAllText($failure, "Windows 更新失败：$message。日志：" + $errorLog)
}}
try {{
    $app = Get-Process -Id {os.getpid()}
    # Open the process handle before telling the app it can exit.
    $null = $app.Handle
    Write-Output "Waiting for app PID $($app.Id)"
    [IO.File]::WriteAllText({literal(ready)}, '')
    if (-not $app.WaitForExit(60000)) {{
        throw 'The app did not exit within 60 seconds.'
    }}
    $app.Dispose()
    $appExited = $true
    $setup = Start-Process -FilePath {literal(setup.resolve())} -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CLOSEAPPLICATIONS', '/NORESTARTAPPLICATIONS',
        {literal(f'/DIR="{exe.parent}"')}, {literal(f'/LOG="{work / "inno-setup.log"}"')}
    )
    Write-Output "Installer exit code: $($setup.ExitCode)"
    if ($setup.ExitCode -ne 0) {{
        throw "Installer exit code: $($setup.ExitCode)"
    }}
}} catch {{
    Save-Failure $_.Exception.Message
}}
if ($appExited) {{
    try {{
        $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
        Start-Process -FilePath $exe -WorkingDirectory {literal(exe.parent)}
        Write-Output 'App relaunched.'
    }} catch {{
        if (Test-Path -LiteralPath $failure) {{
            Write-Output $_.Exception.Message
        }} else {{
            Save-Failure $_.Exception.Message
        }}
    }}
}}
if (Test-Path -LiteralPath $failure) {{ exit 1 }}
""", encoding="utf-8-sig")
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    # DETACHED_PROCESS combined with PowerShell's -WindowStyle can silently skip
    # the script. CREATE_NO_WINDOW hides the console without that combination.
    try:
        with log.open("wb") as output:
            helper = subprocess.Popen([str(powershell), "-NoProfile", "-NonInteractive",
                                       "-ExecutionPolicy", "Bypass", "-File", str(ps)],
                                      cwd=work, stdin=subprocess.DEVNULL, stdout=output,
                                      stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as e:
        raise RuntimeError(f"Windows 更新助手未能启动：{e}。日志：{log}") from e
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if helper.poll() is not None:
            break
        if ready.exists():
            return helper
        time.sleep(0.1)
    if helper.poll() is None:
        helper.kill()
        helper.wait()
    raise RuntimeError(f"Windows 更新助手未能启动，应用仍在运行。日志：{log}")


def _prepare_installer(st, work):
    name = st["installer_name"]
    expected = _expected_sha(st.get("sums"), name)
    if WINDOWS and not expected:
        raise RuntimeError("这个版本没有提供有效的校验文件，请到 Release 页面手动下载")
    work.mkdir(parents=True, exist_ok=True)
    dest = work / name
    size = st.get("installer_size", 0)
    lock = work / "download.lock"
    if paths.is_redirected(lock):
        raise RuntimeError("更新锁文件指向其他位置，无法下载")
    with file_lock(lock, timeout=0):
        if not expected:
            # Older macOS releases without a checksum still get the app-signature gate.
            with tempfile.TemporaryDirectory(prefix="download-", dir=work) as staging:
                partial = Path(staging) / "installer.part"
                _download(st["installer"], partial, size, keep_partial=False)
                _set(phase="verifying", progress=100)
                partial.replace(dest)
            return dest
        if dest.is_file() and not paths.is_redirected(dest) and _sha256(dest) == expected:
            return dest
        # The release digest scopes the partial across clicks and process restarts.
        partial = work / f"download-{expected}.part"
        if paths.is_redirected(partial):
            raise RuntimeError("安装包临时文件指向其他位置，无法下载")
        for attempt in range(2):
            _set(phase="downloading", download_retry=attempt, progress=None)
            _download(st["installer"], partial, size)
            _set(phase="verifying", progress=100, download_retry=0)
            if _sha256(partial) == expected:
                partial.replace(dest)
                return dest
            partial.unlink()
        raise RuntimeError("下载的安装包 SHA-256 校验失败。请重试，或从发布页下载安装包。")


def start(on_ready_to_quit):
    """Download + verify + hand over to the installer helper, in the background.
    Calls on_ready_to_quit() once the helper is waiting for us to exit."""
    if not supported():
        raise RuntimeError("只有安装版支持自动更新；源码运行请用 git pull")
    with _lock:
        if _state["phase"] in BUSY_PHASES:
            return
        if not _state["available"]:
            raise RuntimeError("没有可用的新版本")
        st = _status_locked()
        _state.update(phase="downloading", progress=None, download_retry=0, error=None)

    def run():
        try:
            work = _update_directory(st["latest"])
            dest = _prepare_installer(st, work)
            _set(phase="installing")
            if WINDOWS:
                helper = _windows_install(dest, work)
                on_ready_to_quit()
                exit_code = helper.wait()
                if exit_code:
                    try:
                        error = (work / "install-error.txt").read_text(encoding="utf-8-sig").strip()
                    except OSError:
                        error = f"Windows 更新助手退出（{exit_code}）。日志：{work / 'install-helper.log'}"
                    _set(phase="error", error=error)
            elif _mac_install(dest, work):
                on_ready_to_quit()
        except Exception as e:
            _set(phase="error", error=str(e))
    threading.Thread(target=run, daemon=True).start()
