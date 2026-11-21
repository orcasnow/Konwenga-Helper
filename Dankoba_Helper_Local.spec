# -*- mode: python ; coding: utf-8 -*-
"""One-file Windows build; Chromium is bundled under pw-browsers."""

import os
import sys
import sysconfig
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


# PyInstaller spec execution does not define __file__; SPECPATH is supplied by
# PyInstaller and points to the directory containing the spec file.
ROOT = Path(SPECPATH or ".").resolve()
browser_root_value = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
BROWSER_ROOT = Path(browser_root_value) if browser_root_value else None
if BROWSER_ROOT is None or not BROWSER_ROOT.is_dir():
    raise SystemExit(
        "Playwright browser bundle not found. Run build_windows_exe.bat first."
    )

playwright_datas, playwright_binaries, playwright_hiddenimports = collect_all("playwright")
datas = playwright_datas + [(str(BROWSER_ROOT), "pw-browsers")]

# Some per-user Python installations have working tkinter modules but PyInstaller
# cannot initialize Tcl while probing it. Collect the standard Windows Tcl/Tk
# files explicitly so the GUI is still available in that case.
TCL_ROOT = Path(sys.base_prefix) / "tcl"
try:
    import _tkinter

    TCL_VERSION = _tkinter.TCL_VERSION
    TK_VERSION = _tkinter.TK_VERSION
except (ImportError, AttributeError):
    # Fall back to the pre-3.15 Windows layout if Tcl/Tk cannot be queried.
    TCL_VERSION = TK_VERSION = "8.6"

TCL_DATA = TCL_ROOT / f"tcl{TCL_VERSION}"
TK_DATA = TCL_ROOT / f"tk{TK_VERSION}"
TCL_MODULES = TCL_ROOT / f"tcl{TCL_VERSION.split('.')[0]}"
for source, destination in (
    (TCL_DATA, "_tcl_data"),
    (TK_DATA, "_tk_data"),
    (TCL_MODULES, f"tcl{TCL_VERSION.split('.')[0]}"),
):
    if source.is_dir():
        datas.extend(
            (
                str(path),
                (Path(destination) / path.relative_to(source).parent).as_posix(),
            )
            for path in source.rglob("*")
            if path.is_file()
        )

tkinter_runtime_hook = (
    Path(sysconfig.get_paths()["purelib"])
    / "PyInstaller"
    / "hooks"
    / "rthooks"
    / "pyi_rth__tkinter.py"
)

a = Analysis(
    [str(ROOT / "Dankoba_Helper_Local.py")],
    pathex=[str(ROOT)],
    binaries=playwright_binaries,
    datas=datas,
    hiddenimports=playwright_hiddenimports + [
        "tkinter",
        "_tkinter",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "tkinter.ttk",
    ],
    hookspath=[str(ROOT / "build_hooks")],
    hooksconfig={},
    runtime_hooks=[str(tkinter_runtime_hook)] if tkinter_runtime_hook.is_file() else [],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Dankoba_Helper_Local",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
