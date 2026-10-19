# -*- coding: utf-8 -*-
"""
test_core.py —— khdownloader_core 的自动化测试

运行方式：
    python tests/test_core.py

它不碰真实网站，全部跑在 tests/mock_site.py 这个本地假站点上，
所以没网、或者真实站点拒绝访问时，照样能验证逻辑对不对。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

# 让脚本能被直接运行（python tests/test_core.py）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # noqa: E402

import khdownloader_core  # noqa: E402
import khdownloader_transport as transport  # noqa: E402
from khdownloader_core import (  # noqa: E402
    CancelledError,
    DownloaderError,
    NetworkError,
    NoMp3LinkError,
    NoTracksFoundError,
    SiteBlockedError,
    album_path,
    download_album,
    download_file,
    fetch_html,
    make_session,
    normalize_album_url,
    parse_album_page,
    parse_song_page,
    resolve_destination,
    sanitize_filename,
    summarize_results,
)
from mock_site import (  # noqa: E402
    ALBUM_SLUG,
    FAKE_MP3_SIZE,
    NO_LENGTH_FILE_SIZE,
    SLOW_FILE_SIZE,
    MockSite,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestInputHandling(unittest.TestCase):
    """输入校验与地址处理。"""

    def test_empty_url_rejected(self):
        with self.assertRaises(DownloaderError):
            normalize_album_url("   ")

    def test_url_without_scheme_rejected(self):
        with self.assertRaises(DownloaderError):
            normalize_album_url("downloads.khinsider.com/game-soundtracks/album/x")

    def test_valid_url_kept(self):
        url = "https://downloads.khinsider.com/game-soundtracks/album/demo"
        self.assertEqual(normalize_album_url("  " + url + "  "), url)

    def test_album_path(self):
        url = "https://downloads.khinsider.com/game-soundtracks/album/demo/"
        self.assertEqual(album_path(url), "/game-soundtracks/album/demo")


class TestFilenameSanitizing(unittest.TestCase):
    """文件名净化规则。"""

    def test_windows_forbidden_chars_replaced(self):
        self.assertEqual(sanitize_filename('Bad: Name?'), "Bad_ Name_")

    def test_path_separators_do_not_escape_folder(self):
        name = sanitize_filename("../../etc/passwd")
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)

    def test_trailing_dot_and_space_stripped(self):
        self.assertEqual(sanitize_filename("Track One. "), "Track One")

    def test_reserved_device_name_escaped(self):
        self.assertTrue(sanitize_filename("CON").startswith("_"))

    def test_long_name_truncated(self):
        self.assertLessEqual(len(sanitize_filename("x" * 400)), 120)

    def test_japanese_name_untouched(self):
        self.assertEqual(sanitize_filename("東京 Sky"), "東京 Sky")

    def test_percent_encoding_decoded(self):
        # 曲名里常见的 %E5%8C%97 之类要先解码成真正的中文
        self.assertEqual(sanitize_filename("%E5%8C%97%E6%96%97"), "北斗")

    def test_empty_name_falls_back(self):
        self.assertEqual(sanitize_filename("   "), "untitled")


class TestCollisionRule(unittest.TestCase):
    """同名文件处理规则：新文件 / 大小一致就跳过 / 大小不同就加序号。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_new_file_used_directly(self):
        path, skip = resolve_destination(self.folder, "a.mp3", 100)
        self.assertEqual(path.name, "a.mp3")
        self.assertFalse(skip)

    def test_same_size_skipped(self):
        (self.folder / "a.mp3").write_bytes(b"x" * 100)
        path, skip = resolve_destination(self.folder, "a.mp3", 100)
        self.assertEqual(path.name, "a.mp3")
        self.assertTrue(skip)

    def test_different_size_gets_suffix(self):
        (self.folder / "a.mp3").write_bytes(b"x" * 50)
        path, skip = resolve_destination(self.folder, "a.mp3", 100)
        self.assertEqual(path.name, "a (2).mp3")
        self.assertFalse(skip)

    def test_third_collision_gets_three(self):
        (self.folder / "a.mp3").write_bytes(b"x" * 50)
        (self.folder / "a (2).mp3").write_bytes(b"x" * 50)
        path, _ = resolve_destination(self.folder, "a.mp3", 100)
        self.assertEqual(path.name, "a (3).mp3")


class TestAlbumParsing(unittest.TestCase):
    """专辑页解析：顺序、去重、范围限制。"""

    #: 要和 tests/fixtures/album_page.html 里的链接前缀一致
    ALBUM_URL = "https://downloads.khinsider.com/game-soundtracks/album/mock-album-2020"

    def test_track_list_order_and_names(self):
        tracks = parse_album_page(_fixture("album_page.html"), self.ALBUM_URL)
        self.assertEqual(len(tracks), 3)
        self.assertEqual([t.name for t in tracks], ["Track One", "東京 Sky", "Bad: Name?"])

    def test_duplicate_links_deduplicated(self):
        # 夹具里每首曲目出现了 3 次（时长/MP3/FLAC 三列），应当只留 1 条
        tracks = parse_album_page(_fixture("album_page.html"), self.ALBUM_URL)
        self.assertEqual(len({t.page_url for t in tracks}), 3)

    def test_urls_are_absolute_and_encoded(self):
        tracks = parse_album_page(_fixture("album_page.html"), self.ALBUM_URL)
        for track in tracks:
            self.assertTrue(track.page_url.startswith("https://"))
        self.assertIn("%2520Track%2520One.mp3", tracks[0].page_url)

    def test_scope_excludes_other_albums_and_nav(self):
        tracks = parse_album_page(_fixture("album_page.html"), self.ALBUM_URL)
        joined = " ".join(t.page_url for t in tracks)
        self.assertNotIn("other-album-2019", joined)  # 相关专辑
        self.assertNotIn("forums", joined)  # 论坛
        self.assertNotIn("browse", joined)  # 字母导航
        self.assertNotIn("change_log", joined)  # 变更日志

    def test_fallback_when_songlist_table_missing(self):
        url = "https://downloads.khinsider.com/game-soundtracks/album/mock-album-notable"
        tracks = parse_album_page(_fixture("album_page_without_songlist.html"), url)
        self.assertEqual([t.name for t in tracks], ["Alpha", "Beta"])

    def test_no_tracks_raises_readable_error(self):
        with self.assertRaises(NoTracksFoundError):
            parse_album_page("<html><body><p>nothing here</p></body></html>", self.ALBUM_URL)


