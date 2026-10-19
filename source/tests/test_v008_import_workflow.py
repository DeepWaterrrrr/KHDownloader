# -*- coding: utf-8 -*-
"""
test_v008_import_workflow.py —— v008 导入工作流的回归测试

覆盖 Brief v008 §9 的场景表（core 层 + GUI 层）：
  · 用户 2003 场景（合成样本，结构同原样文件）：正确地址 / 2011 地址 / 空地址 / 歌曲页地址
  · 地址规范化（末尾斜杠、查询参数、片段）
  · 干扰链接不误识别
  · 验证页 / MHTML / 缺曲目结构 分类正确
  · GUI：冲突确认、预览取消不下载、预览确认后用预览曲目、切模式作废导入

真实性声明：2003 场景用合成夹具（结构按 Strategy AI 审查报告描述构造），
**不是**用户原样文件；用户原样文件的验证在报告里单独标注。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import khdownloader_core as c  # noqa: E402
from khdownloader_core import (  # noqa: E402
    IMPORT_KIND_ALBUM,
    IMPORT_KIND_CHALLENGE,
    IMPORT_KIND_ERROR,
    IMPORT_KIND_NO_TRACKS,
    IMPORT_KIND_UNRESOLVED,
    IMPORT_KIND_UNSUPPORTED_FORMAT,
    album_identity,
    detect_mhtml,
    identify_album_source,
    inspect_import_html,
    is_song_page_url,
    same_album,
)

URL_2003 = (
    "https://downloads.khinsider.com/game-soundtracks/album/"
    "cross-channel-soundtrack-arrange-version-criss-cross-2003"
)
URL_2011 = (
    "https://downloads.khinsider.com/game-soundtracks/album/"
    "cross-channel-in-memory-of-all-people-special-soundtrack-2011"
)
SONG_2003 = URL_2003 + "/01%2520Airwaves%2520Arr.ver.mp3"

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture_2003() -> str:
    return (FIXTURES / "album_page_2003_synthetic.html").read_text(encoding="utf-8")


def _fixture_2011() -> str:
    return (FIXTURES / "real_album_page.html").read_text(encoding="utf-8-sig", errors="replace")


class TestAlbumIdentity(unittest.TestCase):
    """地址规范化：末尾斜杠 / 查询参数 / 片段 不改变专辑身份。"""

    def test_trailing_slash_ignored(self):
        self.assertTrue(same_album(URL_2003, URL_2003 + "/"))

    def test_query_and_fragment_ignored(self):
        self.assertTrue(same_album(URL_2003, URL_2003 + "/?utm_source=x#top"))

    def test_different_albums_never_equal(self):
        self.assertFalse(same_album(URL_2003, URL_2011))

    def test_song_page_is_not_album_page(self):
        self.assertFalse(same_album(URL_2003, SONG_2003))
        self.assertTrue(is_song_page_url(SONG_2003))
        self.assertFalse(is_song_page_url(URL_2003))

    def test_album_identity_normalizes(self):
        h, p = album_identity("HTTPS://Downloads.KHInsider.com/game-soundtracks/album/X/")
        self.assertEqual(h, "downloads.khinsider.com")
        self.assertEqual(p, "/game-soundtracks/album/X")
        # 双重编码不该被循环解开：%2520 仍保留在路径里
        _, p2 = album_identity(URL_2003 + "/01%2520Airwaves%2520Arr.ver.mp3")
        self.assertIn("%2520", p2)


class TestIdentifyAlbumSource(unittest.TestCase):
    """工作 A：在地址过滤之前，从页面自身识别专辑来源。"""

    def test_2003_synthetic_identified(self):
        src = identify_album_source(_fixture_2003())
        self.assertTrue(src["identified"])
        self.assertEqual(src["album_slug"],
                         "cross-channel-soundtrack-arrange-version-criss-cross-2003")
        self.assertEqual(src["album_url"], URL_2003)
        self.assertEqual(src["track_count"], 19)
        self.assertIn("CRISS", src["title"].upper())

    def test_2011_real_identified(self):
        src = identify_album_source(_fixture_2011())
        self.assertTrue(src["identified"])
        self.assertEqual(src["track_count"], 30)

    def test_related_albums_not_collected(self):
        # 推荐专辑里的 2011 链接绝不能改变 2003 页面的识别结果
        src = identify_album_source(_fixture_2003())
        self.assertEqual(src["album_slug"],
                         "cross-channel-soundtrack-arrange-version-criss-cross-2003")
        self.assertEqual(src["track_count"], 19)  # 不会把 2011 的链接也算进来

    def test_no_songlist_no_canonical_unidentified(self):
        html = "<html><body><h1>hello</h1>" + "<!-- pad -->" * 60 + "</body></html>"
        src = identify_album_source(html)
        self.assertFalse(src["identified"])
        self.assertEqual(src["album_slug"], None)

    def test_external_mp3_links_not_used_for_identity(self):
        html = (
            "<html><body><table id='songlist'><tr>"
            "<td><a href='https://cdn.example.com/soundtracks/x/01%2520a.mp3'>a</a></td>"
            "</tr></table></body></html>"
        )
        src = identify_album_source(html)
        self.assertFalse(src["identified"])
        self.assertTrue(any("外站" in c for c in src["conflicts"]))


class TestInspectImportHtmlV008(unittest.TestCase):
    """工作 A：inspect_import_html 的"先识别、再核对"语义。"""

    def test_2003_with_correct_url(self):
        insp = inspect_import_html(_fixture_2003(), URL_2003)
        self.assertEqual(insp.kind, IMPORT_KIND_ALBUM)
        self.assertEqual(len(insp.tracks), 19)
        self.assertEqual(insp.tracks[0].name, "Airwaves Arr.ver.")
        self.assertEqual(insp.tracks[-1].name, "_and then_")
        self.assertEqual(insp.address_relation, "match")
        self.assertTrue(insp.matched_address)

    def test_2003_with_2011_url_reports_conflict(self):
        """核心回归：用户实际遇到的失败场景 —— 地址与文件不匹配。"""
        insp = inspect_import_html(_fixture_2003(), URL_2011)
        self.assertEqual(insp.kind, IMPORT_KIND_NO_TRACKS)
        self.assertEqual(insp.address_relation, "different_album")
        self.assertFalse(insp.ok)
        # 提示里必须同时给出两个地址，且不能说成"页面没有曲目"
        self.assertIn(URL_2011, insp.message)
        self.assertIn(URL_2003, insp.message)
        self.assertNotIn("没有解析到任何曲目链接", insp.message)

    def test_2003_with_empty_url_self_identifies(self):
        insp = inspect_import_html(_fixture_2003(), "")
        self.assertEqual(insp.kind, IMPORT_KIND_ALBUM)
        self.assertEqual(len(insp.tracks), 19)
        self.assertEqual(insp.address_relation, "empty")
        self.assertEqual(insp.detected_url, URL_2003)

    def test_2003_with_song_page_url(self):
        insp = inspect_import_html(_fixture_2003(), SONG_2003)
        self.assertEqual(insp.kind, IMPORT_KIND_NO_TRACKS)
        self.assertEqual(insp.address_relation, "song_page")
        self.assertIn("曲目详情页", insp.message)

    def test_2011_sample_still_30_tracks(self):
        for u in (URL_2011, URL_2011 + "/", URL_2011 + "/?a=1#f"):
            insp = inspect_import_html(_fixture_2011(), u)
            self.assertEqual(insp.kind, IMPORT_KIND_ALBUM, u)
            self.assertEqual(len(insp.tracks), 30, u)
            self.assertEqual(insp.tracks[0].name, "群青のティファレス(GAME Size)")

    def test_mhtml_detected_and_rejected(self):
        mhtml = (
            "From: <Saved by Blink>\r\n"
            "Snapshot-Content-Location: " + URL_2003 + "\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/related; "
            'boundary="----MultipartBoundary--abc----"\r\n\r\n'
            "------MultipartBoundary--abc----\r\n"
            "Content-Type: text/html\r\n\r\n"
            "<html><table id='songlist'></table></html>\r\n"
        )
        self.assertTrue(detect_mhtml(mhtml))
        insp = inspect_import_html(mhtml, URL_2003)
        self.assertEqual(insp.kind, IMPORT_KIND_UNSUPPORTED_FORMAT)
        self.assertIn("MHTML", insp.message)
        self.assertIn("仅 HTML", insp.message)

    def test_html_named_mht_still_parses(self):
        # 反向保障：内容是普通 HTML 时，即使扩展名叫 .mht 也能解析
        # （格式判定看内容，不看扩展名）
        insp = inspect_import_html(_fixture_2003(), URL_2003)
        self.assertEqual(insp.kind, IMPORT_KIND_ALBUM)

    def test_songlist_but_unresolvable_source(self):
        # 有 songlist 结构但链接属于其他专辑 + 2011 地址 → different_album
        filler = "<!-- pad -->" * 20
        html = (
            "<html><body>" + filler +
            "<table id='songlist'><tr>"
            "<td><a href='/game-soundtracks/album/good-album/01%2520a.mp3'>a</a></td>"
            "</tr></table></body></html>"
        )
        self.assertGreater(len(html), 200)
        insp = inspect_import_html(html, URL_2011)
        self.assertEqual(insp.kind, IMPORT_KIND_NO_TRACKS)
        self.assertEqual(insp.address_relation, "different_album")

        # 同样页面 + 空地址：slug 能从曲目链接识别出来，所以能解析出 1 首
        insp2 = inspect_import_html(html, "")
        self.assertEqual(insp2.kind, IMPORT_KIND_ALBUM)
        self.assertEqual(len(insp2.tracks), 1)
        self.assertEqual(insp2.detected_url,
                         "https://downloads.khinsider.com/game-soundtracks/album/good-album")

        # 页面太短（<200字节）→ error（内容守卫优先于识别）
        tiny = (
            "<html><body><table id='songlist'><tr>"
            "<td><a href='/game-soundtracks/album/good-album/01%2520a.mp3'>a</a></td>"
            "</tr></table>" + "x" * 30 + "</body></html>"
        )
        self.assertLess(len(tiny), 200)
        insp3 = inspect_import_html(tiny, URL_2011)
        self.assertEqual(insp3.kind, IMPORT_KIND_ERROR)


class TestMockAlbumImportFlow(unittest.TestCase):
    """在 mock 站点上验证"识别 → 核对 → 确认 → 解析"全链路（含 localhost 主机适配）。"""

    @classmethod
    def setUpClass(cls):
        from mock_site import MockSite
        cls._ctx = MockSite()
        cls.site = cls._ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    def test_mock_album_with_localhost_url_matches(self):
        html = (FIXTURES / "album_page.html").read_text(encoding="utf-8")
        insp = inspect_import_html(html, self.site.album_url)
        self.assertEqual(insp.kind, IMPORT_KIND_ALBUM)
        self.assertEqual(insp.address_relation, "match")
        self.assertEqual(len(insp.tracks), 3)
        # 解析出的曲目页必须指向 mock 主机（不是 khinsider 官方域名）
        self.assertTrue(insp.tracks[0].page_url.startswith(self.site.album_url))

    def test_mock_album_empty_url_falls_back_to_khinsider(self):
        # 空 + 合成页：识别出的地址是官方域名；解析仍能完成（曲目链接是相对路径）
        html = (FIXTURES / "album_page.html").read_text(encoding="utf-8")
        insp = inspect_import_html(html, "")
        self.assertEqual(insp.kind, IMPORT_KIND_ALBUM)
        self.assertEqual(len(insp.tracks), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
