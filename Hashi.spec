# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller ビルド定義。

    pyinstaller Hashi.spec

keyring はバックエンドをエントリポイントで動的解決するため、凍結時に取りこぼす。
明示的に収集する。Windows の資格情報マネージャ backend は win32ctypes に依存。
"""
from PyInstaller.utils.hooks import collect_submodules

hidden = []
hidden += collect_submodules("keyring")
hidden += collect_submodules("win32ctypes")   # Windows keyring backend の依存
hidden += [
    "keyring.backends.Windows",
    "keyring.backends.macOS",
    "keyring.backends.SecretService",
    "keyring.backends.chainer",
    "keyring.backends.fail",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3D"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# onefile: バイナリ類を EXE に同梱(配布は単一 Hashi.exe)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Hashi",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,        # GUI アプリ(コンソール窓を出さない)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/hashi.ico",   # 橋モチーフの盾アイコン(assets/hashi.png が元画像)
)
