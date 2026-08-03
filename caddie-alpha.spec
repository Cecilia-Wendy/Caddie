# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules
from app_version import APP_VERSION

ROOT = Path(SPEC).resolve().parent

hiddenimports = collect_submodules("webview") + collect_submodules("fastembed") + [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "static"), "static"),
        (str(ROOT / "scripts"), "scripts"),
        (str(ROOT / "integrations"), "integrations"),
        (str(ROOT / "assets" / "update_public_key.pem"), "assets"),
        (str(ROOT / "assets" / "update_channel.json"), "assets"),
        (str(ROOT / "assets" / "telemetry_channel.json"), "assets"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "tensorflow"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Caddie",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Caddie",
)

app = BUNDLE(
    coll,
    name="Caddie.app",
    icon=str(ROOT / "assets" / "Caddie.png"),
    bundle_identifier="app.caddie.alpha",
    version=APP_VERSION,
    info_plist={
        "CFBundleDisplayName": "Caddie",
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Caddie Alpha",
    },
)
