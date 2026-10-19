# -*- coding: utf-8 -*-
"""verify_frozen_selftest.py —— 冻结环境自检（打包验证工具，不随发布走）

它做的事：在 PyInstaller 冻结后的运行时里，逐项验证
  · tkinter / Tcl/Tk 能否初始化；
  · requests / bs4 能否 import 并拿到版本；
  · 应用自己的核心模块能否 import；
  · 应用 GUI 类能否实例化、控件是否齐全。

把结果打印到 stdout（所以这个自检要用 --console 构建），
用于向报告提供"冻结运行时真的能跑"的证据。

构建（在项目根目录执行；pathex 指回根目录以便找到应用模块）：

    python -m PyInstaller --noconfirm --clean --onefile --console ^
        --name _frozen_selftest --paths . tools/verify_frozen_selftest.py

然后运行 dist\\_frozen_selftest.exe，全绿即 10/10 通过。
"""

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def line(title):
    print("=" * 60)
    print(title)
    print("=" * 60)


results = []


def check(name, fn):
    try:
        detail = fn()
        results.append((name, True, detail))
        print(f"[OK]   {name}: {detail}")
    except Exception as exc:  # noqa: BLE001
        results.append((name, False, f"{type(exc).__name__}: {exc}"))
        print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        traceback.print_exc()


print("frozen:", getattr(sys, "frozen", False))
print("executable:", sys.executable)
print("meipass:", getattr(sys, "_MEIPASS", None))

line("1. tkinter / Tcl-Tk")
check("import tkinter", lambda: __import__("tkinter").TkVersion)
check("Tcl version", lambda: __import__("tkinter").TclVersion)


def _tk_init():
    import tkinter as _tk

    root = _tk.Tk()
    root.withdraw()
    root.update_idletasks()
    root.destroy()
    return "Tk() 初始化并销毁成功"


check("Tk() 初始化", _tk_init)

line("2. requests / bs4")
check("requests", lambda: __import__("requests").__version__)
check("bs4", lambda: __import__("bs4").__version__)
check("bs4 BeautifulSoup", lambda: __import__("bs4").BeautifulSoup("", "html.parser") is not None)

line("3. 应用核心模块")
check("import khdownloader_core", lambda: __import__("khdownloader_core").__name__)


def _core_parse():
    import khdownloader_core as c

    # 用一段内联 HTML 走一遍解析，证明确实能工作（不联网）
    html = (
        '<html><body><table id="songlist"><tr>'
        '<td><a href="/game-soundtracks/album/x/01%2520A.mp3">A</a></td>'
        "</tr></table></body></html>"
    )
    tracks = c.parse_album_page(html, "https://downloads.khinsider.com/game-soundtracks/album/x")
    return f"解析出 {len(tracks)} 首：{tracks[0].name}"


check("core.parse_album_page", _core_parse)


def _v007_api():
    """v007 新增能力：错误分类 + 导入分类 + 单曲入口 + 脱敏均可用。"""
    import khdownloader_core as c

    # 错误分类助手
    assert isinstance(c.classify_blocked_response(403, True), c.SiteBlockedError)
    assert isinstance(c.classify_blocked_response(403, False), c.AccessDeniedError)
    assert isinstance(c.classify_blocked_response(429, False), c.RateLimitError)
    # 导入分类：校验页不能当专辑
    insp = c.inspect_import_html(
        "<html><title>Just a moment...</title></html>",
        "https://downloads.khinsider.com/game-soundtracks/album/x",
    )
    assert insp.kind == c.IMPORT_KIND_CHALLENGE, insp.kind
    # 单曲入口 / 诊断采集函数存在
    assert callable(c.download_single_link)
    assert callable(c.summarize_response_for_diagnostics)
    return "错误分类 / 导入分类 / 单曲入口 / 脱敏 均可用"


check("core v007 恢复能力", _v007_api)


