# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

webview_datas, webview_binaries, webview_hidden = collect_all("webview")
clr_datas, clr_binaries, clr_hidden = collect_all("clr")
loader_datas, loader_binaries, loader_hidden = collect_all("clr_loader")
pythonnet_datas, pythonnet_binaries, pythonnet_hidden = collect_all("pythonnet")

datas = webview_datas + clr_datas + loader_datas + pythonnet_datas + [
    ("dashboard.html", "."),
    ("assets/app.ico", "assets"),
]
binaries = webview_binaries + clr_binaries + loader_binaries + pythonnet_binaries
hiddenimports = webview_hidden + clr_hidden + loader_hidden + pythonnet_hidden

a = Analysis(
    ["agent_token_monitor.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name="AgentTokenMonitor",
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
    icon=["assets/app.ico"],
)
