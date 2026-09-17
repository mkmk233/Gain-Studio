# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_dynamic_libs

binaries = collect_dynamic_libs("rawpy") + collect_dynamic_libs("pillow_heif")
datas = [
    ("tools", "tools"),
    ("assets", "assets"),
    ("licenses", "licenses"),
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=["png", "pillow_heif", "_pillow_heif"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtMultimedia", "PySide6.QtNetworkAuth",
    ],
    noarchive=False,
)

# Qt on current Windows versions links against the operating system's unversioned
# ICU shim (System32\\icuuc.dll). A developer PATH may contain a third-party ICU
# build whose exports are version-suffixed (for example ucnv_open_78). If
# PyInstaller accidentally collects that file as _internal\\icuuc.dll, it shadows
# the Windows shim and QtCore fails to load with WinError 127. Never ship such a
# root-level ICU DLL; tool-local DLLs under tools\\ remain untouched in datas.
_windows_icu_names = {"icuuc.dll", "icuin.dll"}
a.binaries = [
    item for item in a.binaries
    if item[0].lower() not in _windows_icu_names
    and not (
        item[0].lower().startswith("icudt")
        and item[0].lower().endswith(".dll")
    )
]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Gain Studio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon="assets/app_icon.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Gain Studio",
)
