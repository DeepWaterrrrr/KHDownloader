# -*- coding: utf-8 -*-
"""
test_gui_smoke.py —— 界面测试（tkinter）

运行方式：
    python tests/test_gui_smoke.py

它对着 tests/mock_site.py 这个本地假站点跑，验证界面这一层：
  1. 主窗口能建起来，要求的控件都在；
  2. 点"开始下载"能真的把文件下下来，而且界面全程不卡死；
  3. 所有界面更新都发生在主线程里（工作线程绝不直接改控件）；
  4. 取消按钮能安全停下，且不留下半个文件；
  5. 两条进度条（专辑 / 当前文件）表现正确；
  6. 结束汇总分得清 完成 / 跳过 / 失败 / 取消。

没有图形环境时（例如纯命令行的服务器），它会打印一句说明然后跳过，
而不是报一堆莫名其妙的错。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import khdownloader_gui as gui  # noqa: E402
from mock_site import FAKE_MP3_SIZE, SLOW_FILE_SIZE, MockSite  # noqa: E402


class _FakeMessagebox:
    """替换 tkinter.messagebox。

    真实的 messagebox 会弹出一个"必须点确定"的模态窗口，
    在自动化测试里会直接把程序卡住，所以这里换成只做记录的替身。
    askyesno 的返回值由 answer 属性控制（默认 True = 用户确认）。
    """

    def __init__(self, answer: bool = True, answers=None):
        self.calls: list[tuple[str, str, str]] = []
        self.answer = answer
        #: 依次返回的回答队列；用完后回落到 answer（默认 True = 用户确认）。
        self.answers = list(answers) if answers is not None else None

    def showinfo(self, title, message, **kwargs):
        self.calls.append(("info", str(title), str(message)))

    def showwarning(self, title, message, **kwargs):
        self.calls.append(("warning", str(title), str(message)))

    def showerror(self, title, message, **kwargs):
        self.calls.append(("error", str(title), str(message)))

    def askyesno(self, title, message, **kwargs):
        self.calls.append(("askyesno", str(title), str(message)))
        if self.answers:
            return self.answers.pop(0)
        return self.answer


def _tk_available() -> bool:
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


@unittest.skipUnless(_tk_available(), "当前环境没有图形界面，跳过界面测试")
class TestGuiSmoke(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._site_ctx = MockSite()
        cls.site = cls._site_ctx.__enter__()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.folder = Path(cls._tmp.name)
        # 整个测试类**共用一个 Tk 根窗口**。
        # 每个测试各建一个 Tk() 的话，前一个根窗口的 StringVar 会在之后被
        # 别处的线程回收，触发 Tcl 的 "async handler deleted by the wrong thread"
        # 噪声、甚至直接弄死进程。一个类共用一个根窗口就没这个问题。
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:  # noqa: BLE001
            pass
        cls._tmp.cleanup()
        cls._site_ctx.__exit__(None, None, None)

    def setUp(self):
        # 每个测试用同一根窗口下新建的 App，互不干扰
        self.fake_box = _FakeMessagebox()
        self._real_messagebox = gui.messagebox
        gui.messagebox = self.fake_box

        self.app = gui.App(self.root)

        # ---- 记录"谁在更新界面" ----
        self.gui_thread_ids = set()
        self.main_thread_id = threading.get_ident()

        real_log = self.app.log

        def recording_log(text):
            self.gui_thread_ids.add(threading.get_ident())
            real_log(text)

        self.app.log = recording_log

        # 每个测试用独立的子目录，避免互相看到对方下的文件
        self.case_folder = Path(tempfile.mkdtemp(dir=self.folder))

    def tearDown(self):
        gui.messagebox = self._real_messagebox
        # 收尾：先把两个进度条的不确定动画停掉，再取消挂着的 after 回调。
        try:
            self.app.file_progress.stop()
            self.app.album_progress.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.root.update_idletasks()
            for after_id in self.root.tk.call("after", "info"):
                try:
                    self.root.after_cancel(after_id)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        # 把这次测试建的控件拆掉，免得越堆越多
        for child in self.root.winfo_children():
            try:
                child.destroy()
            except Exception:  # noqa: BLE001
                pass
        # 关键：在主线程里显式把 App 丢掉。
        # tkinter 的 StringVar 在 __del__ 里会去问 Tcl"我还存在吗"，
        # 如果它是在**别的线程**（比如假站点的服务线程）被回收的，
        # Tcl 就会打印 "main thread is not in main loop"。
        # 在拆卸阶段主动释放，就能让它在主线程干干净净地消失。
        self.app = None
        import gc

        gc.collect()

    # ------------------------------------------------------------------
    def _pump_until_idle(self, timeout=40.0):
        """模拟界面主循环：不断处理事件，直到下载结束或超时。

        返回 (心跳次数, 耗时秒数)。心跳次数 > 0 说明事件循环在整个下载过程中
        一直在转 —— 也就是界面没有卡死。
        """
        heartbeat = {"count": 0, "stop": False, "after_id": None}

        def beat():
            if heartbeat["stop"]:
                return
            heartbeat["count"] += 1
            heartbeat["after_id"] = self.root.after(10, beat)

        heartbeat["after_id"] = self.root.after(10, beat)
        started = time.time()
        while self.app.running and time.time() - started < timeout:
            self.root.update()
            time.sleep(0.005)
        self.root.update()
        # 收尾：把还没触发的定时器取消掉，免得窗口销毁后它还在回调
        heartbeat["stop"] = True
        if heartbeat["after_id"] is not None:
            try:
                self.root.after_cancel(heartbeat["after_id"])
            except Exception:  # noqa: BLE001 - 已经销毁了就算了
                pass
        return heartbeat["count"], time.time() - started

    def _pump_for(self, seconds: float):
        """只转界面事件循环这么长时间，不关心下载是否结束（测试取消时用）。"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.root.update()
            time.sleep(0.005)

    # ------------------------------------------------------------------
    # 1. 控件与基本校验
    # ------------------------------------------------------------------
    def test_required_widgets_exist(self):
        self.assertIsInstance(self.app.url_entry, tk.Widget)
        self.assertIsInstance(self.app.folder_entry, tk.Widget)
        self.assertIsInstance(self.app.browse_button, tk.Widget)
        self.assertIsInstance(self.app.start_button, tk.Widget)
        self.assertIsInstance(self.app.cancel_button, tk.Widget)
        self.assertIsInstance(self.app.album_progress, tk.Widget)
        self.assertIsInstance(self.app.file_progress, tk.Widget)
        self.assertIsInstance(self.app.log_box, tk.Widget)
        # 专辑进度条必须是"确定型"（能显示 current/total）
        self.assertEqual(str(self.app.album_progress.cget("mode")), "determinate")
        self.assertIsInstance(self.app.album_progress_var.get(), str)
        self.assertIsInstance(self.app.summary_var.get(), str)

    def test_empty_url_shows_readable_error(self):
        self.app.url_var.set("")
        self.app.start_download()
        self.assertEqual(len(self.fake_box.calls), 1)
        kind, title, message = self.fake_box.calls[0]
        self.assertEqual(kind, "error")
        self.assertIn("地址", message)
        self.assertFalse(self.app.running)

    def test_empty_folder_shows_readable_error(self):
        self.app.url_var.set(self.site.album_url)
        self.app.folder_var.set("")
        self.app.start_download()
        self.assertEqual(len(self.fake_box.calls), 1)
        self.assertIn("保存", self.fake_box.calls[0][2])
        self.assertFalse(self.app.running)

    def test_cancel_button_disabled_while_idle(self):
        self.assertEqual(str(self.app.cancel_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.start_button.cget("state")), "normal")

    # ------------------------------------------------------------------
    # 2. 正常整张下载
    # ------------------------------------------------------------------
    def test_full_download_from_gui(self):
        self.app.url_var.set(self.site.album_url)
        self.app.folder_var.set(str(self.case_folder))
        self.app.start_download()

        self.assertTrue(self.app.running, "点击后应进入运行状态")
        self.assertEqual(str(self.app.cancel_button.cget("state")), "normal",
                         "运行中取消按钮应当可用")

        heartbeats, elapsed = self._pump_until_idle()

        self.assertFalse(self.app.running, "下载结束后应恢复空闲状态")
        self.assertGreater(heartbeats, 0, "下载过程中界面事件循环必须还在转（否则就是卡死了）")

        # 文件确实落在了用户选的目录里
        names = sorted(p.name for p in self.case_folder.iterdir())
        self.assertEqual(names, ["Track One.mp3", "東京 Sky.mp3"])
        for name in names:
            self.assertEqual((self.case_folder / name).stat().st_size, FAKE_MP3_SIZE)
        self.assertFalse(any(n.endswith(".part") for n in names))

        # 汇总：mock 专辑 3 首里，第 3 首故意缺 MP3 链接 -> 完成 2、失败 1
        self.assertIn("完成 2", self.app.summary_var.get())
        self.assertIn("失败 1", self.app.summary_var.get())
        self.assertIn("跳过 0", self.app.summary_var.get())
        self.assertIn("结束", self.app.log_box.get("1.0", "end"))
        self.assertTrue(self.fake_box.calls)

    def test_gui_updates_happen_only_on_main_thread(self):
        self.app.url_var.set(self.site.album_url)
        self.app.folder_var.set(str(self.case_folder))
        self.app.start_download()
        self._pump_until_idle()

        self.assertEqual(
            self.gui_thread_ids,
            {self.main_thread_id},
            "界面更新只能发生在主线程；工作线程不得直接改控件",
        )

    def test_running_disables_start_and_enables_cancel(self):
        self.app.url_var.set(self.site.album_url)
        self.app.folder_var.set(str(self.case_folder))
        self.app.start_download()
        self.assertEqual(str(self.app.start_button.cget("state")), "disabled")
        self._pump_until_idle()
        self.assertEqual(str(self.app.start_button.cget("state")), "normal")
        self.assertEqual(str(self.app.cancel_button.cget("state")), "disabled")

    def test_second_start_while_running_is_ignored(self):
        self.app.url_var.set(self.site.album_url)
        self.app.folder_var.set(str(self.case_folder))
        self.app.start_download()
        self.app.start_download()  # 运行中再点一次，应当直接忽略
        self._pump_until_idle()
        self.assertFalse(self.app.running)

    # ------------------------------------------------------------------
    # 3. 取消
    # ------------------------------------------------------------------
    def test_cancel_before_first_track(self):
        """一开跑就取消：应当安全停下，什么都不下，且不报错。"""
        self.app.url_var.set(self.site.album_url)
        self.app.folder_var.set(str(self.case_folder))
        self.app.start_download()
        self.app.cancel_download()  # 立刻取消
        self._pump_until_idle()

        self.assertFalse(self.app.running)
        self.assertEqual(sorted(p.name for p in self.case_folder.iterdir()), [])
        self.assertIn("取消", self.app.summary_var.get())
        # 取消不该弹"错误"框
        self.assertFalse([c for c in self.fake_box.calls if c[0] == "error"])

    def test_cancel_during_download_removes_part_and_keeps_completed(self):
        """下载到一半取消：.part 被删，已下好的文件保留。"""
        self.app.url_var.set(self.site.slow_album_url)
        self.app.folder_var.set(str(self.case_folder))
        self.app.start_download()

        # 等第一首开始下载（会出现 file_progress），再按取消
        waited = 0.0
        while waited < 10 and self.app.running:
            self.root.update()
            if self.app.file_progress["value"] and self.app.file_progress["value"] > 0:
                break
            time.sleep(0.02)
            waited += 0.02

        self.app.cancel_download()
        self._pump_until_idle(timeout=20)

        self.assertFalse(self.app.running)
        names = sorted(p.name for p in self.case_folder.iterdir())
        self.assertFalse(
            any(n.endswith(".part") for n in names),
            f"取消后不能留下 .part 半成品，实际留下：{names}",
        )
        self.assertFalse(
            any(n.endswith(".mp3") for n in names),
            f"取消后不能留一个'看着像下完'的 mp3，实际留下：{names}",
        )
        self.assertIn("取消", self.app.summary_var.get())

    def test_cancelled_files_stay_intact_when_later_file_is_cancelled(self):
        """已经完整下好的那一首，不会因为后续取消而受影响。"""
        # 先完整下一次（两首），确认都在
        self.app.url_var.set(self.site.album_url)
        self.app.folder_var.set(str(self.case_folder))
        self.app.start_download()
        self._pump_until_idle()
        before = {p.name: p.stat().st_size for p in self.case_folder.iterdir()}
        self.assertEqual(len(before), 2)

        # 再对慢速专辑开一次，下载中途取消（写到另一个子目录，互不影响）
        other = Path(tempfile.mkdtemp(dir=self.folder))
        self.app.url_var.set(self.site.slow_album_url)
        self.app.folder_var.set(str(other))
        self.app.start_download()
        self._pump_for(0.6)
        self.app.cancel_download()
        self._pump_until_idle(timeout=20)

        # 原来那两首字节完全没变
        after = {p.name: p.stat().st_size for p in self.case_folder.iterdir()}
        self.assertEqual(before, after, "已完成文件不该被后续的取消/下载动到")

    # ------------------------------------------------------------------
    # 4. 进度
    # ------------------------------------------------------------------
    def test_file_progress_receives_increasing_counts(self):
        """当前文件进度：回调拿到的字节数必须一路递增。"""
        from khdownloader_core import download_file, make_session

        seen: list[int] = []
        with tempfile.TemporaryDirectory() as tmp:
            download_file(
                make_session(),
                self.site.files_url("/files/slow.mp3"),
                Path(tmp),
                "progress.mp3",
                on_progress=lambda written, total: seen.append(written),
            )
        self.assertGreater(len(seen), 1, "慢速文件应当分多块，进度回调要被调用多次")
        self.assertTrue(
            all(b > a for a, b in zip(seen, seen[1:])),
            f"进度必须单调递增，实际：{seen}",
        )
        self.assertEqual(seen[-1], SLOW_FILE_SIZE)

    def test_missing_content_length_does_not_fabricate_percentage(self):
        """没有 Content-Length 时，界面显示"未知大小"，不编百分比。"""
        from khdownloader_core import download_file, make_session

        seen: list[tuple[int, int]] = []
        with tempfile.TemporaryDirectory() as tmp:
            download_file(
                make_session(),
                self.site.files_url("/files/no-length.mp3"),
                Path(tmp),
                "nolen.mp3",
                on_progress=lambda written, total: seen.append((written, total)),
            )
        self.assertTrue(seen)
        self.assertTrue(
            all(total == 0 for _, total in seen),
            f"没有 Content-Length 时 total 必须是 0，实际：{seen}",
        )

        # 再走一遍界面事件处理，确认文案没说谎
        self.app._handle_event("file_progress", {"written": 4096, "total": 0})
        text = self.app.file_progress_var.get()
        self.assertIn("未提供", text)
        self.assertNotIn("%", text, "不知道总大小时绝不能显示百分比")

    def test_album_progress_reflects_current_over_total(self):
        self.app._handle_event("tracks", {"total": 30})
        self.assertIn("0 / 30", self.app.album_progress_var.get())
        self.assertEqual(self.app.album_progress.cget("maximum"), 30)

        self.app._handle_event("track_start", {"index": 12, "total": 30, "name": "Crisscross"})
        self.assertIn("12 / 30", self.app.album_progress_var.get())
        self.assertIn("Crisscross", self.app.current_song_var.get())

        self.app._handle_event("track_done", {"index": 12, "total": 30, "name": "Crisscross"})
        self.assertEqual(self.app.album_progress["value"], 12)

    # ------------------------------------------------------------------
    # 5. 错误分类
    # ------------------------------------------------------------------
    def test_blocked_site_reports_readable_fatal_error(self):
        self.app.url_var.set(self.site.blocked_url)
        self.app.folder_var.set(str(self.case_folder))
        self.app.start_download()
        self._pump_until_idle(timeout=15)

        self.assertFalse(self.app.running)
        errors = [c for c in self.fake_box.calls if c[0] == "error"]
        self.assertTrue(errors, "应当弹出一个人能看懂的报错")
        self.assertIn("Cloudflare", errors[0][2])
        self.assertIn("Cloudflare", self.app.log_box.get("1.0", "end"))

    def test_no_traceback_leaks_into_gui(self):
        """界面上的文字里不能出现原始 Python 堆栈。"""
        self.app.url_var.set(self.site.blocked_url)
        self.app.folder_var.set(str(self.case_folder))
        self.app.start_download()
        self._pump_until_idle(timeout=15)

        shown = self.app.log_box.get("1.0", "end")
        for box_kind, _title, message in self.fake_box.calls:
            shown += message
        self.assertNotIn("Traceback", shown)
        self.assertNotIn('File "', shown)

    # ------------------------------------------------------------------
    # 6. 汇总
    # ------------------------------------------------------------------
    def test_summary_counts_success_skip_failure(self):
        """汇总要能分清 完成 / 跳过 / 失败。"""
        self.app.url_var.set(self.site.album_url)
        self.app.folder_var.set(str(self.case_folder))
        # 先下一次
        self.app.start_download()
        self._pump_until_idle()
        # 再下一次同样的专辑 —— 文件都在，应当全被"跳过"
        self.fake_box.calls.clear()
        self.app.start_download()
        self._pump_until_idle()

        summary = self.app.summary_var.get()
        self.assertIn("跳过 2", summary)
        self.assertIn("完成 0", summary)

    def test_partial_failure_does_not_claim_all_success(self):
        """有失败时，绝不能显示"全部完成"。"""
        # mock 专辑里第 3 首故意缺 MP3 链接 -> 必然失败一首
        self.app.url_var.set(self.site.album_url)
        self.app.folder_var.set(str(self.case_folder))
        self.app.start_download()
        self._pump_until_idle()

        summary = self.app.summary_var.get()
        self.assertIn("失败 1", summary)
        msgs = " ".join(m for _, _, m in self.fake_box.calls)
        self.assertIn("失败", msgs)
        self.assertNotIn("全部完成", msgs)
        self.assertTrue(
            any(c[0] == "warning" for c in self.fake_box.calls),
            "有失败时应当用警告框，而不是'下载完成'提示",
        )