class TestSongPageParsing(unittest.TestCase):
    """曲目页解析：找出真正的 MP3 地址。"""

    #: 和夹具里的曲目页地址保持一致，文件名匹配这条判据才有意义
    PAGE_URL = (
        "https://downloads.khinsider.com/game-soundtracks/album/mock-album-2020"
        "/01%2520Track%2520One.mp3"
    )

    def test_cross_host_link_preferred(self):
        html = _fixture("song_page.html").replace(
            "{{MP3_URL}}",
            "https://files.example.test/soundtracks/mock-album-2020/h4sh/01%20Track%20One.mp3",
        )
        mp3 = parse_song_page(html, self.PAGE_URL)
        self.assertEqual(
            mp3, "https://files.example.test/soundtracks/mock-album-2020/h4sh/01%20Track%20One.mp3"
        )

    def test_filename_match_beats_same_host_decoy(self):
        """页面里若混入同主机的无关 .mp3 链接，文件名一致的那个要胜出。"""
        html = _fixture("song_page.html").replace(
            "{{MP3_URL}}", "https://files.example.test/x/01 Track One.mp3"
        )
        self.assertTrue(parse_song_page(html, self.PAGE_URL).startswith("https://files.example.test/"))

    def test_relative_mp3_link_becomes_absolute(self):
        # 用一个只有唯一候选链接的页面，单独验证"相对地址 -> 绝对地址"
        html = '<html><body><a href="/files/relative.mp3">download</a></body></html>'
        mp3 = parse_song_page(html, self.PAGE_URL)
        self.assertEqual(mp3, "https://downloads.khinsider.com/files/relative.mp3")

    def test_page_without_mp3_link_raises(self):
        page_url = "https://downloads.khinsider.com/game-soundtracks/album/mock-album-2020/03%2520X.mp3"
        with self.assertRaises(NoMp3LinkError):
            parse_song_page(_fixture("song_page_no_mp3.html"), page_url)

    def test_audio_tag_is_used_as_fallback(self):
        page_url = "https://downloads.khinsider.com/game-soundtracks/album/mock-album-2020/09%2520Y.mp3"
        html = '<html><body><audio src="/media/y.mp3"></audio></body></html>'
        self.assertEqual(
            parse_song_page(html, page_url), "https://downloads.khinsider.com/media/y.mp3"
        )


class TestNetworkedFlow(unittest.TestCase):
    """对着本地假站点跑：请求、校验、下载、失败处理。"""

    @classmethod
    def setUpClass(cls):
        cls._site_ctx = MockSite()
        cls.site = cls._site_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._site_ctx.__exit__(None, None, None)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.session = make_session()

    def tearDown(self):
        self.session.close()
        self._tmp.cleanup()

    # ---- 失败路径：人机校验页要翻译成人话错误 ----
    def test_challenge_page_raises_readable_error(self):
        with self.assertRaises(SiteBlockedError) as ctx:
            fetch_html(self.session, self.site.blocked_url)
        self.assertIn("Cloudflare", str(ctx.exception))
        self.assertIn("403", str(ctx.exception))

    def test_missing_page_raises_readable_error(self):
        with self.assertRaises(DownloaderError):
            fetch_html(self.session, self.site.url("/definitely-not-here"))

    # ---- 单文件下载 ----
    def test_download_writes_complete_file_and_no_part_left(self):
        seen_progress = []
        dest, written, skipped = download_file(
            self.session,
            self.site.files_url("/files/ok.mp3"),
            self.folder,
            "ok.mp3",
            on_progress=lambda done, total: seen_progress.append((done, total)),
        )
        self.assertFalse(skipped)
        self.assertEqual(written, FAKE_MP3_SIZE)
        self.assertEqual(dest.stat().st_size, FAKE_MP3_SIZE)
        self.assertFalse(Path(str(dest) + ".part").exists())
        self.assertTrue(seen_progress, "应当收到至少一次进度回调")
        self.assertEqual(seen_progress[-1][0], FAKE_MP3_SIZE)

    def test_truncated_download_is_rejected(self):
        with self.assertRaises(DownloaderError):
            download_file(
                self.session,
                self.site.files_url("/files/truncated.mp3"),
                self.folder,
                "truncated.mp3",
            )
        # 半成品既不能留下 .part，更不能顶着 .mp3 的名字躺着
        self.assertFalse((self.folder / "truncated.mp3").exists())
        self.assertFalse((self.folder / "truncated.mp3.part").exists())

    def test_http_404_download_raises(self):
        with self.assertRaises(DownloaderError):
            download_file(
                self.session,
                self.site.files_url("/files/missing.mp3"),
                self.folder,
                "missing.mp3",
            )
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_existing_same_size_file_is_skipped(self):
        target = self.folder / "ok.mp3"
        target.write_bytes(b"z" * FAKE_MP3_SIZE)
        _, written, skipped = download_file(
            self.session, self.site.files_url("/files/ok.mp3"), self.folder, "ok.mp3"
        )
        self.assertTrue(skipped)
        self.assertEqual(written, 0)
        self.assertEqual(target.read_bytes(), b"z" * FAKE_MP3_SIZE)  # 原文件没被动

    # ---- 整张专辑 ----
    def test_full_album_download(self):
        events = []
        results = download_album(
            album_url=self.site.album_url,
            folder=self.folder,
            on_event=lambda kind, data: events.append((kind, data)),
            session=self.session,
            pause=0,  # 测试里不用等，快点跑完
        )

        kinds = [k for k, _ in events]
        self.assertIn("tracks", kinds)
        self.assertIn("done", kinds)
        self.assertEqual(kinds[-1], "done")

        ok = [r for r in results if r.ok]
        failed = [r for r in results if not r.ok]
        self.assertEqual(len(results), 3)
        self.assertEqual(len(ok), 2)
        self.assertEqual([r.name for r in failed], ["Bad: Name?"], "缺 MP3 链接的曲目应单独失败")
        self.assertTrue(all(r.status == "completed" for r in ok))
        self.assertTrue(all(r.status == "failed" for r in failed))

        # 落盘检查：成功的那两首文件名已净化，失败的那首不留任何东西
        names = sorted(p.name for p in self.folder.iterdir())
        self.assertEqual(names, ["Track One.mp3", "東京 Sky.mp3"])
        self.assertFalse((self.folder / "Bad_ Name_.mp3").exists())
        self.assertFalse(any(n.endswith(".part") for n in names), "不该留下 .part 半成品")

        for name in names:
            self.assertEqual((self.folder / name).stat().st_size, FAKE_MP3_SIZE)

        done = dict(events)["done"]
        self.assertEqual(done["counts"],
                         {"completed": 2, "skipped": 0, "failed": 1, "cancelled": 0})

    def test_album_via_fallback_layout(self):
        results = download_album(
            album_url=self.site.album_no_table_url,
            folder=self.folder,
            on_event=lambda kind, data: None,
            session=self.session,
            pause=0,
        )
        # 这个夹具页里的两条曲目指向不存在的详情页，应当全部"可读地失败"而不是崩掉
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not r.ok for r in results))
        self.assertTrue(all("HTTP 404" in r.message for r in results))


