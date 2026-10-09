#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双击启动入口（开发模式）。

真正的界面代码在 bili_gui.py —— 拆成 .py 模块是为了能被 PyInstaller 正确打包。
打包后的用户请直接运行 BiliDown.exe，不需要本文件。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bili_gui import main  # noqa: E402

if __name__ == "__main__":
    main()
