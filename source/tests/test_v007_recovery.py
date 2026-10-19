# -*- coding: utf-8 -*-
"""
test_v007_recovery.py —— v007 新增行为的测试

覆盖本轮新增/修复的行为（Brief §8.1）：
  1. 校验页 / 普通 403 / 429 / 超时 / 正常 HTML 解析失败 能正确区分；
  2. 导入真实结构的 HTML 能正确解析曲目，验证页不能误判为专辑；
  3. 真实结构样本检验相对 URL、日文及编码路径；
  4. MP3 入口拒绝把 HTML 错误内容保存为音频；
  5. 恢复入口（单曲下载）的取消、重复操作、已有文件保护与失败清理；
  6. 诊断信息脱敏（不含 Cookie / Authorization）。

只测本轮**实际实现**的路径，不删改历史期望。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from khdownloader_core import (  # noqa: E402
    IMPORT_KIND_ALBUM,
    IMPORT_KIND_CHALLENGE,
    IMPORT_KIND_ERROR,
    IMPORT_KIND_NO_TRACKS,
    IMPORT_KIND_UNRESOLVED,
    AccessDeniedError,
    CancelledError,
    DownloaderError,
    NotAudioContentError,
    RateLimitError,
    SiteBlockedError,
    classify_blocked_response,
    download_album,
    download_file,
    download_single_link,
    fetch_html,
    inspect_import_html,
    make_session,
    summarize_response_for_diagnostics,
)
from mock_site import MockSite  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ALBUM_URL = (
    "https://downloads.khinsider.com/game-soundtracks/album/"
    "cross-channel-in-memory-of-all-people-special-soundtrack-2011"
)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8-sig", errors="replace")


class TestErrorClassification(unittest.TestCase):
    """Brief §8.1：校验页 / 403 / 429 / 正常解析失败 必须能区分。"""

    @classmethod
    def setUpClass(cls):
        cls._ctx = MockSite()
        cls.site = cls._ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    def setUp(self):
        self.session = make_session()

    def tearDown(self):
        self.session.close()

    def test_challenge_page_is_site_blocked(self):
        with self.assertRaises(SiteBlockedError) as ctx:
            fetch_html(self.session, self.site.blocked_url)
        self.assertIn("人机校验", str(ctx.exception))

    def test_challenge_with_200_is_still_blocked(self):
        # 状态码 200 但内容是校验页 —— 也要判为被拦，不能当正常页面
        with self.assertRaises(SiteBlockedError):
            fetch_html(self.session, self.site.blocked_200_url)

    def test_plain_403_is_access_denied_not_cloudflare(self):
        # 关键：普通 403 没有人机校验特征，**不能**说成 Cloudflare
        with self.assertRaises(AccessDeniedError) as ctx:
            fetch_html(self.session, self.site.denied_url)
        text = str(ctx.exception)
        self.assertIn("403", text)
        self.assertIn("访问被拒绝", text)
        # 类型必须是 AccessDeniedError，而不是 SiteBlockedError
        self.assertNotIsInstance(ctx.exception, SiteBlockedError)

    def test_429_is_rate_limit_not_cloudflare(self):
        with self.assertRaises(RateLimitError) as ctx:
            fetch_html(self.session, self.site.limited_url)
        text = str(ctx.exception)
        self.assertIn("429", text)
        self.assertIn("限流", text)
        self.assertNotIsInstance(ctx.exception, SiteBlockedError)

    def test_access_denied_and_blocked_are_different_types(self):
        # 两者不能是同一个类（否则界面无法给出不同下一步）
        self.assertFalse(issubclass(AccessDeniedError, SiteBlockedError))
        self.assertFalse(issubclass(SiteBlockedError, AccessDeniedError))

    def test_classify_helper_prioritizes_challenge(self):
        # 校验页优先于状态码：即使 403，只要 challenge_like 就归 SiteBlockedError
        self.assertIsInstance(classify_blocked_response(403, True), SiteBlockedError)
        self.assertIsInstance(classify_blocked_response(403, False), AccessDeniedError)
        self.assertIsInstance(classify_blocked_response(429, False), RateLimitError)
        # 其他状态码不在这里处理
        self.assertIsNone(classify_blocked_response(500, False))


class TestImportHtml(unittest.TestCase):
    """Brief §6.1：导入 HTML 要正确区分专辑页 / 验证页 / 错误页 / 无曲目页。"""

    def test_real_album_html_parses_tracks(self):
        html = _fixture("real_album_page.html")
        insp = inspect_import_html(html, ALBUM_URL)
        self.assertEqual(insp.kind, IMPORT_KIND_ALBUM)
        self.assertTrue(insp.ok)
        self.assertEqual(len(insp.tracks), 30)
        self.assertEqual(insp.tracks[0].name, "群青のティファレス(GAME Size)")

    def test_challenge_html_is_not_album(self):
        html = (
            "<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
            "<body>challenges.cloudflare.com</body></html>"
        )
        insp = inspect_import_html(html, ALBUM_URL)
        self.assertEqual(insp.kind, IMPORT_KIND_CHALLENGE)
        self.assertFalse(insp.ok)
        self.assertEqual(insp.tracks, [])

    def test_challenge_html_detected_via_header_too(self):
        # cf-mitigated 头也要能触发"校验页"判定
        insp = inspect_import_html(
            "<html><body>hello</body></html>", ALBUM_URL,
            headers={"cf-mitigated": "challenge"},
        )
        self.assertEqual(insp.kind, IMPORT_KIND_CHALLENGE)

    def test_tiny_file_is_error(self):
        insp = inspect_import_html("<html></html>", ALBUM_URL)
        self.assertEqual(insp.kind, IMPORT_KIND_ERROR)

    def test_json_is_error(self):
        insp = inspect_import_html('{"error": "nope"}', ALBUM_URL)
        self.assertEqual(insp.kind, IMPORT_KIND_ERROR)

    def test_album_without_tracks_is_no_tracks(self):
        # 一个看起来像网页（长度足够）、但没有本专辑曲目的页面。
        # v008 语义：页面没有曲目表、也无法识别来源 → unresolved（要求用户核对），
        # 不再笼统报 no_tracks（Brief v008 §4.1：先识别文件，再核对地址）。
        filler = "<!-- padding to make this look like a real page -->" * 4
        html = (
            "<html><body><h1>Some Album</h1>"
            "<a href='/game-soundtracks/album/other-thing/01.mp3'>other</a>"
            "<a href='https://example.com/x.mp3'>external</a>"
            f"{filler}"
            "</body></html>"
        )
        insp = inspect_import_html(html, ALBUM_URL)
        self.assertEqual(insp.kind, IMPORT_KIND_UNRESOLVED)
        self.assertFalse(insp.ok)
        self.assertEqual(insp.address_relation, "unresolved")

    def test_relative_and_japanese_links_resolve(self):
        # 真实夹具里的链接是"相对 + 双重编码 + 日文"，导入后必须解析成绝对地址
        insp = inspect_import_html(_fixture("real_album_page.html"), ALBUM_URL)
        first = insp.tracks[0]
        self.assertTrue(first.page_url.startswith(ALBUM_URL))
        self.assertIn("%25E7%25BE%25A4", first.page_url)  # 双重编码保持原样
        self.assertEqual(first.name, "群青のティファレス(GAME Size)")


class TestAudioContentGuard(unittest.TestCase):
    """Brief §8.1：MP3 入口拒绝把 HTML 错误内容保存为音频。"""

    @classmethod
    def setUpClass(cls):
        cls._ctx = MockSite()
        cls.site = cls._ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.session = make_session()

    def tearDown(self):
        self.session.close()
        self._tmp.cleanup()

    def test_html_returned_as_mp3_is_rejected(self):
        with self.assertRaises(NotAudioContentError):
            download_file(self.session, self.site.fake_html_mp3_url, self.folder, "x.mp3")

    def test_json_returned_as_mp3_is_rejected(self):
        with self.assertRaises(NotAudioContentError):
            download_file(self.session, self.site.fake_json_mp3_url, self.folder, "y.mp3")

    def test_rejected_content_leaves_no_file_and_no_part(self):
        for url, name in [(self.site.fake_html_mp3_url, "a.mp3"),
                          (self.site.fake_json_mp3_url, "b.mp3")]:
            with self.assertRaises(NotAudioContentError):
                download_file(self.session, url, self.folder, name)
        names = sorted(p.name for p in self.folder.iterdir())
        self.assertEqual(names, [], f"拒收后不该留下任何文件，实际：{names}")

    def test_real_audio_still_downloads(self):
        dest, written, skipped = download_file(
            self.session, self.site.files_url("/files/ok.mp3"), self.folder, "ok.mp3"
        )
        self.assertTrue(dest.exists())
        self.assertGreater(written, 0)
        self.assertFalse(skipped)


class TestSingleLinkDownload(unittest.TestCase):
    """Brief §6.1：单曲/链接下载入口（取消、重复、已有文件保护、失败清理）。"""

    @classmethod
    def setUpClass(cls):
        cls._ctx = MockSite()
        cls.site = cls._ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.session = make_session()
        self.events = []

    def tearDown(self):
        self.session.close()
        self._tmp.cleanup()

    def _emit(self, kind, data):
        self.events.append((kind, data))

    def test_single_link_downloads_once(self):
        url = self.site.files_url("/files/ok.mp3")
        result = download_single_link(url, self.folder, self._emit, session=self.session)
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.path.exists())
        self.assertTrue(any(k == "single_done" for k, _ in self.events))

    def test_single_link_second_run_skips(self):
        url = self.site.files_url("/files/ok.mp3")
        download_single_link(url, self.folder, self._emit, session=self.session)
        again = download_single_link(url, self.folder, self._emit, session=self.session)
        self.assertEqual(again.status, "skipped")

    def test_single_link_cancel_leaves_no_part(self):
        url = self.site.files_url("/files/slow.mp3")
        flag = {"c": True}

        def cancel():
            return flag["c"]

        result = download_single_link(
            url, self.folder, self._emit, session=self.session, should_cancel=cancel
        )
        self.assertEqual(result.status, "cancelled")
        names = sorted(p.name for p in self.folder.iterdir())
        self.assertEqual(names, [], f"取消后不该留下文件，实际：{names}")

    def test_single_link_html_content_fails_cleanly(self):
        result = download_single_link(
            self.site.fake_html_mp3_url, self.folder, self._emit, session=self.session
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), [])

    def test_single_link_bad_url_fails_cleanly(self):
        result = download_single_link("not-a-url", self.folder, self._emit, session=self.session)
        self.assertEqual(result.status, "failed")


class TestDiagnosticsSanitized(unittest.TestCase):
    """Brief §4.4：诊断信息必须脱敏，不含 Cookie / Authorization。"""

    @classmethod
    def setUpClass(cls):
        cls._ctx = MockSite()
        cls.site = cls._ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    def test_no_credentials_in_diagnostics(self):
        session = make_session()
        # 给会话塞一个伪造的敏感头，确认诊断采集不会把它带出来
        session.headers["Authorization"] = "Bearer SECRET-TOKEN"
        session.cookies.set("session", "SUPER-SECRET-COOKIE")
        try:
            r = session.get(self.site.blocked_url)
            info = summarize_response_for_diagnostics(r)
        finally:
            session.close()

        blob = json.dumps(info, ensure_ascii=False)
        self.assertNotIn("SECRET-TOKEN", blob)
        self.assertNotIn("SUPER-SECRET-COOKIE", blob)
        # 也不应出现 Set-Cookie 头
        self.assertNotIn("Set-Cookie", blob)

    def test_diagnostics_capture_challenge_signal(self):
        session = make_session()
        try:
            r = session.get(self.site.blocked_url)
            info = summarize_response_for_diagnostics(r)
        finally:
            session.close()
        self.assertEqual(info["status_code"], 403)
        self.assertTrue(info["challenge_like"])