class TestCancellationCore(unittest.TestCase):
    """取消行为（核心层）：不接界面，直接问"核心会不会干净地停下来"。

    界面上那个取消按钮，最终就是把 should_cancel 变成 True。
    这里要锁住三件事：
      · 取消后不再开新的曲目；
      · 没下完的 .part 被删掉，绝不留下顶着 .mp3 名字的半成品；
      · 已经下好的 .mp3 原封不动。
    """

    @classmethod
    def setUpClass(cls):
        cls._site_ctx = MockSite()
        cls.site = cls._site_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._site_ctx.__exit__(None, None, None)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.session = make_session()

    def tearDown(self):
        self.session.close()
        self._tmp.cleanup()

    def test_cancel_before_first_track_downloads_nothing(self):
        """还没开始就取消：目录里不该出现任何文件。"""
        results = download_album(
            album_url=self.site.album_url,
            folder=self.folder,
            on_event=lambda kind, data: None,
            session=self.session,
            pause=0,
            should_cancel=lambda: True,  # 一上来就"已取消"
        )
        self.assertEqual(list(self.folder.iterdir()), [], "取消后不该下载任何文件")
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.status == "cancelled" for r in results))

    def test_cancel_during_album_marks_remaining_cancelled(self):
        """取消后，未处理的曲目也要记为 cancelled，总数才对得上。"""
        results = download_album(
            album_url=self.site.album_url,
            folder=self.folder,
            on_event=lambda kind, data: None,
            session=self.session,
            pause=0,
            should_cancel=lambda: True,
        )
        counts = summarize_results(results)
        # 一首都没下，三首全部算"取消"，不该混进 "失败"
        self.assertEqual(counts["completed"], 0)
        self.assertEqual(counts["failed"], 0)
        self.assertEqual(counts["cancelled"], 3)
        self.assertEqual(len(results), 3)

    def test_cancel_between_tracks_keeps_finished_files(self):
        """下载完第一首之后再取消：第一首的 .mp3 必须完好保留。

        做法：用事件流来判断"第一首下完了"。一看到第一首的 track_done，
        就把取消标志竖起来 —— 这样取消一定发生在两首曲目**之间**，
        而不是第一首下载到一半时。
        """
        flag = {"cancel": False}

        def on_event(kind, data):
            if kind == "track_done":
                flag["cancel"] = True  # 第一首刚下完，从现在起宣布取消

        results = download_album(
            album_url=self.site.album_url,
            folder=self.folder,
            on_event=on_event,
            session=self.session,
            pause=0,
            should_cancel=lambda: flag["cancel"],
        )
        names = sorted(p.name for p in self.folder.iterdir())
        # 第一首（Track One）下好了；取消发生在它之后，不该再有别的文件
        self.assertEqual(names, ["Track One.mp3"])
        self.assertEqual((self.folder / "Track One.mp3").stat().st_size, FAKE_MP3_SIZE)
        counts = summarize_results(results)
        self.assertEqual(counts["completed"], 1)
        self.assertGreaterEqual(counts["cancelled"], 1)
        self.assertEqual(counts["failed"], 0)

    def test_cancel_mid_stream_removes_part_and_no_fake_mp3(self):
        """在"正在下载"的过程中取消：.part 要被删掉，也不能有假 .mp3。

        做法：让文件发得很慢，进度回调第一次触发时就宣布"取消"。
        这时数据已经写了一部分（.part 已存在），取消必须把它清掉。
        """
        state = {"fired": False}

        def cancel_on_first_chunk() -> bool:
            if state["fired"]:
                return True
            state["fired"] = True
            return False  # 第一次问：先放行，让至少写下一块

        with self.assertRaises(CancelledError):
            download_file(
                self.session,
                self.site.files_url("/files/slow.mp3"),
                self.folder,
                "slow.mp3",
                on_progress=lambda done, total: cancel_on_first_chunk(),
                should_cancel=lambda: state["fired"],
            )
        # 取消后：目录里既没有 .part 半成品，也没有冒名顶替的 .mp3
        leftovers = sorted(p.name for p in self.folder.iterdir())
        self.assertEqual(leftovers, [], f"取消后不该留文件，实际留下：{leftovers}")

    def test_completed_file_survives_later_cancellation(self):
        """先正常下好一首，再取消后续下载 —— 已完成的文件不受影响。"""
        # 先完整下一首
        dest, written, skipped = download_file(
            self.session,
            self.site.files_url("/files/ok.mp3"),
            self.folder,
            "keep.mp3",
        )
        self.assertEqual(written, FAKE_MP3_SIZE)
        self.assertEqual(dest.read_bytes()[:3], b"ID3")

        # 再对着慢速端点发起一次"中途取消"
        state = {"fired": False}

        def flip() -> bool:
            state["fired"] = True
            return False

        with self.assertRaises(CancelledError):
            download_file(
                self.session,
                self.site.files_url("/files/slow.mp3"),
                self.folder,
                "slow.mp3",
                on_progress=lambda done, total: flip(),
                should_cancel=lambda: state["fired"],
            )

        # 之前下好的 keep.mp3 必须原样健在，字节数分毫不差
        self.assertTrue((self.folder / "keep.mp3").exists())
        self.assertEqual((self.folder / "keep.mp3").stat().st_size, FAKE_MP3_SIZE)
        self.assertFalse((self.folder / "slow.mp3").exists())
        self.assertFalse((self.folder / "slow.mp3.part").exists())


