# -*- coding: utf-8 -*-
"""源码态图标检查：resource_path / apply_window_icon 是否按预期工作。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk  # noqa: E402

from khdownloader_gui import (  # noqa: E402
    ASSET_SUBDIR,
    ICON_ICO_NAME,
    ICON_PNG_NAME,
    apply_window_icon,
    resource_path,
    resource_roots,
)

print("cwd            :", os.getcwd())
print("resource_roots :", [str(r) for r in resource_roots()])
ico = resource_path(ICON_ICO_NAME)
png = resource_path(ICON_PNG_NAME)
print("ico  ->", ico, ico.stat().st_size if ico else None)
print("png  ->", png, png.stat().st_size if png else None)
assert ico and ico.is_file(), "源码态找不到 ICO"
assert png and png.is_file(), "源码态找不到 PNG"
assert str(ico).replace("\\", "/").endswith(f"{ASSET_SUBDIR}/{ICON_ICO_NAME}")

root = tk.Tk()
root.update_idletasks()
mode = apply_window_icon(root)
print("apply_window_icon ->", mode)
assert mode != "none", "源码态窗口图标没生效"

# cwd 在别处时也必须能定位（模拟从任意目录启动）
os.chdir(str(Path.home()))
print("切到 HOME 后 ico 仍可定位 ->", resource_path(ICON_ICO_NAME))
assert resource_path(ICON_ICO_NAME) is not None, "依赖 cwd 了，不符合要求"
os.chdir(str(Path(__file__).resolve().parent))

root.destroy()
print("OK：源码态图标检查通过")