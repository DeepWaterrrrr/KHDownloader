# assets/ —— 应用图标资源

| 文件 | 用途 | 谁读它 |
| --- | --- | --- |
| `KHDownloader.ico` | Windows 图标，7 种规格 16/24/32/48/64/128/256，全部 32bpp | PyInstaller spec 的 `icon=`（编进 EXE 的 PE 资源表）；运行时 `khdownloader_gui.apply_window_icon()` 读它设窗口图标 |
| `KHDownloader.png` | 原始透明 PNG（1254×1254 RGBA），用于后续维护或 Tk 兜底 | `khdownloader_gui.apply_window_icon()` 的 PNG 兜底分支 |

两份文件都必须用本目录名和本目录位置：`KHDownloader.spec` 的 `datas` 把它们打进包内 `assets/`，运行时按 `sys._MEIPASS/assets/` 定位。
改文件名或移动位置会让窗口退回系统默认图标。