class TestProgressSemantics(unittest.TestCase):
    """进度语义：字节数递增、缺 Content-Length 时不编百分比。"""

    @classmethod
    def setUpClass(cls):
        cls._site_ctx = MockSite()
        cls.site = cls._site_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._site_ctx.__exit__(None, None, None)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.session = make_session()

    def tearDown(self):
        self.session.close()
        self._tmp.cleanup()

    def test_progress_bytes_strictly_increase(self):
        """进度回调拿到的字节数必须严格递增，最后等于文件大小。"""
        seen: list[tuple[int, int]] = []
        download_file(
            self.session,
            self.site.files_url("/files/slow.mp3"),
            self.folder,
            "slow.mp3",
            on_progress=lambda done, total: seen.append((done, total)),
        )
        self.assertGreater(len(seen), 1, "慢速文件应当分成多块，进度回调不止一次")
        bytes_seen = [done for done, _ in seen]
        self.assertEqual(bytes_seen, sorted(bytes_seen), "进度字节数不该倒退")
        self.assertEqual(len(set(bytes_seen)), len(bytes_seen), "进度字节数不该重复")
        self.assertEqual(bytes_seen[-1], SLOW_FILE_SIZE)
        # 声明了 Content-Length，所以 total 应当是该文件的真实大小
        self.assertTrue(all(total == SLOW_FILE_SIZE for _, total in seen))

    def test_missing_content_length_reports_zero_total(self):
        """对方没给 Content-Length 时，total 必须是 0（界面据此显示"未知大小"）。

        核心层的契约是：total == 0 表示"大小未知"，绝不编一个假的数字出来。
        """
        seen: list[tuple[int, int]] = []
        download_file(
            self.session,
            self.site.files_url("/files/no-length.mp3"),
            self.folder,
            "no-length.mp3",
            on_progress=lambda done, total: seen.append((done, total)),
        )
        self.assertTrue(seen, "应当收到进度回调")
        self.assertTrue(
            all(total == 0 for _, total in seen),
            f"没有 Content-Length 时 total 必须是 0，实际：{seen}",
        )
        self.assertEqual(seen[-1][0], NO_LENGTH_FILE_SIZE)
        # 文件本身仍然要完整落盘（没有长度就用不了"字节数校验"这一关）
        self.assertEqual((self.folder / "no-length.mp3").stat().st_size, NO_LENGTH_FILE_SIZE)


class TestSummaryModel(unittest.TestCase):
    """汇总模型：计数、以及"有失败就不能说全部成功"。"""

    def test_summarize_counts_all_four_statuses(self):
        from khdownloader_core import TrackResult

        results = [
            TrackResult("a", "completed", "ok"),
            TrackResult("b", "completed", "ok"),
            TrackResult("c", "skipped", "skip"),
            TrackResult("d", "failed", "boom"),
            TrackResult("e", "cancelled", "stop"),
        ]
        counts = summarize_results(results)
        self.assertEqual(
            counts, {"completed": 2, "skipped": 1, "failed": 1, "cancelled": 1}
        )

    def test_summarize_empty_is_all_zero(self):
        self.assertEqual(
            summarize_results([]),
            {"completed": 0, "skipped": 0, "failed": 0, "cancelled": 0},
        )

    def test_skipped_counts_as_ok_but_not_completed(self):
        from khdownloader_core import TrackResult

        result = TrackResult("a", "skipped", "已存在")
        self.assertTrue(result.ok, "跳过算顺利，不算失败")
        self.assertEqual(summarize_results([result])["completed"], 0)

    def test_partial_failure_is_not_all_successful(self):
        """有一首失败时，counts 里 failed 必须 > 0 —— 界面才能不给"全部完成"的错误结论。"""
        from khdownloader_core import TrackResult

        results = [
            TrackResult("a", "completed", "ok"),
            TrackResult("b", "completed", "ok"),
            TrackResult("c", "failed", "HTTP 404"),
        ]
        counts = summarize_results(results)
        self.assertGreater(counts["failed"], 0)
        self.assertEqual(counts["completed"] + counts["skipped"], 2)
        self.assertNotEqual(counts["completed"] + counts["skipped"], len(results))

    def test_cancelled_does_not_count_as_success(self):
        from khdownloader_core import TrackResult

        counts = summarize_results(
            [TrackResult("a", "completed", "ok"), TrackResult("b", "cancelled", "已取消")]
        )
        self.assertEqual(counts["completed"], 1)
        self.assertEqual(counts["cancelled"], 1)
        # 别把取消混进"成功"里
        self.assertNotIn("cancelled", ("completed", "skipped"))

    def test_album_done_event_carries_counts_and_results(self):
        """download_album 的 done 事件必须同时带上 counts 和完整 results。"""
        with MockSite() as site:
            with tempfile.TemporaryDirectory() as tmp:
                events = []
                session = make_session()
                try:
                    results = download_album(
                        album_url=site.album_url,
                        folder=Path(tmp),
                        on_event=lambda kind, data: events.append((kind, data)),
                        session=session,
                        pause=0,
                    )
                finally:
                    session.close()

        done = [d for k, d in events if k == "done"]
        self.assertEqual(len(done), 1)
        self.assertIn("counts", done[0])
        self.assertIn("results", done[0])
        self.assertEqual(done[0]["counts"], summarize_results(results))
        self.assertEqual(done[0]["counts"]["failed"], 1)  # 第 3 首缺 MP3 链接


