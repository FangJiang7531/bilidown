# -*- mode: python ; coding: utf-8 -*-
"""BiliDown v0.2 打包配置（onedir）。

目标：整包复制到一台「没有 Python、没有 ffmpeg、没有 yt-dlp」的电脑上，
双击 BiliDown.exe 就能用。

要点：
  * yt-dlp 的提取器是运行时动态 import 的，静态分析找不到，
    必须 collect_submodules('yt_dlp') 全部收进来，否则 B 站解析会失败。
  * ffmpeg / ffprobe 不打进 _internal，而是打包后放到 exe 同级的
    tools\\ 目录 —— 既让用户看得见、可替换，也避免体积翻倍。
"""
import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(SPECPATH)

hiddenimports = collect_submodules("yt_dlp")
hiddenimports += ["bili_core", "bili_gui"]

# 只在开发期用到的重型库，运行时完全不需要，排除掉以缩小体积
excludes = [
    "numpy", "pandas", "matplotlib", "scipy", "PIL", "IPython",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "notebook",
    "pytest", "unittest", "pydoc_data",
]

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[(os.path.join(ROOT, "app.ico"), ".")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BiliDown",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                 # 窗口程序，不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, "app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="BiliDown",
)
