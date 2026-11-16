# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir WebView2 build for the original-stream downloader."""

from PyInstaller.utils.hooks import collect_all

_webview_datas, _webview_binaries, _webview_hidden = collect_all("webview")

_hidden_imports = [
    "bili_clipboard_dolby",
    "bili_raw_downloader_core",
    "webview",
    "requests",
    "urllib3",
    "charset_normalizer",
    "certifi",
    "idna",
] + _webview_hidden

a = Analysis(
    ["bili_raw_downloader.pyw"],
    pathex=["."],
    binaries=_webview_binaries,
    datas=[
        ("bili_raw_ui", "bili_raw_ui"),
        ("bili_raw_downloader.ico", "."),
    ] + _webview_datas,
    hiddenimports=_hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "cefpython3"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BiliRawDownloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # pythonnet's managed runtime DLL must remain byte-for-byte loadable by
    # .NET; UPX can make frozen WebView2 builds fail before the window opens.
    upx=False,
    console=False,
    icon="bili_raw_downloader.ico",
    manifest="bili_raw_downloader.manifest",
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="BiliRawDownloader",
)
