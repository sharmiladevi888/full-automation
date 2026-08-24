# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Continuity Studio (Windows desktop app).

Build:
    pyinstaller ContinuityStudio.spec --noconfirm

Produces a one-folder app at dist/ContinuityStudio/ that needs NO Python and
NO system ffmpeg on the target machine. Inno Setup wraps dist/ into Setup.exe.
"""
from PyInstaller.utils.hooks import collect_submodules, collect_data_files
import os

block_cipher = None

# --- Heavy / dynamically-imported packages need explicit submodule collection.
hidden = []
for pkg in ("uvicorn", "fastapi", "starlette", "anyio", "click",
            "faster_whisper", "ctranslate2", "onnxruntime",
            "tokenizers", "huggingface_hub", "av", "PIL", "webview"):
    try:
        hidden += collect_submodules(pkg)
    except Exception:
        pass

# Local app modules that are imported by name / only on a fallback path so the
# static analysis can miss them. Bundle them all explicitly.
hidden += [
    "app", "config", "store", "pipeline", "editor", "derouter",
    "claude_client", "vault_crypto", "voice", "audio_gen", "youtube",
    "transcribe", "image_queue", "video", "pollinations", "diffusers",
    "gen_with_refs", "punchup", "nowindow", "runtime_security", "ui_patch",
]

datas = [
    ("static", "static"),          # the entire UI (index.html etc.)
    ("ffmpeg_bin", "ffmpeg"),      # bundled ffmpeg.exe + ffprobe.exe -> ffmpeg/
    ("assets", "assets"),          # app icon (icon.ico / icon.png) for the window
]
# onnxruntime / ctranslate2 ship native DLLs + capi data that must come along.
for pkg in ("onnxruntime", "ctranslate2", "faster_whisper", "av"):
    try:
        datas += collect_data_files(pkg)
    except Exception:
        pass

a = Analysis(
    ["desktop.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "tensorflow", "matplotlib", "tkinter", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Strip sensitive / user-specific files that should NOT be bundled into the
# installer.  Each user's vault.json / users.json / codes.json / .secret is
# created at first run in %LOCALAPPDATA%\\ContinuityStudio\\.  Bundling a
# dev-machine copy would cause encryption-key mismatch errors on install.
_strip = {"vault.json", "users.json", "codes.json", ".secret"}
a.datas = [t for t in a.datas if os.path.basename(t[0]) not in _strip]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ContinuityStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # no console window (GUI app)
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ContinuityStudio",
)