class TestCancelledEventSequence(unittest.TestCase):
    """取消时的事件序列：先 cancelled，再 done，且 done 里带正确计数。"""

    def test_cancelled_event_precedes_done(self):
        with MockSite() as site:
            with tempfile.TemporaryDirectory() as tmp:
                events = []
                session = make_session()
                try:
                    download_album(
                        album_url=site.album_url,
                        folder=Path(tmp),
                        on_event=lambda kind, data: events.append(kind),
                        session=session,
                        pause=0,
                        should_cancel=lambda: True,
                    )
                finally:
                    session.close()

        self.assertIn("cancelled", events)
        self.assertEqual(events[-1], "done", "done 事件必须是最后一个")
        self.assertLess(
            events.index("cancelled"), events.index("done"),
            "cancelled 事件应当先于 done 事件",
        )

    def test_phase_events_bracket_the_download(self):
        """每首曲目应当先发 resolving，再发 downloading。

        注意：第 3 首（"Bad: Name?"）故意缺 MP3 链接，它只走到 resolving
        就失败了，所以 resolving 会比 downloading 多 —— 这是正确的，
        反而是"失败曲目不该报 downloading"的证明。
        """
        with MockSite() as site:
            with tempfile.TemporaryDirectory() as tmp:
                phases = []
                session = make_session()
                try:
                    download_album(
                        album_url=site.album_url,
                        folder=Path(tmp),
                        on_event=lambda kind, data: phases.append(data.get("phase"))
                        if kind == "phase"
                        else None,
                        session=session,
                        pause=0,
                    )
                finally:
                    session.close()

        self.assertIn("resolving", phases)
        self.assertIn("downloading", phases)
        # 成功下载的 2 首都发过 downloading；缺链接的那首只停在 resolving
        self.assertEqual(phases.count("downloading"), 2)
        self.assertEqual(phases.count("resolving"), 3)
        self.assertLess(phases.index("resolving"), phases.index("downloading"))


class TestBlockedStillDistinguishable(unittest.TestCase):
    """Cloudflare 人机校验在整条专辑流程里依然可分辨（不会被吞成"没找到曲目"）。"""

    def test_album_blocked_raises_site_blocked_error(self):
        with MockSite() as site:
            with tempfile.TemporaryDirectory() as tmp:
                session = make_session()
                try:
                    with self.assertRaises(SiteBlockedError) as ctx:
                        download_album(
                            album_url=site.blocked_url,
                            folder=Path(tmp),
                            on_event=lambda kind, data: None,
                            session=session,
                            pause=0,
                        )
                finally:
                    session.close()
        message = str(ctx.exception)
        self.assertIn("Cloudflare", message)
        # v1.0.0：立场变了 —— 不再是"本工具不会去绕过这道保护"，而是"自动逐级升级"。
        # 所以断言改成检查升级层是否写清楚（旧的 assertIn("绕过", message) 对应 v008）。
        self.assertIn("指纹", message)
        self.assertIn("真浏览器", message)

    def test_blocked_is_not_reported_as_no_tracks(self):
        """被拦时抛的必须是 SiteBlockedError，不能是 NoTracksFoundError。"""
        with MockSite() as site:
            with tempfile.TemporaryDirectory() as tmp:
                session = make_session()
                try:
                    with self.assertRaises(DownloaderError) as ctx:
                        download_album(
                            album_url=site.blocked_200_url,
                            folder=Path(tmp),
                            on_event=lambda kind, data: None,
                            session=session,
                            pause=0,
                        )
                finally:
                    session.close()
        self.assertIsInstance(ctx.exception, SiteBlockedError)
        self.assertNotIsInstance(ctx.exception, NoTracksFoundError)


class TestRealSiteEvidence(unittest.TestCase):
    """用**真实页面**做的回归测试。

    夹具 `tests/fixtures/real_song_page.html` 是从真实站点上直接取回来的
    （HTTP 200 / text/html，详见该文件顶部的来源说明），不是合成页面。
    这些测试锁住的是"真实站点长什么样"这件事——哪天站点改版，这几条会先炸。
    """

    #: 真实曲目详情页的地址（注意：它虽然是 .mp3 结尾，但返回的是 HTML）
    REAL_PAGE_URL = (
        "https://downloads.khinsider.com/game-soundtracks/album/"
        "cross-channel-in-memory-of-all-people-special-soundtrack-2011/02%2520Crisscross.mp3"
    )
    #: 真实站点上这一曲的 MP3 实际地址
    REAL_MP3_URL = (
        "https://lambda.vgmtreasurechest.com/soundtracks/"
        "cross-channel-in-memory-of-all-people-special-soundtrack-2011/dighssju/02%20Crisscross.mp3"
    )

    def setUp(self):
        self.html = _fixture("real_song_page.html")

    def test_real_detail_page_resolves_exact_mp3_url(self):
        self.assertEqual(parse_song_page(self.html, self.REAL_PAGE_URL), self.REAL_MP3_URL)

    def test_real_mp3_url_is_absolute(self):
        mp3 = parse_song_page(self.html, self.REAL_PAGE_URL)
        self.assertTrue(mp3.startswith("https://"))

    def test_real_mp3_url_is_cross_host(self):
        mp3 = parse_song_page(self.html, self.REAL_PAGE_URL)
        self.assertNotEqual(urlsplit(mp3).netloc, urlsplit(self.REAL_PAGE_URL).netloc)
        self.assertEqual(urlsplit(mp3).netloc, "lambda.vgmtreasurechest.com")

    def test_flac_link_is_not_selected(self):
        """真实页面上还有一个写法几乎一样的 "download as FLAC" 链接，绝不能选错。"""
        mp3 = parse_song_page(self.html, self.REAL_PAGE_URL)
        self.assertFalse(mp3.lower().endswith(".flac"))
        self.assertTrue(mp3.lower().endswith(".mp3"))
        self.assertIn(".flac", self.html, "夹具里必须真的有 FLAC 链接，这条测试才有意义")

    def test_picks_one_link_out_of_many(self):
        """真实页面上有上百个链接（A~Z 导航等），只能挑出那一个下载地址。"""
        from bs4 import BeautifulSoup

        anchor_count = len(BeautifulSoup(self.html, "html.parser").find_all("a", href=True))
        self.assertGreater(anchor_count, 50, "夹具应当保留大量干扰链接")
        self.assertEqual(parse_song_page(self.html, self.REAL_PAGE_URL), self.REAL_MP3_URL)

    def test_detail_page_is_not_mistaken_for_album_page(self):
        """曲目页不该被专辑页解析器当成有曲目的专辑页（反之亦然）。"""
        with self.assertRaises(NoTracksFoundError):
            parse_album_page(self.html, self.REAL_PAGE_URL)


