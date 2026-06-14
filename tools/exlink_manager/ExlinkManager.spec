# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

block_cipher = None
manager_dir = Path(SPECPATH).resolve()
tools_dir = manager_dir.parent
repo_dir = tools_dir.parent

a = Analysis(
    [str(manager_dir / "main.py")],
    pathex=[str(repo_dir), str(tools_dir), str(manager_dir)],
    binaries=[],
    datas=[(str(manager_dir / "resources"), "tools/exlink_manager/resources")],
    hiddenimports=[
        "serial",
        "serial.tools",
        "serial.tools.list_ports",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "exlink_jtag_test",
        "exlink_xvc_server",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name="ExlinkManager",
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