class _FakeFileDialog:
    """替身：让 askopenfilename 直接返回预设路径，不弹真实文件框。"""

    def __init__(self, path):
        self.path = path

    def askopenfilename(self, **kwargs):
        return self.path


@unittest.skipUnless(_tk_available(), "当前环境没有图形界面，跳过界面测试")
class TestGuiV007(unittest.TestCase):
    """v007 新增恢复入口与状态展示的界面测试。"""

    @classmethod
    def setUpClass(cls):
        cls._site_ctx = MockSite()
        cls.site = cls._site_ctx.__enter__()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.folder = Path(cls._tmp.name)
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:  # noqa: BLE001
            pass
        cls._tmp.cleanup()
        cls._site_ctx.__exit__(None, None, None)

    def setUp(self):
        self.fake_box = _FakeMessagebox()
        self._real_messagebox = gui.messagebox
        gui.messagebox = self.fake_box
        self.app = gui.App(self.root)
        self.case_folder = Path(tempfile.mkdtemp(dir=self.folder))

    def tearDown(self):
        gui.messagebox = self._real_messagebox
        try:
            self.app.file_progress.stop()
        except Exception:  # noqa: BLE001
            pass
        for child in self.root.winfo_children():
            try:
                child.destroy()
            except Exception:  # noqa: BLE001
                pass
        self.app = None
        import gc

        gc.collect()

    def _pump_until_idle(self, timeout=30.0):
        started = time.time()
        while self.app.running and time.time() - started < timeout:
            self.root.update()
            time.sleep(0.005)
        self.root.update()

    def test_recovery_widgets_exist(self):
        self.assertTrue(hasattr(self.app, "import_button"))
        self.assertTrue(hasattr(self.app, "single_button"))
        self.assertEqual(self.app.import_button.cget("text"), "导入专辑页面…")
        self.assertEqual(self.app.single_button.cget("text"), "下载单个链接…")

    def test_import_challenge_page_is_rejected(self):
        """导入一个校验页，必须被拒，且不能当成功。"""
        challenge = self.folder / "challenge.html"
        challenge.write_text(
            "<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
            "<body>challenges.cloudflare.com</body></html>",
            encoding="utf-8",
        )
        real_dialog = gui.filedialog
        gui.filedialog = _FakeFileDialog(str(challenge))
        try:
            self.app.url_var.set(self.site.blocked_url)  # 只为通过 URL 校验
            self.app.folder_var.set(str(self.case_folder))
            self.app.import_album_page()
        finally:
            gui.filedialog = real_dialog
        self.assertFalse(self.app.running)
        self.assertTrue(
            any(c[0] == "warning" for c in self.fake_box.calls),
            "导入校验页应当给出警告",
        )

    def test_import_real_album_html_then_downloads(self):
        """导入真实结构 HTML 后，能走通下载（mock 曲目页/MP3）。"""
        # 用一个"指向 mock 站点"的专辑页 HTML：结构同 album_page.html
        album_html = (Path(__file__).resolve().parent / "fixtures" / "album_page.html").read_text(
            encoding="utf-8"
        )
        # album_page.html 里的相对链接是相对 album_url 的，这里把 URL 设为 mock 专辑地址
        path = self.case_folder / "imported.html"
        path.write_text(album_html, encoding="utf-8")

        real_dialog = gui.filedialog
        gui.filedialog = _FakeFileDialog(str(path))
        try:
            self.app.url_var.set(self.site.album_url)
            self.app.folder_var.set(str(self.case_folder))
            self.app.import_album_page()
            self._pump_until_idle()
        finally:
            gui.filedialog = real_dialog

        self.assertFalse(self.app.running)
        # 至少下下来一个（mock 专辑 2 首可下 + 1 首无链接）
        mp3s = sorted(p.name for p in self.case_folder.glob("*.mp3"))
        self.assertTrue(mp3s, f"导入后应当下载出文件，实际：{mp3s}")

    def test_single_link_download_from_gui(self):
        self.app.url_var.set(self.site.files_url("/files/ok.mp3"))
        self.app.folder_var.set(str(self.case_folder))
        self.app.download_single_link()
        self._pump_until_idle()
        self.assertFalse(self.app.running)
        mp3s = sorted(p.name for p in self.case_folder.glob("*.mp3"))
        self.assertEqual(len(mp3s), 1, f"单曲下载应当只产生 1 个文件，实际：{mp3s}")
        self.assertIn("完成 1", self.app.summary_var.get())

    def test_single_link_rejects_html_content(self):
        self.app.url_var.set(self.site.fake_html_mp3_url)
        self.app.folder_var.set(str(self.case_folder))
        self.app.download_single_link()
        self._pump_until_idle()
        names = sorted(p.name for p in self.case_folder.iterdir())
        self.assertEqual(names, [], f"拒收后不该留下文件，实际：{names}")

    def test_status_shows_fetching_then_ready(self):
        """导入并确认预览后，进入下载状态（v008：确认后立即开始）。"""
        album_html = (Path(__file__).resolve().parent / "fixtures" / "album_page.html").read_text(
            encoding="utf-8"
        )
        path = self.case_folder / "imported2.html"
        path.write_text(album_html, encoding="utf-8")
        real_dialog = gui.filedialog
        gui.filedialog = _FakeFileDialog(str(path))
        try:
            self.app.url_var.set(self.site.album_url)
            self.app.folder_var.set(str(self.case_folder))
            self.app.import_album_page()
            # v008：预览确认（askyesno 默认 True）后立即开始下载；
            # 状态应显示"开始下载（已导入网页）"，且来源标记为已导入网页。
            self.assertIn("开始下载", self.app.status_var.get())
            self.assertIn("已导入网页", self.app.source_var.get())
            self.assertIsNotNone(self.app.confirmed_import)
            self._pump_until_idle()
        finally:
            gui.filedialog = real_dialog


