# -*- mode: python ; coding: utf-8 -*-
# Preserve imported standard libraries and their DLL dependencies.
a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=["pystray._win32"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["numpy", "scipy", "tkinter", "PIL.ImageTk", "PIL.ImageQt",
              "PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure, optimize=1)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="GameSwitcher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
)