class TestRealAlbumPageEvidence(unittest.TestCase):
    """用**真实专辑页**做的回归测试。

    夹具 `tests/fixtures/real_album_page.html` 是从真实站点上直接取回来的
    （HTTP 200 / text/html / 83,062 字节，详见该文件顶部来源说明），不是合成页面。
    它锁住的是"真实专辑页长什么样"——哪天站点改版，这几条会先炸。

    这张页面的真实结构比合成夹具棘手得多，它同时包含三类干扰：
      · 同一行里 6 个链接指向同一地址（必须去重）；
      · 时长 "2:27"、MP3 大小 "4.70 MB"、FLAC 大小 "17.15 MB" 三个格子
        挂着和曲名**同一个** href，只是文字不同（不能把文字当曲名）；
      · 页面上还有大量 A~Z 导航、相关专辑、论坛等无关链接（不能越界收录）。
    """

    REAL_ALBUM_URL = (
        "https://downloads.khinsider.com/game-soundtracks/album/"
        "cross-channel-in-memory-of-all-people-special-soundtrack-2011"
    )
    #: 真实专辑页上一共 30 首（第 1、28、29、30 首是日文曲名）
    REAL_TRACK_COUNT = 30

    def setUp(self):
        self.html = _fixture("real_album_page.html")
        self.tracks = parse_album_page(self.html, self.REAL_ALBUM_URL)

    def test_track_count_and_order(self):
        self.assertEqual(len(self.tracks), self.REAL_TRACK_COUNT)
        self.assertEqual(self.tracks[0].name, "群青のティファレス(GAME Size)")
        self.assertEqual(self.tracks[1].name, "Crisscross")
        self.assertEqual(self.tracks[26].name, "CROSSING")
        self.assertEqual(self.tracks[29].name, "時間の雲(FULL Size)")

    def test_no_numeric_labels_leak_into_track_names(self):
        """时长/文件大小这类纯数字标签绝不能被当成曲名。"""
        for track in self.tracks:
            self.assertNotRegex(
                track.name, r"^[\d\s.,:]+$", f"曲名不该是数字标签：{track.name!r}"
            )
            self.assertNotIn("MB", track.name)
            self.assertNotIn("KB", track.name)

    def test_every_track_has_exactly_one_url(self):
        """同一行 6 个等价链接必须去重成 1 条。"""
        self.assertEqual(len(self.tracks), self.REAL_TRACK_COUNT)
        self.assertEqual(len({t.page_url for t in self.tracks}), self.REAL_TRACK_COUNT)

    def test_urls_stay_inside_this_album(self):
        """导航、相关专辑、论坛的链接一律不能混进来。"""
        prefix = self.REAL_ALBUM_URL + "/"
        for track in self.tracks:
            self.assertTrue(track.page_url.startswith(prefix), track.page_url)
        joined = " ".join(t.page_url for t in self.tracks)
        self.assertNotIn("/forums", joined)
        self.assertNotIn("/game-soundtracks/browse", joined)
        self.assertNotIn("random-album", joined)

    def test_urls_are_absolute_and_double_encoded(self):
        for track in self.tracks:
            self.assertTrue(track.page_url.startswith("https://"))
        self.assertIn("%2520Crisscross.mp3", self.tracks[1].page_url)

    def test_japanese_titles_survive(self):
        """非 ASCII 曲名不能乱码（requests 猜错编码是常见坑）。"""
        names = [t.name for t in self.tracks]
        self.assertIn("群青のティファレス(FULL Size)", names)
        self.assertIn("時間の雲(GAME Size)", names)

    def test_japanese_titles_produce_valid_filenames(self):
        """日文曲名落盘时不能被净化规则弄成空串，也不该丢掉后缀。"""
        for track in self.tracks:
            filename = sanitize_filename(track.name)
            self.assertTrue(filename)
            self.assertNotEqual(filename, "untitled", track.name)
            self.assertNotIn("/", filename)

    def test_real_album_page_is_not_mistaken_for_detail_page(self):
        """专辑页里不该解析出 MP3 直链（那些 .mp3 链接是详情页，不是音频）。"""
        with self.assertRaises(NoMp3LinkError):
            parse_song_page(self.html, self.REAL_ALBUM_URL)

    def test_songlist_guard_blocks_every_rule(self):
        """结构守卫必须对四条规则都生效，不能只在其中一条上生效。

        构造一个"最坏情况"页面：唯一的 .mp3 链接就在 table#songlist 里，
        而且链接文字和 class 都写成最能骗过前三规则的形态。即便这样，
        也必须判定为"找不到下载地址"，而不是返回详情页地址。
        """
        fake = (
            '<html><body>'
            '<table id="songlist"><tr>'
            '<td><a href="/game-soundtracks/album/mock/01%2520A.mp3" class="x">'
            '<span class="songDownloadLink">get_app Click here to download as MP3</span>'
            "</a></td>"
            "</tr></table>"
            "</body></html>"
        )
        with self.assertRaises(NoMp3LinkError):
            parse_song_page(fake, "https://downloads.khinsider.com/game-soundtracks/album/mock")

    def test_download_link_outside_table_still_works(self):
        """守卫不能误伤正常情况：下载链接在表外时，规则照常生效。"""
        fake = (
            '<html><body>'
            '<table id="songlist"><tr><td><a href="/game-soundtracks/album/mock/01%2520A.mp3">A</a></td></tr></table>'
            '<a href="https://files.example.test/mock/01%20A.mp3" class="dl">'
            '<span class="songDownloadLink">get_app Click here to download as MP3</span></a>'
            "</body></html>"
        )
        self.assertEqual(
            parse_song_page(fake, "https://downloads.khinsider.com/game-soundtracks/album/mock/01%2520A.mp3"),
            "https://files.example.test/mock/01%20A.mp3",
        )

    def test_songlist_table_is_the_scope(self):
        """确认我们依赖的 table#songlist 在真实页面里确实存在。"""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(self.html, "html.parser")
        table = soup.find("table", id="songlist")
        self.assertIsNotNone(table, "真实专辑页必须有 table#songlist")
        self.assertEqual(table.find("tr", id="songlist_header") is not None, True)
        self.assertEqual(table.find("tr", id="songlist_footer") is not None, True)


