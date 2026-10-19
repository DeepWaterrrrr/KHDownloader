# -*- coding: utf-8 -*-
"""
khdownloader_gui.py —— 图形界面（tkinter / ttk）

运行方式：
    python khdownloader_gui.py

这个文件只负责"界面"和"线程调度"，真正的下载逻辑全在 khdownloader_core.py。

界面的两条主线：

  一、不卡死
    网络请求要花好几秒甚至几分钟。如果在界面线程里直接下载，窗口会"卡死"
    （点不动、拖不动）。所以：
      - 下载跑在一个后台工作线程里；
      - 工作线程**不碰任何界面控件**，只往 queue.Queue 里塞消息；
      - 界面线程用 root.after() 每隔一小段时间去 queue 里取消息再更新界面。
    这样既不会卡死，也不会出现"跨线程改控件"这种危险操作。

  二、能取消
    界面上有个"取消"按钮，工作线程会不断来看一眼"用户点了吗"。
    取消是**协作式**的：不是硬杀线程，而是在安全点主动停下 ——
    所以不会留下半个文件，也不会损坏已经下好的东西。
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from khdownloader_core import (
    IMPORT_KIND_ALBUM,
    IMPORT_KIND_CHALLENGE,
    IMPORT_KIND_ERROR,
    IMPORT_KIND_NO_TRACKS,
    IMPORT_KIND_UNRESOLVED,
    IMPORT_KIND_UNSUPPORTED_FORMAT,
    DownloaderError,
    download_album,
    download_single_link,
    inspect_import_html,
    is_song_page_url,
    normalize_album_url,
)

#: 界面每隔多少毫秒去队列里取一次消息。50 ms = 每秒 20 次，人眼感觉是实时的。
POLL_INTERVAL_MS = 50

#: 图标文件名。ico 给 Windows 窗口/任务栏用，png 作为 Tk 兜底与后续维护用。
ICON_ICO_NAME = "KHDownloader.ico"
ICON_PNG_NAME = "KHDownloader.png"

#: 图标在包内的子目录。必须和 PyInstaller spec / build_exe.bat 里的 datas 一致。
ASSET_SUBDIR = "assets"


def resource_roots() -> list[Path]:
    """列出可能存放资源文件的根目录，按优先级从高到低。

    - 源码运行：资源在 ``<项目目录>/assets/``
    - PyInstaller onefile 冻结：资源被解包到 ``sys._MEIPASS/assets/``

    刻意**不**依赖当前工作目录（cwd）——双击 EXE 时 cwd 可能是任意目录，
    也可能恰好是交付目录，用 cwd 找资源是最容易在别人机器上失效的做法。
    """
    roots: list[Path] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))

    try:
        roots.append(Path(__file__).resolve().parent)
    except (OSError, NameError):  # pragma: no cover - 极端嵌入环境
        pass

    seen: set[str] = set()
    unique: list[Path] = []
    for r in roots:
        key = str(r)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def resource_path(filename: str) -> Path | None:
    """在资源根目录里找 ``filename``，找到返回路径，找不到返回 ``None``。

    不抛异常：图标缺失不该让整个程序起不来。
    """
    for root in resource_roots():
        for candidate in (
            root / ASSET_SUBDIR / filename,   # 源码运行 / 冻结运行的主路径
            root / filename,                  # 兼容直接把图标放在根目录的旧布局
        ):
            try:
                if candidate.is_file():
                    return candidate
            except OSError:  # pragma: no cover - 权限异常时按未找到处理
                continue
    return None


def apply_window_icon(root: tk.Tk) -> str:
    """给窗口设置图标，并返回实际生效的方式。

    优先用 ICO（Windows 标题栏 + 任务栏 + 资源管理器都用这一份）；
    失败再退到 PNG（仍能设标题栏图标，但任务栏表现可能不如 ICO）；
    都没有则返回 ``"none"``，**不影响程序其余功能**。

    只在 Tk 主线程调用。
    """
    ico = resource_path(ICON_ICO_NAME)
    if ico is not None:
        try:
            root.iconbitmap(default=str(ico))
            return f"ico:{ico}"
        except tk.TclError:
            pass

    png = resource_path(ICON_PNG_NAME)
    if png is not None:
        try:
            img = tk.PhotoImage(file=str(png))
            root.iconphoto(True, img)
            # 必须留住引用，否则 PhotoImage 会被垃圾回收，图标会突然变回默认
            root._khdownloader_icon_img = img  # type: ignore[attr-defined]
            return f"png:{png}"
        except Exception:
            # PhotoImage 只保证带 Tk 的构建支持 PNG；不支持就当没有图标
            pass

    return "none"


class App:
    """主窗口。"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("KHDownloader —— KHInsider MP3 下载器")
        self.icon_mode = apply_window_icon(root)
        self.root.geometry("780x640")
        self.root.minsize(660, 520)

        # 工作线程 -> 界面线程 的消息队列
        self.events: queue.Queue = queue.Queue()
        # 正在下载时置 True：用来禁用开始按钮、防止重复启动
        self.running = False
        # 取消标志。工作线程会不断来读它；必须用 threading.Event（线程安全）。
        self.cancel_requested = threading.Event()
        # 保存目录在本次会话里一直保留（见 Brief：Preserve the current selected directory）
        self.last_folder = str(Path.home() / "Downloads" / "KHInsider")

        self.session = None  # 真跑起来时才创建，方便界面启动得很快

        # v008：当前导入任务的"已确认状态"。
        # 预览确认后才有值；开始下载/预览都必须用它，不能回头读地址框。
        self.confirmed_import = None   # dict: {album_url, tracks, title, source}

        self._build_widgets()

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _build_widgets(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)

        row = 0

        # ---- 专辑地址 ----
        ttk.Label(outer, text="专辑地址：").grid(row=row, column=0, sticky="w", pady=4)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(outer, textvariable=self.url_var)
        self.url_entry.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        row += 1

        # ---- 保存目录 ----
        ttk.Label(outer, text="保存到：").grid(row=row, column=0, sticky="w", pady=4)
        self.folder_var = tk.StringVar(value=self.last_folder)
        self.folder_entry = ttk.Entry(outer, textvariable=self.folder_var)
        self.folder_entry.grid(row=row, column=1, sticky="ew", pady=4)
        self.browse_button = ttk.Button(outer, text="选择文件夹…", command=self.choose_folder)
        self.browse_button.grid(row=row, column=2, sticky="w", padx=(8, 0), pady=4)
        row += 1

        # ---- 按钮行：开始 / 取消 ----
        button_row = ttk.Frame(outer)
        button_row.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        self.start_button = ttk.Button(button_row, text="开始下载", command=self.start_download)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(
            button_row, text="取消", command=self.cancel_download, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=(8, 0))
        ttk.Button(button_row, text="打开保存文件夹", command=self.open_folder).pack(
            side="left", padx=(16, 0)
        )
        ttk.Button(button_row, text="清空日志", command=self.clear_log).pack(
            side="left", padx=(8, 0)
        )
        row += 1

        # ---- 恢复入口（v007）：站点被拦时的备用路线 ----
        # 说明：这两个入口解决**不同的**问题，界面上要分清楚，不能互相冒充：
        #   · 导入专辑页面  = 恢复“专辑/曲目发现”，拿不到详情页时仍需用下一条。
        #   · 下载单个链接  = 只下载**一个** MP3 链接，不是整张专辑。
        recovery_row = ttk.Frame(outer)
        recovery_row.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(2, 4))
        ttk.Label(recovery_row, text="被拦时的恢复入口：").pack(side="left")
        self.import_button = ttk.Button(
            recovery_row, text="导入专辑页面…", command=self.import_album_page
        )
        self.import_button.pack(side="left", padx=(6, 0))
        self.single_button = ttk.Button(
            recovery_row, text="下载单个链接…", command=self.download_single_link
        )
        self.single_button.pack(side="left", padx=(8, 0))
        ttk.Button(recovery_row, text="复制地址", command=self.copy_url).pack(
            side="left", padx=(8, 0)
        )
        self.browser_button = ttk.Button(
            recovery_row, text="用浏览器打开", command=self.open_in_browser
        )
        self.browser_button.pack(side="left", padx=(8, 0))
        self.save_guide_button = ttk.Button(
            recovery_row, text="保存网页指引", command=self.show_save_guide
        )
        self.save_guide_button.pack(side="left", padx=(8, 0))
        row += 1

        # ---- v008：当前来源状态（直接请求 / 已导入网页）----
        #: "已导入网页"时必须显示出来 —— 此时"开始下载"走的是**导入的曲目列表**，
        #: 不再请求专辑页；用户改地址/重新导入都会清掉这个状态。
        self.source_var = tk.StringVar(value="来源：直接请求（填好地址后点“开始下载”）")
        ttk.Label(outer, textvariable=self.source_var, foreground="#0a5c0a").grid(
            row=row, column=0, columnspan=3, sticky="w"
        )
        row += 1

        # ---- 专辑进度 ----
        ttk.Label(outer, text="专辑进度").grid(row=row, column=0, sticky="w", pady=(10, 0))
        row += 1
        self.album_progress = ttk.Progressbar(outer, mode="determinate", maximum=1, value=0)
        self.album_progress.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(2, 2))
        row += 1
        self.album_progress_var = tk.StringVar(value="曲目 0 / 0")
        ttk.Label(outer, textvariable=self.album_progress_var).grid(
            row=row, column=0, columnspan=3, sticky="w"
        )
        row += 1
        #: 当前正在处理的曲名（对新手来说，"现在在弄哪一首"比进度条数字更重要）
        self.current_song_var = tk.StringVar(value="当前：—")
        ttk.Label(outer, textvariable=self.current_song_var, foreground="#333333").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(2, 0)
        )
        row += 1

        # ---- 当前文件进度 ----
        ttk.Label(outer, text="当前文件").grid(row=row, column=0, sticky="w", pady=(10, 0))
        row += 1
        #: 一开始是 indeterminate（不知道大小），拿到 Content-Length 后切 determinate。
        self.file_progress = ttk.Progressbar(outer, mode="determinate", maximum=100, value=0)
        self.file_progress.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(2, 2))
        row += 1
        self.file_progress_var = tk.StringVar(value="—")
        ttk.Label(outer, textvariable=self.file_progress_var, foreground="#555555").grid(
            row=row, column=0, columnspan=3, sticky="w"
        )
        row += 1

        # ---- 日志区 ----
        ttk.Label(outer, text="状态 / 日志：").grid(row=row, column=0, sticky="nw", pady=(10, 0))
        self.log_box = scrolledtext.ScrolledText(outer, height=14, wrap="word", state="disabled")
        self.log_box.grid(row=row, column=1, columnspan=2, sticky="nsew", pady=(10, 0))
        outer.rowconfigure(row, weight=1)
        row += 1

        # ---- 完成汇总 ----
        self.summary_var = tk.StringVar(value="完成 0 | 跳过 0 | 失败 0 | 取消 0")
        ttk.Label(outer, textvariable=self.summary_var, foreground="#333333").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(10, 0)
        )
        row += 1

        # ---- 底部状态 ----
        self.status_var = tk.StringVar(value="就绪。")
        ttk.Label(outer, textvariable=self.status_var, foreground="#555555").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )

    # ------------------------------------------------------------------
    # 简单的界面动作
    # ------------------------------------------------------------------
    def choose_folder(self):
        """弹出文件夹选择框，并把选中的目录记住。"""
        chosen = filedialog.askdirectory(
            title="选择保存文件夹",
            initialdir=self.folder_var.get() or str(Path.home()),
        )
        if chosen:
            self.folder_var.set(chosen)
            self.last_folder = chosen

    def open_folder(self):
        """用系统文件管理器打开保存目录。"""
        folder = Path(self.folder_var.get().strip() or ".")
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))  # Windows 专用；换系统这里要改
        except Exception as exc:  # noqa: BLE001 - 打开失败不该让程序崩
            messagebox.showwarning("打不开文件夹", str(exc))

    # ------------------------------------------------------------------
    # 恢复入口的辅助动作（复制地址 / 打开浏览器）
    # ------------------------------------------------------------------
    def copy_url(self):
        """把地址框里的地址复制到剪贴板（不联网，方便用户粘到浏览器）。"""
        url = self.url_var.get().strip()
        if not url:
            messagebox.showinfo("没有地址", "地址框是空的，先粘贴一个专辑地址。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self.log(f"已复制地址到剪贴板：{url}")

    def open_in_browser(self):
        """用系统默认浏览器打开地址框里的地址（交给用户去过验证）。

        注意：这只是"帮你打开浏览器"，**不会**自动把浏览器的验证状态带回程序。
        验证通过后，请用"导入专辑页面…"或"下载单个链接…"继续。
        """
        url = self.url_var.get().strip()
        if not url:
            messagebox.showinfo("没有地址", "地址框是空的，先粘贴一个专辑地址。")
            return
        if is_song_page_url(url):
            # 用户填的是某首歌的详情页：提示打开的将不是专辑页
            if not messagebox.askyesno(
                "这是歌曲页地址",
                "地址框里填的是一首歌的详情页，不是专辑页。\n"
                "仍要用浏览器打开它吗？\n\n"
                "（建议改填专辑页地址：…/game-soundtracks/album/<专辑名>）",
            ):
                return
        try:
            webbrowser.open(url)
            self.log(f"已在浏览器打开：{url}")
            self.log("（浏览器里的验证状态不会自动带回程序；验证后请用下方恢复入口继续。）")
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("打不开浏览器", str(exc))

    def show_save_guide(self):
        """v008：获取网页 / 保存指引（轻量流程，整合浏览器打开）。

        分四步讲清楚：打开 → 等曲目列表出现 → 另存为「仅 HTML」→ 回来导入。
        不声称能读取外部浏览器的当前标签页，也不把 requests 请求包装成"保存网页"。
        """
        url = self.url_var.get().strip()
        if not url:
            messagebox.showinfo(
                "保存网页指引",
                "1. 先在地址框粘贴专辑地址（…/game-soundtracks/album/<专辑名>）。\n"
                "2. 点「用浏览器打开」在正常浏览器里打开它。\n"
                "3. 等页面完全加载、**曲目列表真正出现**。\n"
                "4. 按 Ctrl+S 另存网页，保存类型选「网页，仅 HTML」（.html）。\n"
                "5. 回到本程序，点「导入专辑页面…」选择刚保存的文件。",
            )
            return
        if is_song_page_url(url):
            messagebox.showinfo(
                "保存网页指引",
                "地址框里填的是一首歌的详情页。\n"
                "保存网页请先回到**专辑页**：把地址改成 …/game-soundtracks/album/<专辑名> 再打开。\n\n"
                "专辑页上才有完整的曲目列表。",
            )
            return
        messagebox.showinfo(
            "保存网页指引",
            "按这个顺序做：\n\n"
            "1. 点下方「用浏览器打开」，在正常浏览器里打开这个专辑页。\n"
            "2. 等页面完全加载、**曲目列表真正出现**（能看到每首歌的名字）。\n"
            "3. 按键盘 Ctrl+S 保存网页：\n"
            "     · 保存类型务必选「网页，仅 HTML」（.html）——\n"
            "       本版本支持 .html/.htm；\n"
            "     · 「网页，单个文件」（.mht/.mhtml）是归档格式，本版本**不支持**，\n"
            "       改扩展名也没有用，请重新另存。\n"
            "4. 回到本程序，点「导入专辑页面…」选择刚保存的 .html 文件。",
        )
        self.log("已显示保存网页指引（等待曲目列表出现 → Ctrl+S 另存为「仅 HTML」→ 导入）。")

    def _current_folder(self) -> Path:
        """取当前保存目录（不存在就建）。返回 Path 或 None（并弹窗说明）。"""
        folder_text = self.folder_var.get().strip()
        if not folder_text:
            messagebox.showerror("缺少保存目录", "请选择或填写一个保存文件夹。")
            return None
        folder = Path(folder_text)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("保存目录不可用", f"没法创建这个文件夹：\n{exc}")
            return None
        if not folder.is_dir():
            messagebox.showerror("保存目录不可用", f"这不是一个文件夹：\n{folder}")
            return None
        self.last_folder = folder_text
        return folder

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def log(self, text: str):
        """往日志区追加一行（只有界面线程会调用它）。"""
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def set_running(self, running: bool):
        """统一切换"运行中 / 空闲"状态。

        运行中：开始按钮禁用、取消按钮可用、输入框锁住（避免改到一半）。
        """
        self.running = running
        input_state = "disabled" if running else "normal"
        self.start_button.configure(state=input_state)
        self.url_entry.configure(state=input_state)
        self.folder_entry.configure(state=input_state)
        self.browse_button.configure(state=input_state)
        self.import_button.configure(state=input_state)
        self.single_button.configure(state=input_state)
        self.save_guide_button.configure(state=input_state)
        self.browser_button.configure(state=input_state)
        self.cancel_button.configure(state="normal" if running else "disabled")

    def cancel_download(self):
        """用户点了取消：立个旗子，让工作线程自己收尾。"""
        if not self.running:
            return
        self.cancel_requested.set()
        self.cancel_button.configure(state="disabled")
        self.status_var.set("正在取消…（会把没下完的临时文件删掉）")
        self.log("—— 收到取消请求，正在安全停止… ——")

    # ------------------------------------------------------------------
    # 开始下载
    # ------------------------------------------------------------------
    def start_download(self):
        if self.running:
            return

        # ---- 输入校验（校验失败就直接弹窗告诉用户，不启动线程）----
        try:
            album_url = normalize_album_url(self.url_var.get())
        except DownloaderError as exc:
            messagebox.showerror("地址有问题", str(exc))
            return

        # v008：直接请求模式开始时，清除任何已确认的导入任务。
        # "开始下载"= 重新请求专辑页，来源切换必须显式，不能一边显示旧导入一边下新的。
        if self.confirmed_import is not None:
            self.log("已切换到直接请求模式，之前的导入预览已作废。")
            self.confirmed_import = None
            self.source_var.set("来源：直接请求（填好地址后点“开始下载”）")

        folder = self._current_folder()
        if folder is None:
            return

        # ---- 重置界面状态 ----
        self.clear_log()
        self.cancel_requested.clear()
        self.album_progress.configure(maximum=1, value=0)
        self.album_progress_var.set("曲目 0 / 0")
        self.current_song_var.set("当前：—")
        self.file_progress.configure(mode="determinate", maximum=100, value=0)
        self.file_progress_var.set("—")
        self.summary_var.set("完成 0 | 跳过 0 | 失败 0 | 取消 0")
        self.status_var.set("正在获取页面…")   # 状态：正在获取页面
        self.set_running(True)
        self.log(f"开始：{album_url}")
        self.log(f"保存到：{folder}")

        # ---- 起工作线程（daemon=True：主窗口关掉时它不会拖住程序）----
        worker = threading.Thread(target=self._worker, args=(album_url, folder), daemon=True)
        worker.start()
        self.root.after(POLL_INTERVAL_MS, self._pump_events)

    # ------------------------------------------------------------------
    # 恢复入口 1：导入专辑页面 HTML（识别 → 核对 → 预览 → 确认下载）
    # ------------------------------------------------------------------
    def import_album_page(self):
        """让用户选择正常浏览器保存下来的专辑页 HTML。

        v008 流程（Brief §1）：获取与保存网页指引 → 导入检查 → 自动识别专辑
        → 核对来源地址 → 曲目预览 → 用户主动开始下载。

        关键修正：**先识别文件、再核对地址**（识别与核对在 core 里完成）。
        地址框为空也允许导入 —— 只要页面自身能可靠识别出专辑来源。
        """
        if self.running:
            return

        # 地址框允许为空（v008）：空时若文件能识别，识别后回填并请用户确认。
        raw_url = self.url_var.get().strip()
        if raw_url:
            try:
                normalize_album_url(raw_url)
            except DownloaderError as exc:
                messagebox.showerror("地址有问题", str(exc))
                return

        folder = self._current_folder()
        if folder is None:
            return

        path_str = filedialog.askopenfilename(
            title="选择你保存的专辑页面 HTML",
            filetypes=[("网页文件", "*.html *.htm"), ("所有文件", "*.*")],
        )
        if not path_str:
            return

        try:
            html = Path(path_str).read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            messagebox.showerror("读不了这个文件", f"{exc}")
            return

        # ---- 导入检查 + 来源识别 + 地址核对（core 完成）----
        inspection = inspect_import_html(html, raw_url)

        if inspection.kind == IMPORT_KIND_UNSUPPORTED_FORMAT:
            messagebox.showwarning("文件格式不支持", inspection.message)
            self.log("—— 导入失败：MHTML 等归档格式不受支持 ——")
            return
        if inspection.kind == IMPORT_KIND_CHALLENGE:
            messagebox.showwarning("这是人机校验页", inspection.message)
            self.log("—— 导入失败：选中的文件是人机校验页，不是专辑页 ——")
            return
        if inspection.kind == IMPORT_KIND_ERROR:
            messagebox.showwarning("文件不像网页", inspection.message)
            self.log(f"—— 导入失败：{inspection.message.splitlines()[0]} ——")
            return

        # ---- 地址与文件来源的关系处理（Brief §4.2 表格）----
        relation = inspection.address_relation
        detected = inspection.detected_url
        imported_file = path_str

        if relation == "different_album":
            # 地址框是另一张专辑：明确展示双方，让用户选择，不静默替换。
            use_detected = messagebox.askyesno(
                "地址与网页不是同一张专辑",
                f"地址框里填的是：\n  {raw_url}\n\n"
                f"但导入的网页属于：\n  {detected}\n\n"
                f"（{inspection.evidence}）\n\n"
                "要使用**网页中的专辑地址**继续吗？\n"
                "选「否」则取消导入，地址框保持不变。",
            )
            if not use_detected:
                self.log("—— 导入已取消：地址与网页不属于同一张专辑 ——")
                return
            # 用户确认：把网页识别出的地址填入地址框（显式、可见）
            self.url_var.set(detected)
            raw_url = detected
            relation = "match"
            self.log(f"已按你的确认改用网页识别的专辑地址：{detected}")

        elif relation == "song_page":
            # 填的是某首歌的详情页地址
            if detected:
                use_detected = messagebox.askyesno(
                    "这是歌曲页地址，不是专辑页",
                    f"地址框里填的是一首歌的详情页。\n\n"
                    f"导入的网页属于专辑：\n  {detected}\n\n"
                    "要切换到**该专辑地址**继续吗？",
                )
                if not use_detected:
                    self.log("—— 导入已取消：需要先填专辑地址 ——")
                    return
                self.url_var.set(detected)
                raw_url = detected
                relation = "match"
                self.log(f"已按你的确认改用专辑地址：{detected}")
            else:
                messagebox.showwarning("这是歌曲页地址", inspection.message)
                return

        elif relation == "empty":
            if not detected:
                messagebox.showwarning(
                    "无法识别专辑来源",
                    "地址框为空，且无法从网页中识别出专辑来源。\n"
                    "请先在地址框填好专辑地址，再导入。\n\n" + (inspection.evidence or ""),
                )
                return
            # 识别成功：回填并请用户确认（确认框 = 核对来源这一步）
            use_detected = messagebox.askyesno(
                "已识别出专辑",
                f"地址框为空。从网页中识别出专辑地址：\n  {detected}\n\n"
                f"（{inspection.evidence}）\n\n"
                "使用这个地址继续吗？",
            )
            if not use_detected:
                self.log("—— 导入已取消：未确认识别出的专辑地址 ——")
                return
            self.url_var.set(detected)
            raw_url = detected
            relation = "match"
            self.log(f"已按你的确认填入识别出的专辑地址：{detected}")

        elif relation == "unresolved":
            messagebox.showwarning("无法识别专辑来源", inspection.message)
            self.log("—— 导入失败：无法确定网页属于哪个专辑 ——")
            return

        # relation == "match"：地址相符，直接往下走

        # ---- 解析曲目（match 之后才解析；用确认过的地址）----
        if not inspection.tracks:
            # song_page/different_album 分支确认后，core 只给了核对信息，
            # 这里用确认后的地址重新检查一次拿到曲目。
            inspection = inspect_import_html(html, raw_url)
            if inspection.kind != IMPORT_KIND_ALBUM or not inspection.tracks:
                messagebox.showwarning("没有解析到曲目", inspection.message)
                self.log(f"—— 导入失败：{inspection.message.splitlines()[0]} ——")
                return

        # ---- 曲目预览（Brief §5）：不立即下载，等用户主动开始 ----
        confirmed = self._preview_and_confirm(inspection, raw_url, imported_file, folder)
        if confirmed is None:
            self.log("—— 已在预览中取消，未开始下载 ——")
            return

        # ---- 用户确认：进入下载（用预览确认的同一份对象）----
        self._start_confirmed_import(confirmed)

    def _preview_and_confirm(self, inspection, album_url: str, imported_file: str,
                             folder: Path):
        """显示曲目预览对话框。返回确认 dict；取消返回 None。"""
        tracks = inspection.tracks
        title = inspection.page_title or album_url
        lines = [
            f"专辑：{title}",
            f"专辑地址：{album_url}",
            f"曲目数：{len(tracks)}",
            f"保存到：{folder}",
            "",
            "曲目顺序：",
        ]
        for i, t in enumerate(tracks, start=1):
            lines.append(f"  {i:>2}. {t.name}")
        lines += [
            "",
            f"来源文件：{Path(imported_file).name}",
            "（识别依据：" + (inspection.evidence or "地址相符") + "）",
        ]
        if inspection.conflicts:
            lines.append("注意：" + "；".join(inspection.conflicts))

        ok = messagebox.askyesno(
            "导入预览 —— 确认后开始下载",
            "\n".join(lines) + "\n\n确认无误就点「是」开始下载；点「否」返回不下载。",
        )
        if not ok:
            return None
        return {
            "album_url": album_url,
            "tracks": list(tracks),
            "title": title,
            "source_file": imported_file,
        }

    def _start_confirmed_import(self, confirmed: dict):
        """用**预览确认过的**导入对象启动下载线程（不再请求专辑页）。"""
        tracks = confirmed["tracks"]
        album_url = confirmed["album_url"]
        folder = self._current_folder()
        if folder is None:
            return

        # 记录本次已确认的任务（改地址/切模式时会显式作废）
        self.confirmed_import = confirmed

        self.clear_log()
        self.cancel_requested.clear()
        self.album_progress.configure(maximum=max(len(tracks), 1), value=0)
        self.album_progress_var.set(f"曲目 0 / {len(tracks)}")
        self.current_song_var.set("当前：—")
        self.file_progress.configure(mode="determinate", maximum=100, value=0)
        self.file_progress_var.set("—")
        self.summary_var.set("完成 0 | 跳过 0 | 失败 0 | 取消 0")
        self.status_var.set("开始下载（已导入网页）…")
        self.source_var.set(f"来源：已导入网页（{album_url}）")
        self.set_running(True)
        self.log(f"来源：已导入网页 —— {confirmed.get('source_file', '')}")
        self.log(f"专辑地址：{album_url}")
        self.log(f"保存到：{folder}")

        worker = threading.Thread(
            target=self._import_worker,
            args=(tracks, album_url, folder),
            daemon=True,
        )
        worker.start()
        self.root.after(POLL_INTERVAL_MS, self._pump_events)

    def _import_worker(self, tracks, album_url: str, folder: Path):
        """后台线程：基于**已解析好的曲目列表**下载（跳过专辑页请求）。

        复用 `download_album` 的等价流程：这里直接逐首走详情页→MP3 的下载管线。
        仍然严格顺序、可取消、`.part` 安全、已有文件跳过。
        """
        from khdownloader_core import (
            CancelledError,
            TrackResult,
            download_file,
            fetch_html,
            make_session,
            parse_song_page,
            sanitize_filename,
            summarize_results,
        )

        def emit(kind, data):
            self.events.put((kind, data))

        # 恢复入口可能不经过 download_album（它才会懒创建 session），
        # 所以这里自己要保证 session 存在。
        if self.session is None:
            self.session = make_session()

        results = []
        was_cancelled = False
        last_index = 0
        emit("tracks", {"total": len(tracks)})
        emit("log", {"text": f"从导入的页面开始下载，共 {len(tracks)} 首。"})

        for index, track in enumerate(tracks, start=1):
            if self.cancel_requested.is_set():
                was_cancelled = True
                break
            last_index = index
            emit("track_start", {"index": index, "total": len(tracks), "name": track.name})
            try:
                emit("phase", {"index": index, "total": len(tracks), "phase": "resolving"})
                page_html = fetch_html(self.session, track.page_url)
                mp3_url = parse_song_page(page_html, track.page_url)
                filename = sanitize_filename(track.name)
                if not filename.lower().endswith(".mp3"):
                    filename += ".mp3"
                emit("phase", {"index": index, "total": len(tracks), "phase": "downloading"})
                dest, written, skipped = download_file(
                    self.session, mp3_url, folder, filename,
                    on_progress=lambda done, total: emit("file_progress",
                                                         {"written": done, "total": total}),
                    should_cancel=self.cancel_requested.is_set,
                )
                if skipped:
                    msg = f"已存在且大小一致，跳过：{dest.name}"
                    emit("log", {"text": f"  [跳过] {msg}"})
                    results.append(TrackResult(track.name, "skipped", msg, dest))
                else:
                    msg = f"完成（{written / 1024 / 1024:.2f} MB）-> {dest.name}"
                    emit("log", {"text": f"  [完成] {msg}"})
                    results.append(TrackResult(track.name, "completed", msg, dest))
            except CancelledError:
                was_cancelled = True
                results.append(TrackResult(track.name, "cancelled", "已取消"))
                break
            except DownloaderError as exc:
                emit("track_fail", {"index": index, "total": len(tracks),
                                    "name": track.name, "error": str(exc)})
                results.append(TrackResult(track.name, "failed", str(exc)))
            except OSError as exc:
                msg = f"写文件失败：{exc}"
                emit("track_fail", {"index": index, "total": len(tracks),
                                    "name": track.name, "error": msg})
                results.append(TrackResult(track.name, "failed", msg))
            emit("track_done", {"index": index, "total": len(tracks), "name": track.name})

        if was_cancelled:
            for track in tracks[len(results):]:
                results.append(TrackResult(track.name, "cancelled", "未处理（已取消）"))
            emit("cancelled", {"index": last_index, "total": len(tracks)})
        emit("done", {"counts": summarize_results(results), "results": results})

    # ------------------------------------------------------------------
    # 恢复入口 2：下载单个链接（恢复“单个 MP3 下载”，不是整张专辑）
    # ------------------------------------------------------------------
    def download_single_link(self):
        """下载**一个**用户提供的公开 MP3 链接（明确标注为单曲/链接下载）。"""
        if self.running:
            return

        link = self.url_var.get().strip()
        if not link:
            messagebox.showinfo("需要链接", "请把要下载的单个 MP3 链接粘贴到地址框。")
            return
        try:
            normalize_album_url(link)  # 复用同一套校验（http/https + 域名）
        except DownloaderError as exc:
            messagebox.showerror("链接有问题", str(exc))
            return

        # v008：切到单曲模式时清除已确认的导入任务（来源切换必须显式）
        if self.confirmed_import is not None:
            self.log("已切换到单曲下载，之前的导入预览已作废。")
            self.confirmed_import = None
            self.source_var.set("来源：直接请求（填好地址后点“开始下载”）")

        folder = self._current_folder()
        if folder is None:
            return

        self.clear_log()
        self.cancel_requested.clear()
        self.album_progress.configure(maximum=1, value=0)
        self.album_progress_var.set("单曲下载（不是整张专辑）")
        self.current_song_var.set("当前：单曲/链接下载")
        self.file_progress.configure(mode="determinate", maximum=100, value=0)
        self.file_progress_var.set("—")
        self.summary_var.set("完成 0 | 跳过 0 | 失败 0 | 取消 0")
        self.status_var.set("正在下载单个链接…")
        self.set_running(True)
        self.log("提示：这是“单曲/链接下载”，只下载这一个链接，不会下载整张专辑。")
        self.log(f"链接：{link}")
        self.log(f"保存到：{folder}")

        worker = threading.Thread(target=self._single_worker, args=(link, folder), daemon=True)
        worker.start()
        self.root.after(POLL_INTERVAL_MS, self._pump_events)

    def _single_worker(self, link: str, folder: Path):
        """后台线程：下载单个链接，再把结果翻译成界面事件。"""
        try:
            result = download_single_link(
                url=link,
                folder=folder,
                on_event=lambda kind, data: self.events.put((kind, data)),
                session=self.session,
                should_cancel=self.cancel_requested.is_set,
            )
            # 用统一的事件把单曲结果也走一遍"完成汇总"逻辑
            counts = {
                "completed": 1 if result.status == "completed" else 0,
                "skipped": 1 if result.status == "skipped" else 0,
                "failed": 1 if result.status == "failed" else 0,
                "cancelled": 1 if result.status == "cancelled" else 0,
            }
            if result.status == "cancelled":
                self.events.put(("cancelled", {"index": 1, "total": 1}))
            self.events.put(("done", {"counts": counts, "results": [result]}))
        except DownloaderError as exc:
            self.events.put(("fatal", {"text": str(exc), "kind": "single"}))
        except Exception as exc:  # noqa: BLE001
            self.events.put(
                ("fatal", {"text": f"发生了意料之外的错误：{type(exc).__name__}: {exc}",
                           "kind": "unexpected"})
            )

    def _worker(self, album_url: str, folder: Path):
        """后台线程：只干活 + 往队列发消息，绝不碰界面控件。"""
        try:
            download_album(
                album_url=album_url,
                folder=folder,
                on_event=lambda kind, data: self.events.put((kind, data)),
                session=self.session,
                should_cancel=self.cancel_requested.is_set,  # 传函数进去，不是传 Event 对象
            )
        except DownloaderError as exc:
            self.events.put(("fatal", {"text": str(exc), "kind": "download"}))
        except Exception as exc:  # noqa: BLE001 - 兜底，避免线程静默死掉
            self.events.put(
                ("fatal", {"text": f"发生了意料之外的错误：{type(exc).__name__}: {exc}",
                           "kind": "unexpected"})
            )

    # ------------------------------------------------------------------
    # 界面线程：把队列里的消息翻译成控件更新
    # ------------------------------------------------------------------
    def _pump_events(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                self._handle_event(kind, data)
        except queue.Empty:
            pass

        if self.running:
            self.root.after(POLL_INTERVAL_MS, self._pump_events)

    def _handle_event(self, kind: str, data: dict):
        if kind == "log":
            self.log(data["text"])

        elif kind == "tracks":
            total = data["total"]
            self.album_progress.configure(maximum=max(total, 1), value=0)
            self.album_progress_var.set(f"曲目 0 / {total}")

        elif kind == "track_start":
            index, total = data["index"], data["total"]
            self.album_progress_var.set(f"曲目 {index} / {total}")
            self.current_song_var.set(f"当前：{data['name']}")
            self.log(f"[{index}/{total}] {data['name']}")
            # 换了一首，当前文件进度先归零
            self.file_progress.configure(mode="determinate", maximum=100, value=0)
            self.file_progress_var.set("—")

        elif kind == "phase":
            # 让用户知道"现在卡在哪一步"：是在解析曲目页，还是真的在下数据
            if data["phase"] == "resolving":
                self.status_var.set("正在解析曲目页…")
                self.file_progress.configure(mode="indeterminate")
                self.file_progress.start(12)
            else:  # downloading
                self.file_progress.stop()
                self.file_progress.configure(mode="determinate", maximum=100, value=0)
                self.status_var.set("正在下载…")

        elif kind == "file_progress":
            written, total = data["written"], data["total"]
            if total:
                # 知道总大小：显示真实百分比
                self.file_progress.stop()
                self.file_progress.configure(mode="determinate", maximum=100)
                percent = min(100, written * 100 // total)
                self.file_progress["value"] = percent
                self.file_progress_var.set(
                    f"{written / 1024 / 1024:.2f} / {total / 1024 / 1024:.2f} MB（{percent}%）"
                )
            else:
                # 对方没给 Content-Length：诚实地显示"未知大小"，绝不编百分比
                self.file_progress.stop()
                self.file_progress.configure(mode="indeterminate")
                self.file_progress.start(12)
                self.file_progress_var.set(
                    f"已下载 {written / 1024 / 1024:.2f} MB（对方未提供文件大小）"
                )

        elif kind == "track_done":
            self.album_progress["value"] = data["index"]
            self.file_progress.stop()
            self.file_progress.configure(mode="determinate", value=100)

        elif kind == "track_fail":
            first_line = data["error"].splitlines()[0]
            self.log(f"  [失败] {first_line}")
            self.status_var.set(f"第 {data['index']} 首失败，继续下一首…")

        elif kind == "cancelled":
            self.file_progress.stop()
            self.status_var.set("已取消。")
            self.log("—— 已取消 ——")

        elif kind == "done":
            self._finish(data)

        elif kind == "fatal":
            self._fatal(data)

        elif kind == "single_fail":
            first_line = data["error"].splitlines()[0]
            self.log(f"  [失败] {first_line}")

        elif kind == "single_cancelled":
            self.file_progress.stop()
            self.status_var.set("已取消。")
            self.log("—— 已取消（单曲下载）——")

        elif kind == "single_done":
            # 单曲下载的结果会再走一次 "done" 事件做汇总，这里只记一行
            self.log(f"  单曲结果：{data.get('status')} -> {data.get('path')}")

    def _finish(self, data: dict):
        """收尾：切回空闲、写汇总、按情况弹窗。"""
        self.file_progress.stop()
        self.set_running(False)
        # 任务结束：导入任务已消费完毕，回到默认来源提示
        self.confirmed_import = None
        self.source_var.set("来源：直接请求（填好地址后点“开始下载”）")
        counts = data["counts"]
        completed = counts.get("completed", 0)
        skipped = counts.get("skipped", 0)
        failed = counts.get("failed", 0)
        cancelled = counts.get("cancelled", 0)

        self.summary_var.set(
            f"完成 {completed} | 跳过 {skipped} | 失败 {failed} | 取消 {cancelled}"
        )

        total_done = completed + skipped
        self.album_progress_var.set(f"曲目 {total_done} / {total_done + failed + cancelled}")

        # 汇总要说人话，而且**不能在有失败/取消时还说"全部完成"**
        parts = [f"完成 {completed} 首"]
        if skipped:
            parts.append(f"跳过 {skipped} 首")
        if failed:
            parts.append(f"失败 {failed} 首")
        if cancelled:
            parts.append(f"取消 {cancelled} 首")
        summary_text = "，".join(parts) + "。"
        self.log(f"—— 结束：{summary_text} ——")
        self.status_var.set(summary_text)

        if failed:
            messagebox.showwarning(
                "下载结束（有失败）",
                f"{summary_text}\n\n有 {failed} 首没下成功，详情请看日志区。",
            )
        elif cancelled:
            messagebox.showinfo("已取消", f"{summary_text}\n已经下好的文件都保留着。")
        else:
            messagebox.showinfo("下载完成", f"{summary_text}")

    def _fatal(self, data: dict):
        """整张专辑都没能开始（例如地址打不开、被 Cloudflare 拦、被限流）。

        v007：按错误类型给出**不同的**下一步操作，并且**保留 URL 与保存目录**
        （它们一直在输入框里，不清空），让用户可以直接用恢复入口继续。
        """
        self.file_progress.stop()
        self.set_running(False)
        # 任务中止：导入任务作废（用户重试时需要重新导入或直接请求）
        self.confirmed_import = None
        self.source_var.set("来源：直接请求（填好地址后点“开始下载”）")
        self.status_var.set("出错了，任务已停止。")
        self.log("—— 任务中止 ——")
        self.log(data["text"])

        text = data["text"]
        # 根据错误关键字给出针对性的下一步（不改文案来源，只做分类提示）
        if "人机校验" in text:
            # v1.0.0：文案变了。以前只能让用户手工去浏览器里打开再导入 HTML，
            # 现在程序自己会先换浏览器指纹、再借本机浏览器，手工路径降级成
            # "自动那条路也走不通"时的最后手段 —— 不能再说成第一步。
            self.status_var.set("被站点人机校验拦住。")
            messagebox.showerror(
                "被站点人机校验拦住",
                text + "\n\n可以这样做（地址和保存目录都还留着，不用重填）：\n"
                "  1. 先确认本机装了 Chrome 或 Edge（自动兜底要借它来渲染页面）；\n"
                "  2. 点“重试”，程序会重新走一遍换指纹 → 借浏览器这两步；\n"
                "  3. 仍然不行，就点“用浏览器打开”，在正常浏览器里通过验证后，\n"
                "     用“导入专辑页面…”载入该页 HTML；\n"
                "  4. 若只是详情页打不开，用“下载单个链接…”直接下载你拿到的 MP3 链接。",
            )
        elif "429" in text or "限流" in text:
            messagebox.showwarning("被限流了", text + "\n\n稍等一会儿再点“开始下载”。")
        elif "403" in text:
            messagebox.showerror("访问被拒绝", text + "\n\n可以先用“用浏览器打开”核对地址。")
        else:
            messagebox.showerror("出错了", text)


def _run_transport_selftest(args: list[str]) -> int:
    """（v1.0.0）打包后的自检入口：不弹窗，只写报告文件，用退出码表达成败。

    为什么需要它：EXE 是 console=False 的，没有 stdout；而"防火墙问题到底修好没有"
    取决于**打进包里的 curl_cffi 能不能加载**（能不能真的换指纹）。
    如果这个依赖在打包时漏了，程序会静默退回普通请求、又被 Cloudflare 拦住，
    但界面上只会看到"被拦住"，没人知道是代码问题还是打包问题。
    所以给 EXE 一个可以按退出码判断的入口：

        KHDownloader.exe --selftest-transport [--selftest-out 报告路径] [--selftest-url 地址]

    退出码 0 = 换指纹这一层在冻包里能用；1 = 不能用（报告里写明卡在哪一层）。
    """
    report_path = None
    target = "https://downloads.khinsider.com/game-soundtracks/album/" "cross-channel-in-memory-of-all-people-special-soundtrack-2011"
    i = 0
    while i < len(args):
        item = args[i]
        if item == "--selftest-out" and i + 1 < len(args):
            report_path = args[i + 1]
            i += 2
            continue
        if item == "--selftest-url" and i + 1 < len(args):
            target = args[i + 1]
            i += 2
            continue
        i += 1
    if report_path is None:
        report_path = str(Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent / "selftest_transport.txt")

    lines: list[str] = []
    ok = False
    logs: list[str] = []
    try:
        import khdownloader_transport as transport
        import khdownloader_core as core

        lines.append(f"frozen        : {bool(getattr(sys, 'frozen', False))}")
        lines.append(f"executable    : {sys.executable}")
        lines.append("--- transport.describe() ---")
        lines.append(str(transport.describe()))
        lines.append("--- 实际请求 ---")
        lines.append(f"url           : {target}")
        # 注意：这里刻意用 khdownloader_core.make_session / fetch_html ——
        # 也就是主流程真正走的那两个入口，而不是直接调 curl_cffi。
        # 自检要证明的是"程序这条路能通"，不是"库里有个函数"。
        # fetch_html 只在拿到 200 且过了校验时才返回 HTML，
        # 否则抛 SiteBlockedError / RateLimitError / …，所以走到下一行就等于 200。
        session = core.make_session(on_log=logs.append)
        html = core.fetch_html(session, target, on_log=logs.append)
        challenge = core._looks_like_challenge_html(html, None)
        lines.append("status        : 200（fetch_html 只在 200 且过了校验时才返回）")
        lines.append(f"chars         : {len(html)}")
        lines.append(f"challenge     : {challenge}")
        try:
            tracks = core.parse_album_page(html, target)  # 主流程用的曲目解析
        except Exception as exc:  # pragma: no cover - 仅在页面结构变化时
            tracks = []
            lines.append(f"parse error   : {type(exc).__name__}: {exc}")
        lines.append(f"tracks        : {len(tracks)}")
        if tracks:
            lines.append(f"first track   : {tracks[0].name}")
        lines.append("--- 升级过程（on_log）---")
        if logs:
            lines.extend(f"  · {line}" for line in logs)
        else:
            lines.append("  （没有升级动作：第一层就成功了）")
        ok = (not challenge) and len(tracks) > 0
    except Exception as exc:
        import traceback

        lines.append(f"EXCEPTION     : {type(exc).__name__}: {exc}")
        lines.append(traceback.format_exc())
    lines.append(f"RESULT        : {'PASS' if ok else 'FAIL'}")
    text = "\n".join(lines) + "\n"
    try:
        report = Path(report_path)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(text, encoding="utf-8")
    except Exception as exc:  # pragma: no cover - 报告路径不可写时不影响退出码
        text += f"（报告写不进去 {report_path}：{type(exc).__name__}: {exc}）\n"
    try:
        if not getattr(sys, "frozen", False):
            print(text)
    except Exception:  # pragma: no cover
        pass
    return 0 if ok else 1


def main():
    if "--selftest-transport" in sys.argv[1:]:
        raise SystemExit(_run_transport_selftest(sys.argv[2:]))
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
