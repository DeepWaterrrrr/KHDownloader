# -*- mode: python ; coding: utf-8 -*-
"""KHDownloader —— 面向用户的最终程序（无阶段尾号）。

三点和旧 spec 的区别，都是 v008 §10.1 要求的：

1. ``name`` 用无尾号的 ``KHDownloader``（不是 ``KHDownloader_v008``）；
2. ``icon`` 指定 ``assets/KHDownloader.ico`` —— 图标会编进 PE 资源表，
   资源管理器 / 快捷方式 / 任务栏读的是这一份；
3. ``datas`` 把 ico 和 png 作为**运行数据**打进 onefile 包，
   窗口图标在运行时从 ``sys._MEIPASS/assets/`` 读（见 khdownloader_gui.resource_path）。

必须用本 spec 构建（``pyinstaller KHDownloader.spec``）；
命令行再传 ``--name`` / ``--icon`` 不会覆盖 spec 里的字段，
所以 build_exe.bat 也走 spec，保证脚本与 spec 不产生两个不同的最终程序名。

v1.0.0 追加（"防火墙问题"的修复要能带到 EXE 里）：

4. curl_cffi 必须**连同它自带的 libcurl 动态库和 cacert.pem** 一起打进包，
   所以这里用 ``collect_all('curl_cffi')`` 而不是手写 hiddenimports。
   少了这一坨，EXE 在别人机器上会静默退回普通请求，又被 Cloudflare 拦住 ——
   也就是说"修好了"的只是源码，发给用户的那个 exe 还是坏的。
   因此本 spec 的立场是：**没装 curl_cffi 就拒绝构建**（和缺图标就拒绝构建同一个道理）。
5. playwright 明确排除：它自带 node 驱动，体积上百 MB；而打包后的兜底路径
   改用"借本机 Chrome/Edge 无头渲染"（见 khdownloader_transport.render_with_system_browser），
   不需要把浏览器内核塞进 EXE。
"""

# PyInstaller 执行 spec 时 CWD = spec 所在目录，SPECPATH = spec 所在目录绝对路径。
# 不同机器上 spec 可能被构建命令用绝对/相对路径引用，所以**从 SPECPATH 向上找**入口脚本，
# 不依赖 CWD，也不假设 spec 一定在项目根。
from pathlib import Path

_spec_dir = Path(SPECPATH).resolve()
PROJECT_ROOT = next(
    (d for d in (_spec_dir, *_spec_dir.parents) if (d / "khdownloader_gui.py").is_file()),
    _spec_dir,
)
GUI_ENTRY = PROJECT_ROOT / "khdownloader_gui.py"
ASSETS = PROJECT_ROOT / "assets"

if not GUI_ENTRY.is_file():
    raise SystemExit(f"找不到入口脚本：{GUI_ENTRY}")

for _icon_name in ("KHDownloader.ico", "KHDownloader.png"):
    if not (ASSETS / _icon_name).is_file():
        raise SystemExit(
            f"找不到图标资源：{ASSETS / _icon_name}\n"
            "没有真实图标文件时不允许用旧默认图标冒充完成，"
            "请让用户补充 Strategy AI 提供的 assets/KHDownloader.ico 与 .png。"
        )

# ---- v1.0.0：把"换浏览器指纹"这一层打进 EXE，否则发出去的还是被人机校验拦住的那个版本。
try:
    from PyInstaller.utils.hooks import collect_all

    _cffi_datas, _cffi_binaries, _cffi_hiddenimports = collect_all("curl_cffi")
except Exception as _exc:  # pragma: no cover - 依赖环境
    _cffi_datas, _cffi_binaries, _cffi_hiddenimports = [], [], []
    _collect_error = f"{type(_exc).__name__}: {_exc}"
else:
    _collect_error = ""

if not _cffi_hiddenimports:
    raise SystemExit(
        "找不到 curl_cffi。\n"
        "v1.0.0 起 EXE 必须带上它，否则打包出来的程序仍然会被站点的程序化访问校验拦住，\n"
        "等于「修好的只是源码、发给用户的那个 exe 还是坏的」。\n"
        "请先安装：python -m pip install curl_cffi\n"
        + (f"（collect_all 报错：{_collect_error}）" if _collect_error else "")
    )

a = Analysis(
    [str(GUI_ENTRY)],
    pathex=[str(PROJECT_ROOT)],
    binaries=list(_cffi_binaries),
    datas=[
        # (源文件, 包内目标目录)。目标目录必须和 khdownloader_gui.ASSET_SUBDIR 一致。
        (str(ASSETS / "KHDownloader.ico"), "assets"),
        (str(ASSETS / "KHDownloader.png"), "assets"),
        # curl_cffi 的 cacert.pem 等数据文件 + 自带的 libcurl 动态库。
        *list(_cffi_datas),
    ],
    hiddenimports=list(_cffi_hiddenimports) + ["_cffi_backend"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # playwright 不进 EXE：兜底路径用"本机 Chrome/Edge 无头渲染"代替（体积差上百 MB）。
    excludes=["playwright"],
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
    name="KHDownloader",                 # ← 无阶段尾号的最终程序名
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                       # 图形界面程序，不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ASSETS / "KHDownloader.ico"),   # ← 编入 PE 资源表
)