class TestFailureClasses(unittest.TestCase):
    """四类失败必须分得开：网络层 / 人机校验 / HTTP 错误 / 解析失败。"""

    def test_error_hierarchy(self):
        self.assertTrue(issubclass(NetworkError, DownloaderError))
        self.assertTrue(issubclass(SiteBlockedError, DownloaderError))
        self.assertTrue(issubclass(NoTracksFoundError, DownloaderError))
        self.assertTrue(issubclass(NoMp3LinkError, DownloaderError))

    def test_classes_are_distinct(self):
        classes = {NetworkError, SiteBlockedError, NoTracksFoundError, NoMp3LinkError}
        self.assertEqual(len(classes), 4)


class TestChallengeDetection(unittest.TestCase):
    """人机校验不能只认 403 —— 状态码可能是别的，但页面内容一定是那个校验页。"""

    @classmethod
    def setUpClass(cls):
        cls._site_ctx = MockSite()
        cls.site = cls._site_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._site_ctx.__exit__(None, None, None)

    def setUp(self):
        self.session = make_session()

    def tearDown(self):
        self.session.close()

    def test_503_challenge_is_detected(self):
        with self.assertRaises(SiteBlockedError):
            fetch_html(self.session, self.site.blocked_503_url)

    def test_200_with_challenge_body_is_detected(self):
        with self.assertRaises(SiteBlockedError):
            fetch_html(self.session, self.site.blocked_200_url)

    def test_403_challenge_still_detected(self):
        with self.assertRaises(SiteBlockedError):
            fetch_html(self.session, self.site.blocked_url)

    def test_plain_404_is_not_a_challenge(self):
        """普通 HTTP 错误不能被误报成人机校验。"""
        with self.assertRaises(DownloaderError) as ctx:
            fetch_html(self.session, self.site.url("/definitely-not-here"))
        self.assertNotIsInstance(ctx.exception, SiteBlockedError)

    def test_blocked_message_explains_it_is_not_no_tracks(self):
        """被拦时的提示必须讲清楚：这跟"没找到曲目"不是一回事，且写明了升级层。"""
        with self.assertRaises(SiteBlockedError) as ctx:
            fetch_html(self.session, self.site.blocked_url)
        message = str(ctx.exception)
        self.assertIn("没找到曲目", message)
        # v1.0.0：三层升级（指纹 / 真浏览器 / 本机浏览器）都要在提示里说清楚。
        self.assertIn("指纹", message)
        self.assertIn("真浏览器", message)
        self.assertNotIn("cf_clearance", message.lower())


class TestAlbumPathGuard(unittest.TestCase):
    """确认解析出的曲目地址确实都属于本专辑（防止跨专辑误抓）。"""

    def test_all_tracks_share_album_prefix(self):
        album_url = "https://downloads.khinsider.com/game-soundtracks/album/" + ALBUM_SLUG
        tracks = parse_album_page(_fixture("album_page.html"), album_url)
        prefix = album_path(album_url) + "/"
        for track in tracks:
            self.assertTrue(urlsplit(track.page_url).path.startswith(prefix), track.page_url)


class TestFingerprintTransport(unittest.TestCase):
    """v1.0.0 的防火墙修复（传输层换浏览器指纹）：接口形状与"不乱跑网络"的边界。

    这些用例全部离线：明文 http 地址不触发升级梯子，所以不会去开浏览器、也不会去打外网。
    """

    def test_transport_layer_keeps_requests_semantics(self):
        """换指纹之后，拿到的还得是个 requests.Session —— 上层代码一行都不用改。"""
        session = make_session()
        try:
            self.assertIsInstance(session, requests.Session)
        finally:
            session.close()

    def test_local_http_never_escalates(self):
        """本地 http 地址直接跳过升级梯子：保证测试不碰外网、不开浏览器。"""
        session = make_session()
        try:
            response, html, tier = transport.recover_html(session, "http://127.0.0.1:9/x")
        finally:
            session.close()
        self.assertIsNone(response)
        self.assertIsNone(html)
        self.assertEqual(tier, "skipped:not-https")

    def test_local_http_still_uses_plain_requests(self):
        with MockSite() as site:
            session = make_session()
            try:
                html = fetch_html(session, site.album_url)
                self.assertEqual(getattr(session, "transport_tier", "requests"), "requests")
            finally:
                session.close()
        self.assertIn("songlist", html)

    def test_describe_reports_ladder_and_flags(self):
        info = transport.describe()
        self.assertIn(info["mode"], ("auto", "requests", "impersonate"))
        self.assertTrue(info["ladder"], "升级梯子不能是空的")
        self.assertIsInstance(info["curl_cffi"], bool)
        self.assertIsInstance(info["playwright"], bool)

    def test_env_switch_can_disable_system_browser(self):
        """`KHD_SYSTEM_BROWSER=0` 要能关掉"借本机浏览器"那一层。"""
        os.environ["KHD_SYSTEM_BROWSER"] = "0"
        try:
            self.assertFalse(transport.system_browser_enabled())
        finally:
            os.environ.pop("KHD_SYSTEM_BROWSER", None)
        self.assertTrue(transport.system_browser_enabled())

    def test_challenge_judgement_matches_core(self):
        """传输层和 core 的"是不是人机校验页"判据必须一致，否则梯子会判错。"""

        class _Fake:
            def __init__(self, status, text, headers=None):
                self.status_code = status
                self.text = text
                self.headers = headers or {}

        cases = [
            _Fake(403, "<title>Just a moment...</title>", {"cf-mitigated": "challenge"}),
            _Fake(200, "<html>challenges.cloudflare.com</html>"),
            _Fake(503, "x", {"cf-mitigated": "challenge"}),
            _Fake(403, "<html><h1>403 Forbidden</h1></html>"),
            _Fake(200, "<html><body>songlist</body></html>"),
        ]
        for case in cases:
            self.assertEqual(
                transport.looks_like_challenge(case),
                khdownloader_core._looks_like_challenge(case),
                f"判据不一致：{case.status_code} {case.text[:40]!r}",
            )


