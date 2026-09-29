# -*- mode: python ; coding: utf-8 -*-
"""One-file Windows build; Chromium is bundled under pw-browsers."""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


ROOT = Path(__file__).resolve().parent
browser_root_value = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
BROWSER_ROOT = Path(browser_root_value) if browser_root_value else None
if BROWSER_ROOT is None or not BROWSER_ROOT.is_dir():
    raise SystemExit(
        "Playwright browser bundle not found. Run build_windows_exe.bat first."
    )

playwright_datas, playwright_binaries, playwright_hiddenimports = collect_all("playwright")
datas = playwright_datas + [(str(BROWSER_ROOT), "pw-browsers")]

a = Analysis(
    [str(ROOT / "Dankoba_Helper_Local.py")],
    pathex=[str(ROOT)],
    binaries=playwright_binaries,
    datas=datas,
    hiddenimports=playwright_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
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
