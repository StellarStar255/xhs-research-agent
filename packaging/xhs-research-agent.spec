# PyInstaller spec: one-folder build with two executables sharing the same files.
#   XHS Research Agent  — windowed launcher (starts the local server, opens the browser)
#   xhs-cli             — console CLI the agent backends run for scraping
# Build:  pyinstaller packaging/xhs-research-agent.spec   (from the repo root)
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
PKG = ROOT / "xhs_reader"
VERSION = re.search(r'__version__ = "(.+?)"', (PKG / "__init__.py").read_text()).group(1)
APP_NAME = "XHS Research Agent"
MAC = sys.platform == "darwin"
WIN = sys.platform == "win32"

# Playwright drives the user's installed Chrome/Edge through its bundled Node.js driver.
# Ship the driver (not any browser); node itself goes in as a binary so it keeps its
# executable bit and gets code-signed.
driver = Path(__import__("playwright").__file__).parent / "driver"
node = driver / ("node.exe" if WIN else "node")
pw_datas = [d for d in collect_data_files("playwright", include_py_files=False) if Path(d[0]) != node]

datas = [
    (str(PKG / "static"), "xhs_reader/static"),
    (str(PKG / "agent_prompt.md"), "xhs_reader"),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "THIRD_PARTY_LICENSES.txt"), "."),
] + pw_datas
binaries = [(str(node), "playwright/driver")]
hidden = collect_submodules("uvicorn") + collect_submodules("xhs_reader")

common = dict(pathex=[str(ROOT)], binaries=binaries, datas=datas, hiddenimports=hidden,
              excludes=["tkinter", "matplotlib", "numpy", "PIL", "IPython"])
gui_a = Analysis([str(ROOT / "packaging" / "entry_gui.py")], **common)
cli_a = Analysis([str(ROOT / "packaging" / "entry_cli.py")], **common)

# Real code signing happens afterwards in packaging/macos_sign_notarize.sh, which also
# signs Playwright's node binary; PyInstaller only ad-hoc signs here.
icon = str(ROOT / "packaging" / ("icon.icns" if MAC else "icon.ico"))
utf8 = [("X utf8", None, "OPTION")]  # UTF-8 mode: Chinese output on Windows consoles/pipes

gui_exe = EXE(PYZ(gui_a.pure), gui_a.scripts, utf8, exclude_binaries=True, name=APP_NAME,
              console=False, icon=icon)
cli_exe = EXE(PYZ(cli_a.pure), cli_a.scripts, utf8, exclude_binaries=True, name="xhs-cli",
              console=True, icon=icon)

coll = COLLECT(gui_exe, cli_exe,
               gui_a.binaries, gui_a.datas, cli_a.binaries, cli_a.datas,
               name=APP_NAME)

if MAC:
    app = BUNDLE(
        coll, name=f"{APP_NAME}.app", icon=icon, version=VERSION,
        bundle_identifier="io.github.stellarstar255.xhs-research-agent",
        info_plist={
            "CFBundleDisplayName": "小红书调研助手",
            "CFBundleName": "小红书调研助手",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSUIElement": True,          # no Dock icon: the UI lives in the browser
            "LSMinimumSystemVersion": "11.0",
            "NSHumanReadableCopyright": "MIT License · 与小红书无关联",
        },
    )