@unittest.skipUnless(_tk_available(), "当前环境没有图形界面，跳过界面测试")
class TestGuiV008(unittest.TestCase):
    """v008：导入工作流 —— 冲突确认 / 预览取消 / 主动开始 / 切模式作废。"""

    # 用 mock 站点的专辑（localhost 主机适配已验证），预览确认默认"是"
    @classmethod
    def setUpClass(cls):
        cls._site_ctx = MockSite()
        cls.site = cls._site_ctx.__enter__()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.folder = Path(cls._tmp.name)
        cls.root = tk.Tk()
        cls.root.withdraw()
        # 合成 2003 夹具（结构同用户原样文件；真实原样文件验证单独标注）
        cls.album_html_2003 = (Path(__file__).resolve().parent / "fixtures"
                               / "album_page_2003_synthetic.html").read_text(encoding="utf-8")
        # 用户**原样保存**的 2003 页面（见 fixtures/SOURCES.md）；GUI 流程用它做真实回归
        cls.user_original_2003 = (Path(__file__).resolve().parent / "fixtures"
                                  / "user_original_2003_album_page.html"
                                  ).read_text(encoding="utf-8-sig", errors="replace")
        cls.URL_2003 = (
            "https://downloads.khinsider.com/game-soundtracks/album/"
            "cross-channel-soundtrack-arrange-version-criss-cross-2003"
        )
        cls.URL_2011 = (
            "https://downloads.khinsider.com/game-soundtracks/album/"
            "cross-channel-in-memory-of-all-people-special-soundtrack-2011"
        )

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:  # noqa: BLE001
            pass
        cls._tmp.cleanup()
        cls._site_ctx.__exit__(None, None, None)

    def setUp(self):
        self.fake_box = _FakeMessagebox()
        self._real_messagebox = gui.messagebox
        gui.messagebox = self.fake_box
        self.app = gui.App(self.root)
        self.case_folder = Path(tempfile.mkdtemp(dir=self.folder))

    def tearDown(self):
        gui.messagebox = self._real_messagebox
        try:
            self.app.file_progress.stop()
        except Exception:  # noqa: BLE001
            pass
        for child in self.root.winfo_children():
            try:
                child.destroy()
            except Exception:  # noqa: BLE001
                pass
        self.app = None
        import gc
        gc.collect()

    def _pump_until_idle(self, timeout=30.0):
        started = time.time()
        while self.app.running and time.time() - started < timeout:
            self.root.update()
            time.sleep(0.005)
        self.root.update()

    def _import_file(self, html_text: str, name="imported.html") -> Path:
        path = self.case_folder / name
        path.write_text(html_text, encoding="utf-8")
        return path

    def test_save_guide_button_exists(self):
        self.assertEqual(self.app.save_guide_button.cget("text"), "保存网页指引")
        self.app.show_save_guide()  # 地址为空：只弹 info 指引，不联网
        self.assertTrue(any(c[0] == "info" for c in self.fake_box.calls))

    def test_import_with_wrong_address_asks_and_switches(self):
        """地址框 2011 + 文件 2003（用 mock slug 模拟）：应询问，确认后用网页地址。"""
        # 构造"地址与文件不同专辑"的场景：地址填 other slug，文件是 mock-album-2020
        html = (Path(__file__).resolve().parent / "fixtures" / "album_page.html").read_text(
            encoding="utf-8")
        path = self._import_file(html)
        other_url = self.site.album_url.rsplit("/", 1)[0] + "/other-album-2019"
        real_dialog = gui.filedialog
        gui.filedialog = _FakeFileDialog(str(path))
        try:
            self.app.url_var.set(other_url)
            self.app.folder_var.set(str(self.case_folder))
            self.app.import_album_page()
            # 第一次 askyesno：是否切换到网页中的专辑地址 → 是
            asks = [c for c in self.fake_box.calls if c[0] == "askyesno"]
            self.assertTrue(asks, "地址不匹配时应当询问用户")
            self.assertIn("不是同一张专辑", asks[0][1] + asks[0][2])
            # 确认后：地址框被改写为网页识别的地址，并进入预览（第二次 askyesno）
            self.assertEqual(self.app.url_var.get(), self.site.album_url)
            self.assertIn("开始下载", self.app.status_var.get())
        finally:
            gui.filedialog = real_dialog

    def test_import_conflict_declined_does_not_download(self):
        """地址不匹配时用户选「否」：不下载、地址框保持原样。"""
        html = (Path(__file__).resolve().parent / "fixtures" / "album_page.html").read_text(
            encoding="utf-8")
        path = self._import_file(html)
        other_url = self.site.album_url.rsplit("/", 1)[0] + "/other-album-2019"
        self.fake_box.answer = False  # 用户拒绝切换
        real_dialog = gui.filedialog
        gui.filedialog = _FakeFileDialog(str(path))
        try:
            self.app.url_var.set(other_url)
            self.app.folder_var.set(str(self.case_folder))
            self.app.import_album_page()
        finally:
            gui.filedialog = real_dialog
        self.assertFalse(self.app.running)
        self.assertEqual(self.app.url_var.get(), other_url)  # 地址保持不变
        self.assertEqual(sorted(p.suffix for p in self.case_folder.glob("*.mp3")), [])
        self.assertIsNone(self.app.confirmed_import)

    def test_preview_declined_does_not_download_or_touch_network(self):
        """预览时点「否」：不创建会话、不请求详情页、不写任何文件。"""
        path = self._import_file(self.album_html_2003.replace(
            "/game-soundtracks/album/cross-channel-soundtrack-arrange-version-criss-cross-2003/",
            f"/game-soundtracks/album/{self.site.album_url.rsplit('/', 1)[-1]}/",
        ))
        self.fake_box.answer = False  # 预览确认 → 否
        real_dialog = gui.filedialog
        gui.filedialog = _FakeFileDialog(str(path))
        try:
            self.app.url_var.set(self.site.album_url)
            self.app.folder_var.set(str(self.case_folder))
            self.app.import_album_page()
        finally:
            gui.filedialog = real_dialog
        self.assertFalse(self.app.running)
        self.assertIsNone(self.app.confirmed_import)
        self.assertEqual(self.app.session, None)  # 没建网络会话
        self.assertEqual(sorted(p.name for p in self.case_folder.glob("*")),
                         ["imported.html"])  # 只有导入的 HTML 本身

    def test_preview_confirmed_uses_preview_tracks(self):
        """预览确认后：下载的是预览曲目；来源显示"已导入网页"。"""
        slug = self.site.album_url.rsplit("/", 1)[-1]
        html = self.album_html_2003.replace(
            "/game-soundtracks/album/cross-channel-soundtrack-arrange-version-criss-cross-2003/",
            f"/game-soundtracks/album/{slug}/",
        )
        path = self._import_file(html)
        real_dialog = gui.filedialog
        gui.filedialog = _FakeFileDialog(str(path))
        try:
            self.app.url_var.set(self.site.album_url)
            self.app.folder_var.set(str(self.case_folder))
            self.app.import_album_page()
            self.assertIn("已导入网页", self.app.source_var.get())
            confirmed = self.app.confirmed_import
            self.assertIsNotNone(confirmed)
            self.assertEqual(len(confirmed["tracks"]), 19)  # 2003 专辑 19 首
            self.assertEqual(confirmed["tracks"][0].name, "Airwaves Arr.ver.")
            self._pump_until_idle()
        finally:
            gui.filedialog = real_dialog
        self.assertFalse(self.app.running)

    def test_switch_to_direct_mode_invalidates_import(self):
        """确认导入后改走"开始下载"：导入任务作废、来源标签复位。"""
        slug = self.site.album_url.rsplit("/", 1)[-1]
        html = self.album_html_2003.replace(
            "/game-soundtracks/album/cross-channel-soundtrack-arrange-version-criss-cross-2003/",
            f"/game-soundtracks/album/{slug}/",
        )
        path = self._import_file(html)
        real_dialog = gui.filedialog
        gui.filedialog = _FakeFileDialog(str(path))
        try:
            self.app.url_var.set(self.site.album_url)
            self.app.folder_var.set(str(self.case_folder))
            self.app.import_album_page()
            self.assertIsNotNone(self.app.confirmed_import)
            # 不跑完下载，直接重置 running 后切换模式
        finally:
            gui.filedialog = real_dialog
        self.app.set_running(False)
        self.app.url_var.set(self.site.album_url)
        self.app.folder_var.set(str(self.case_folder))
        self.app.start_download()
        self.assertIsNone(self.app.confirmed_import)  # 已作废
        self.assertIn("直接请求", self.app.source_var.get())
        self._pump_until_idle()

    # ── 用**用户原样 2003 文件**跑 GUI 流程（Brief §9 第二行，全程不联网）──────

    def test_user_original_2003_wrong_address_conflict_then_decline(self):
        """地址框留着 2011、导入的是原样 2003 文件：必须提示"不是同一张专辑"；
        用户选「否」→ 地址不变、不建会话、不写音频。"""
        path = self._import_file(self.user_original_2003, "user_original_2003.html")
        self.fake_box.answer = False
        real_dialog = gui.filedialog
        gui.filedialog = _FakeFileDialog(str(path))
        try:
            self.app.url_var.set(self.URL_2011)
            self.app.folder_var.set(str(self.case_folder))
            self.app.import_album_page()
        finally:
            gui.filedialog = real_dialog

        asks = [c for c in self.fake_box.calls if c[0] == "askyesno"]
        self.assertTrue(asks, "地址与文件不同专辑时必须询问用户")
        blob = asks[0][1] + asks[0][2]
        self.assertIn("不是同一张专辑", blob)
        self.assertIn(self.URL_2011, blob)   # 展示用户填的地址
        self.assertIn(self.URL_2003, blob)   # 展示网页真正的地址
        self.assertNotIn("没有解析到任何曲目链接", blob)

        self.assertFalse(self.app.running)
        self.assertEqual(self.app.url_var.get(), self.URL_2011)  # 地址保持不变
        self.assertIsNone(self.app.confirmed_import)
        self.assertIsNone(self.app.session)
        self.assertEqual(sorted(p.name for p in self.case_folder.glob("*")),
                         ["user_original_2003.html"])

    def test_user_original_2003_conflict_confirmed_then_preview_declined(self):
        """同一场景，用户确认改用网页地址 → 地址被显式回填为 2003，
        随后出现 19 首预览；预览选「否」→ 不下载、不建会话。"""
        path = self._import_file(self.user_original_2003, "user_original_2003.html")
        # 第一次 askyesno = 冲突确认（是）；第二次 = 预览确认（否，不下载）
        self.fake_box.answers = [True, False]
        real_dialog = gui.filedialog
        gui.filedialog = _FakeFileDialog(str(path))
        try:
            self.app.url_var.set(self.URL_2011)
            self.app.folder_var.set(str(self.case_folder))
            self.app.import_album_page()
        finally:
            gui.filedialog = real_dialog

        asks = [c for c in self.fake_box.calls if c[0] == "askyesno"]
        self.assertEqual(len(asks), 2, "应有：冲突确认 + 曲目预览两次询问")
        preview = asks[1][1] + asks[1][2]
        self.assertIn("19", preview)              # 预览里写明 19 首
        self.assertIn("Airwaves Arr.ver.", preview)   # 首曲
        self.assertIn("_and then_", preview)          # 末曲
        self.assertIn(self.URL_2003, preview)

        self.assertEqual(self.app.url_var.get(), self.URL_2003)  # 已显式回填
        self.assertFalse(self.app.running)
        self.assertIsNone(self.app.confirmed_import)  # 预览取消 → 没有确认对象
        self.assertIsNone(self.app.session)           # 没有网络会话
        self.assertEqual(sorted(p.name for p in self.case_folder.glob("*")),
                         ["user_original_2003.html"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
