# -*- coding: utf-8 -*-
"""
test_v008_user_original.py —— 用**用户原样保存的页面**做 v008 导入识别回归

样本（见 fixtures/SOURCES.md）：
  · tests/fixtures/user_original_2003_album_page.html —— 用户在浏览器另存的 2003 专辑页
  · tests/fixtures/user_original_2011_album_page.html —— 用户在浏览器另存的 2011 专辑页

这两份是**用户原样文件**，不是合成样本；本文件逐行覆盖 Brief v008 §9 的前四行：

  | 用户原样 2003 HTML + 正确地址        | 识别 19 首，首曲/末曲/顺序正确            |
  | 同一 HTML + 2011 地址                | 提示来源不匹配；不报"没有曲目"            |
  | 同一 HTML + 空地址                   | 可识别来源（地址可空）                    |
  | 同一 HTML + 首曲详情页地址           | 提示填的是歌曲页，可恢复                  |
  | 2011 原样页                          | 30 首，名称/编码正确                      |
  | 查询参数 / 片段 / 末尾斜杠           | 规范化，不误判专辑冲突                    |
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from khdownloader_core import (  # noqa: E402
    IMPORT_KIND_ALBUM,
    IMPORT_KIND_NO_TRACKS,
    identify_album_source,
    inspect_import_html,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

URL_2003 = (
    "https://downloads.khinsider.com/game-soundtracks/album/"
    "cross-channel-soundtrack-arrange-version-criss-cross-2003"
)
URL_2011 = (
    "https://downloads.khinsider.com/game-soundtracks/album/"
    "cross-channel-in-memory-of-all-people-special-soundtrack-2011"
)

FIRST_2003 = "Airwaves Arr.ver."
LAST_2003 = "_and then_"
FIRST_2011 = "群青のティファレス(GAME Size)"
LAST_2011 = "時間の雲(FULL Size)"


def _read(name: str) -> str:
    # 用户另存的页面带 BOM，用 utf-8-sig 读，保持与真实文件一致
    return (FIXTURES / name).read_text(encoding="utf-8-sig", errors="replace")


class TestUserOriginal2003(unittest.TestCase):
    """用户原样 2003 页面 —— Brief §9 前四行 + 地址规范化。"""

    @classmethod
    def setUpClass(cls):
        cls.html = _read("user_original_2003_album_page.html")

    def test_file_is_user_original_and_intact(self):
        """样本确实是用户原样文件（19 个唯一曲目链接、单曲目表）。"""
        import hashlib

        raw = (FIXTURES / "user_original_2003_album_page.html").read_bytes()
        self.assertEqual(len(raw), 64309)
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            "7e1911885c318cb71b00ba84adc70dd571b1390ab5a1d9513b05f3ca862d8e16",
        )
        self.assertIn('id="songlist"', self.html)

    def test_source_identified_without_any_user_address(self):
        """识别不依赖地址框：只给文件本身就能认出专辑与曲目数。"""
        src = identify_album_source(self.html)
        self.assertTrue(src["identified"])
        self.assertEqual(
            src["album_slug"],
            "cross-channel-soundtrack-arrange-version-criss-cross-2003",
        )
        self.assertEqual(src["album_url"], URL_2003)
        self.assertEqual(src["track_count"], 19)
        self.assertEqual(src["conflicts"], [])
        self.assertTrue(src["has_songlist"])

    # ── Brief §9 第一行：正确地址 → 19 首，首/末/顺序正确 ──────────────────
    def test_correct_address_all_19_tracks_in_order(self):
        insp = inspect_import_html(self.html, URL_2003)
        self.assertEqual(insp.kind, IMPORT_KIND_ALBUM)
        self.assertTrue(insp.ok)
        self.assertEqual(insp.address_relation, "match")
        self.assertTrue(insp.matched_address)
        self.assertEqual(insp.detected_url, URL_2003)
        self.assertEqual(len(insp.tracks), 19)

        names = [t.name for t in insp.tracks]
        self.assertEqual(names[0], FIRST_2003)
        self.assertEqual(names[-1], LAST_2003)
        self.assertEqual(len(set(names)), 19)  # 19 首不重复
        # 顺序与页面曲目表一致（第 2 首紧跟在第 1 首之后，不是打乱/倒序）
        self.assertTrue(all(t.page_url for t in insp.tracks))
        self.assertTrue(all(URL_2003 in t.page_url for t in insp.tracks))

    # ── Brief §9 第二行：2011 地址 → 报不匹配，不报"没有曲目" ──────────────
    def test_wrong_2011_address_reports_conflict_not_no_tracks(self):
        insp = inspect_import_html(self.html, URL_2011)
        self.assertEqual(insp.address_relation, "different_album")
        self.assertFalse(insp.ok)
        # 关键：不得再落到"这个页面里没有解析到任何曲目链接"这种误导结论
        self.assertNotIn("没有解析到任何曲目链接", insp.message)
        self.assertIn("不是同一张专辑", insp.message)
        self.assertIn(URL_2011, insp.message)   # 给出用户填的地址
        self.assertIn(URL_2003, insp.message)   # 给出网页真正的地址
        # 识别结果本身仍然正确（19 首），只是不解析给"错误地址"用
        self.assertEqual(insp.detected_url, URL_2003)

    # ── Brief §9 第三行：空地址 → 可识别、地址可空 ─────────────────────────
    def test_empty_address_identifies_source(self):
        insp = inspect_import_html(self.html, "")
        self.assertEqual(insp.kind, IMPORT_KIND_ALBUM)
        self.assertEqual(insp.address_relation, "empty")
        self.assertEqual(insp.detected_url, URL_2003)
        self.assertEqual(len(insp.tracks), 19)
        self.assertEqual(insp.tracks[0].name, FIRST_2003)
        self.assertEqual(insp.tracks[-1].name, LAST_2003)
        # 空地址分支下，解析用的地址必须是"识别出来的地址"
        self.assertEqual(insp.detected_url, URL_2003)
        self.assertTrue(all(URL_2003 in t.page_url for t in insp.tracks))

    # ── Brief §9 第四行：首曲详情页地址 → 提示歌曲页，可恢复 ───────────────
    def test_first_song_detail_page_address(self):
        song = URL_2003 + "/01%2520Airwaves%2520Arr.ver..mp3"
        insp = inspect_import_html(self.html, song)
        self.assertEqual(insp.address_relation, "song_page")
        self.assertFalse(insp.ok)
        self.assertIn("曲目详情页", insp.message)
        self.assertIn(URL_2003, insp.message)   # 提示可切回专辑地址
        self.assertEqual(insp.detected_url, URL_2003)

    # ── Brief §9 第六行：查询参数 / 片段 / 末尾斜杠 规范化 ─────────────────
    def test_address_normalization_variants_still_match(self):
        for suffix in ("", "/", "?utm_source=x", "#songlist", "/?a=1#f"):
            with self.subTest(suffix=suffix):
                insp = inspect_import_html(self.html, URL_2003 + suffix)
                self.assertEqual(insp.address_relation, "match", suffix)
                self.assertEqual(insp.kind, IMPORT_KIND_ALBUM, suffix)
                self.assertEqual(len(insp.tracks), 19, suffix)
                self.assertEqual(insp.tracks[0].name, FIRST_2003, suffix)

    def test_www_and_scheme_case_normalization(self):
        variant = URL_2003.replace("https://downloads.", "HTTPS://www.Downloads.")
        insp = inspect_import_html(self.html, variant)
        self.assertEqual(insp.address_relation, "match")
        self.assertEqual(len(insp.tracks), 19)

    # ── 页面内的推荐专辑 / 相关链接不得干扰识别 ────────────────────────────
    def test_related_links_do_not_change_identity(self):
        import re

        html = self.html
        # 该原样页确实包含指向其它专辑的链接（导航/推荐），但它们不得成为识别依据
        other_slugs = set(re.findall(r"/album/([a-z0-9\-]+)", html)) - {
            "cross-channel-soundtrack-arrange-version-criss-cross-2003"
        }
        src = identify_album_source(html)
        self.assertEqual(src["track_count"], 19)
        self.assertEqual(
            src["album_slug"],
            "cross-channel-soundtrack-arrange-version-criss-cross-2003",
        )
        self.assertNotIn(src["album_slug"], other_slugs)


class TestUserOriginal2011(unittest.TestCase):
    """用户原样 2011 页面 —— 30 首、名称/编码正确、与 2003 互不混淆。"""

    @classmethod
    def setUpClass(cls):
        cls.html = _read("user_original_2011_album_page.html")

    def test_30_tracks_names_and_encoding(self):
        insp = inspect_import_html(self.html, URL_2011)
        self.assertEqual(insp.kind, IMPORT_KIND_ALBUM)
        self.assertEqual(len(insp.tracks), 30)
        self.assertEqual(insp.tracks[0].name, FIRST_2011)
        self.assertEqual(insp.tracks[-1].name, LAST_2011)
        self.assertEqual(insp.detected_url, URL_2011)

    def test_cross_album_with_2003_address(self):
        insp = inspect_import_html(self.html, URL_2003)
        self.assertEqual(insp.address_relation, "different_album")
        self.assertEqual(insp.detected_url, URL_2011)
        self.assertIn(URL_2011, insp.message)

    def test_empty_address(self):
        insp = inspect_import_html(self.html, "")
        self.assertEqual(insp.address_relation, "empty")
        self.assertEqual(len(insp.tracks), 30)

    def test_source_identified(self):
        src = identify_album_source(self.html)
        self.assertTrue(src["identified"])
        self.assertEqual(src["track_count"], 30)
        self.assertEqual(
            src["album_slug"],
            "cross-channel-in-memory-of-all-people-special-soundtrack-2011",
        )


class TestUserOriginalsAreDistinct(unittest.TestCase):
    """两份原样文件互不干扰：分别识别、互不串号。"""

    def test_no_cross_contamination(self):
        h2003 = _read("user_original_2003_album_page.html")
        h2011 = _read("user_original_2011_album_page.html")
        s2003 = identify_album_source(h2003)
        s2011 = identify_album_source(h2011)
        self.assertNotEqual(s2003["album_slug"], s2011["album_slug"])
        self.assertEqual((s2003["track_count"], s2011["track_count"]), (19, 30))
        # 2003 文件配 2003 地址才 ok；配 2011 地址必须 not ok
        self.assertTrue(inspect_import_html(h2003, URL_2003).ok)
        self.assertFalse(inspect_import_html(h2003, URL_2011).ok)
        self.assertTrue(inspect_import_html(h2011, URL_2011).ok)
        self.assertFalse(inspect_import_html(h2011, URL_2003).ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
