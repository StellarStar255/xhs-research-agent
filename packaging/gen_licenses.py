"""Write THIRD_PARTY_LICENSES.txt for the packaged app.

Covers every runtime dependency (the closure of requirements.txt, not build tools),
the Python runtime, and points to Playwright's own notices, which ship inside the
bundle unchanged (playwright/driver/LICENSE for Node.js, driver/package/LICENSE,
NOTICE and ThirdPartyNotices.txt for Playwright).
"""
import re
import subprocess
import sys
import sysconfig
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def closure(names):
    seen, todo = {}, list(names)
    while todo:
        name = re.split(r"[<>=!~\[; ]", todo.pop())[0].strip()
        key = name.lower().replace("_", "-")
        if not key or key in seen:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        seen[key] = dist.metadata["Name"]
        for req in dist.requires or []:
            if "extra ==" not in req:  # skip optional extras
                todo.append(req)
    return sorted(seen.values(), key=str.lower)


def main():
    reqs = [l.strip() for l in (ROOT / "requirements.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
    pkgs = closure(reqs)
    body = subprocess.run(
        [sys.executable, "-m", "piplicenses", "--packages", *pkgs, "--format=plain-vertical",
         "--with-license-file", "--no-license-path", "--with-urls"],
        check=True, capture_output=True, text=True).stdout
    py_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    out = [
        "小红书调研助手 (xhs-research-agent) — third-party software notices",
        "=" * 70,
        "",
        "This application bundles the following open-source software. Each is used",
        "under its own license, reproduced below.",
        "",
        "Not bundled: Google Chrome / Microsoft Edge (the user's installed browser is used)",
        "and Claude Code (the user's own installation is used, if any).",
        "",
        "Playwright's driver ships with its own notices inside the app, unchanged:",
        "  playwright/driver/LICENSE                       (Node.js)",
        "  playwright/driver/package/LICENSE, NOTICE,",
        "  playwright/driver/package/ThirdPartyNotices.txt (Playwright and its dependencies)",
        "",
        "The executables are built with PyInstaller, whose bootloader is licensed under",
        "the GPL with an exception that permits distribution in any application.",
        "",
        "=" * 70,
        "Python " + sys.version.split()[0],
        "=" * 70,
        py_license.read_text(encoding="utf-8", errors="replace") if py_license.exists() else "PSF License (see https://docs.python.org/3/license.html)",
        "",
        "=" * 70,
        "Python packages",
        "=" * 70,
        body,
    ]
    (ROOT / "THIRD_PARTY_LICENSES.txt").write_text("\n".join(out), encoding="utf-8")
    print(f"THIRD_PARTY_LICENSES.txt: {len(pkgs)} packages: {', '.join(pkgs)}")


if __name__ == "__main__":
    main()