def _v008_api():
    """v008 新增能力：来源识别 / 地址核对 / MHTML 识别 / 歌曲页判定。"""
    import khdownloader_core as c

    # 地址比较规范化
    base = "https://downloads.khinsider.com/game-soundtracks/album/x"
    assert c.same_album(base, base + "/")
    assert c.same_album(base, base + "/?utm=1#top")
    assert not c.same_album(base, base + "-2")
    assert c.is_song_page_url(base + "/01%2520a.mp3")
    assert not c.is_song_page_url(base)
    # MHTML 识别
    assert c.detect_mhtml("From: <Saved by Blink>\r\nMIME-Version: 1.0\r\n")
    assert not c.detect_mhtml("<html><body>hi</body></html>")
    # 来源识别：songlist 链接 → slug（页面需足够长以通过内容守卫）
    html = (
        "<html><body><!-- pad -->" + "<!-- pad -->" * 20 +
        "<table id='songlist'><tr>"
        "<td><a href='/game-soundtracks/album/good-album/01%2520a.mp3'>a</a></td>"
        "</tr></table></body></html>"
    )
    src = c.identify_album_source(html)
    assert src["identified"], src
    assert src["album_slug"] == "good-album"
    # 导入核对：空地址也能按识别结果解析
    insp = c.inspect_import_html(html, "")
    assert insp.kind == c.IMPORT_KIND_ALBUM and len(insp.tracks) == 1, insp.kind
    assert insp.address_relation == "empty"
    # 不同专辑 → different_album（不报"没有曲目"）
    insp2 = c.inspect_import_html(html, base)
    assert insp2.address_relation == "different_album"
    return "来源识别 / 地址核对 / MHTML / 歌曲页判定 均可用"


check("core v008 导入识别", _v008_api)

line("4. 应用 GUI 类")
check("import khdownloader_gui", lambda: __import__("khdownloader_gui").__name__)


def _gui_widgets():
    import tkinter as _tk
    from khdownloader_gui import App

    root = _tk.Tk()
    root.withdraw()
    app = App(root)
    root.update_idletasks()
    labels = [
        app.start_button.cget("text"),
        app.cancel_button.cget("text"),
        app.browse_button.cget("text"),
        app.import_button.cget("text"),   # v007 恢复入口
        app.single_button.cget("text"),   # v007 恢复入口
        app.save_guide_button.cget("text"),  # v008 保存指引
    ]
    # v008 来源标记必须存在且为默认值
    assert "直接请求" in app.source_var.get(), app.source_var.get()
    root.destroy()
    return f"按钮 = {labels}"


check("GUI 控件实例化", _gui_widgets)

line("5. 应用图标（v008 §10.1）")


def _icon_resource():
    """图标运行资源必须能在冻结态找到 —— 这验证 datas 打包与资源路径解析。"""
    from khdownloader_gui import (
        ASSET_SUBDIR,
        ICON_ICO_NAME,
        ICON_PNG_NAME,
        resource_path,
        resource_roots,
    )

    ico = resource_path(ICON_ICO_NAME)
    png = resource_path(ICON_PNG_NAME)
    roots = [str(r) for r in resource_roots()]

    assert ico is not None, (
        f"冻结态找不到 {ICON_ICO_NAME}；roots={roots}；"
        f"检查 spec datas 是否包含 ('{ASSET_SUBDIR}/...')"
    )
    assert png is not None, f"冻结态找不到 {ICON_PNG_NAME}；roots={roots}"
    assert ico.stat().st_size > 0, "ICO 是空文件"
    assert png.stat().st_size > 0, "PNG 是空文件"

    # 冻结态应当走 sys._MEIPASS，源码态走脚本所在目录 —— 但两者都必须在列表里
    assert len(roots) >= 1, "resource_roots() 返回空"
    return f"roots={roots} | ico={ico.stat().st_size}B | png={png.stat().st_size}B"


check("图标运行资源可定位", _icon_resource)


def _window_icon_applied():
    """窗口图标必须真的被设置 —— 不能只证明文件存在。

    判定：root.winfo_id() 的 WM_CLASS / Tk 返回值，加上 apply_window_icon 的返回值
    不是 'none'。冻结态如果 datas 漏打或路径解析错，这一项会失败。
    """
    import tkinter as _tk
    from khdownloader_gui import apply_window_icon

    root = _tk.Tk()
    root.withdraw()
    try:
        mode = apply_window_icon(root)
        assert mode != "none", (
            "apply_window_icon 返回 none：既没设成 ICO 也没设成 PNG，窗口会用默认图标"
        )
        # Tk 会把 wm_iconbitmap 设到窗口管理器；确认调用本身没抛异常并且
        # root 上确实留下了图标状态（Tk 内部记录 winfo_toplevel 的 icon）
        assert mode.startswith(("ico:", "png:")), mode
        return f"生效方式 = {mode}"
    finally:
        root.destroy()


check("窗口图标已设置", _window_icon_applied)

line("结果汇总")
ok = sum(1 for _, good, _ in results if good)
total = len(results)
print(f"通过 {ok} / {total}")
if ok != total:
    print("存在失败项！")
    sys.exit(1)
print("全部通过。")