@unittest.skipUnless(transport.CURL_CFFI_AVAILABLE, "需要 curl_cffi")
class TestTransportResponseAdapter(unittest.TestCase):
    """curl_cffi 的响应被包一层后，要跟 requests 的响应一样好用。"""

    def test_adapter_supports_context_manager_encoding_and_streaming(self):
        with MockSite() as site:
            session = make_session()
            try:
                response = session.raw_impersonated_request(
                    "GET", site.album_url, impersonate="chrome"
                )
                self.assertEqual(response.status_code, 200)

                # download_file() 用了 `with response:`，curl_cffi 原生不支持 —— 这层必须补上。
                with response as opened:
                    self.assertEqual(opened.status_code, 200)

                # fetch_html() 会给 .encoding 赋值（日文曲名不乱码那一段），必须可写。
                response.encoding = "utf-8"
                self.assertEqual(response.encoding, "utf-8")
                self.assertIn("songlist", response.text)
                self.assertTrue(response.apparent_encoding)

                # 分块读：非 stream 请求下 curl_cffi 原生会 assert，适配层要退化成
                # "把缓冲好的内容切块"（requests 就是这个行为）。
                body = b"".join(response.iter_content(chunk_size=4096))
                self.assertIn(b"songlist", body)

                # 真·流式路径（下载 MP3 走的就是这条）：stream=True 时必须能边收边吐。
                streamed = session.raw_impersonated_request(
                    "GET", site.album_url, impersonate="chrome", stream=True
                )
                try:
                    chunks = list(streamed.iter_content(chunk_size=8192))
                finally:
                    streamed.close()
                self.assertGreaterEqual(len(chunks), 1)
                self.assertIn(b"songlist", b"".join(chunks))

                # 响应头 / url / request.url 这些诊断字段要有。
                self.assertIn("html", response.headers.get("Content-Type", "").lower())
                self.assertTrue(response.url.startswith("http://localhost"))
                self.assertTrue(response.request.url.startswith("http://localhost"))
            finally:
                session.close()


class TestEscalationLadder(unittest.TestCase):
    """升级梯子本身：本地假站点上离线跑一遍（用 KHD_TRANSPORT_ALLOW_LOCAL 放开 https 限制）。"""

    def setUp(self):
        self._saved = {
            key: os.environ.get(key)
            for key in ("KHD_TRANSPORT_ALLOW_LOCAL", "KHD_BROWSER_FALLBACK", "KHD_IMPERSONATE")
        }
        os.environ["KHD_TRANSPORT_ALLOW_LOCAL"] = "1"
        os.environ["KHD_BROWSER_FALLBACK"] = "0"  # 只测指纹层，别在测试里开浏览器
        os.environ.pop("KHD_IMPERSONATE", None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @unittest.skipUnless(transport.CURL_CFFI_AVAILABLE, "需要 curl_cffi")
    def test_challenged_first_then_recovers(self):
        """第一次被拦 → 换指纹重试 → 专辑页真的被救回来了（全程离线）。"""
        with MockSite() as site:
            site.reset_flaky()
            session = make_session()
            try:
                html = fetch_html(session, site.flaky_album_url)
            finally:
                session.close()
        self.assertIn("songlist", html)

    @unittest.skipUnless(transport.CURL_CFFI_AVAILABLE, "需要 curl_cffi")
    def test_ladder_rotates_fingerprints_until_it_works(self):
        with MockSite() as site:
            site.reset_flaky()
            session = make_session()
            try:
                response, html, tier = transport.recover_html(session, site.flaky_album_url)
            finally:
                session.close()
        self.assertIsNotNone(response)
        self.assertIsNone(html)
        self.assertTrue(tier.startswith("L1:impersonate="), tier)
        self.assertIn("songlist", response.text)

    @unittest.skipUnless(transport.CURL_CFFI_AVAILABLE, "需要 curl_cffi")
    def test_all_fingerprints_fail_reports_what_was_tried(self):
        """整条梯子都没过时，报告里要说清楚试过哪些指纹（方便用户自查）。"""
        with MockSite() as site:
            session = make_session()
            try:
                response, html, tier = transport.recover_html(session, site.blocked_url)
            finally:
                session.close()
        self.assertIsNone(response)
        self.assertIsNone(html)
        self.assertTrue(tier.startswith("L1-failed"), tier)
        self.assertIn("chrome", tier)


class TestSystemBrowserFallback(unittest.TestCase):
    """L4：借本机 Chrome/Edge 无头渲染（打包成 EXE 之后唯一还能用的一层）。"""

    @unittest.skipUnless(transport.find_system_browser(), "本机没有 Chrome/Edge")
    def test_dump_dom_returns_rendered_html(self):
        with MockSite() as site:
            html = transport.render_with_system_browser(site.album_url, budget_ms=3000)
        self.assertIsNotNone(html, "Chrome/Edge 没能渲染出页面")
        self.assertIn("songlist", html)

    @unittest.skipUnless(transport.find_system_browser(), "本机没有 Chrome/Edge")
    def test_challenge_page_is_not_mistaken_for_content(self):
        """本机浏览器拿回来的要是校验页，就必须判成"没成功"，不能当天真页面用。"""
        with MockSite() as site:
            html = transport.render_with_system_browser(site.blocked_url, budget_ms=3000)
        self.assertIsNotNone(html)
        self.assertTrue(transport.looks_like_challenge(transport._HtmlProbe(html)))


if __name__ == "__main__":
    unittest.main(verbosity=2)

