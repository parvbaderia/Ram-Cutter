# -*- mode: python ; coding: utf-8 -*-
# ============================================================
#  RAM Cutter — PyInstaller spec
#  Produces a single windowed .exe that auto-requests UAC elevation.
#  Build with:  python -m PyInstaller "RAM Cutter.spec" --noconfirm
#           or: double-click build.bat
# ============================================================

import os

block_cipher = None

# All source .py files live flat in the project root
a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'psutil',
        'psutil._pswindows',
        'ctypes',
        'ctypes.wintypes',
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.filedialog',
        'tkinter.simpledialog',
        'logging',
        'logging.handlers',
        'threading',
        'queue',
        'json',
        'os',
        'sys',
        'time',
        'math',
        'collections',
        'dataclasses',
        'typing',
        # project modules
        'dashboard',
        'monitor',
        'backend',
        'game_ready',
        'config',
        'status',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'numpy', 'scipy', 'PIL', 'cv2'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='RAM Cutter',
    icon='assets/RAM Cutter.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Windowed (no console window) — crashes still appear via the
    # "Press Enter to close this window..." handler in main.py
    console=False,
    # Request admin elevation via the Windows UAC manifest.
    # The OS will show the blue UAC prompt before the app starts —
    # no Python code needed to trigger it.
    uac_admin=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
