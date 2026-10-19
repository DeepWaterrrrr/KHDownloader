# -*- coding: utf-8 -*-
"""用用户原样保存的 2003 / 2011 专辑页面，对 v008 导入识别做实跑验证。

样本查找顺序：
  1) `<项目>/tests/fixtures/user_original_*_album_page.html`（仓库自带，可复现）；
  2) 环境变量 `KHD_ORIG_DIR` 指向的目录（想把"浏览器另存为"的原始目录也拿来比对时设置）。
只读样本，不修改任何原始文件。
"""
import io
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

import khdownloader_core as core  # noqa: E402

_FIX = os.path.join(_ROOT, "tests", "fixtures")
_ORIG = os.environ.get("KHD_ORIG_DIR", "")

P2003 = next(p for p in (
    os.path.join(_FIX, "user_original_2003_album_page.html"),
    os.path.join(_ORIG, "3", "CROSS\u2020CHANNEL Soundtrack Arrange Version CRISS\u2020CROSS (2003) MP3 - Download Soundtracks for FREE!.html"),
) if os.path.exists(p))
P2011 = next(p for p in (
    os.path.join(_FIX, "user_original_2011_album_page.html"),
    os.path.join(_ORIG, "CROSS\u2020CHANNEL ~In memory of all people~ SPECIAL SOUNDTRACK (2011) MP3 - Download Soundtracks for FREE!.html"),
) if os.path.exists(p))

URL2003 = "https://downloads.khinsider.com/game-soundtracks/album/cross-channel-soundtrack-arrange-version-criss-cross-2003"
URL2011 = "https://downloads.khinsider.com/game-soundtracks/album/cross-channel-in-memory-of-all-people-special-soundtrack-2011"

out = io.StringIO()


def P(*a):
    print(*a)
    print(*a, file=out)


def load(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        return f.read()


for label, path in (("2003", P2003), ("2011", P2011)):
    P("=" * 78)
    P(f"[{label}] 文件：{os.path.basename(path)}")
    P(f"[{label}] 存在：{os.path.exists(path)}  大小：{os.path.getsize(path) if os.path.exists(path) else '-'} bytes")
    P(f"[{label}] SHA-256：{__import__('hashlib').sha256(open(path,'rb').read()).hexdigest() if os.path.exists(path) else '-'}")

html2003 = load(P2003)
html2011 = load(P2011)

P("")
P("### identify_album_source（不依赖任何用户输入地址）")
for label, html in (("2003", html2003), ("2011", html2011)):
    src = core.identify_album_source(html)
    P(f"-- {label} --")
    for k in ("identified", "album_slug", "album_url", "canonical_url", "title",
              "track_count", "has_songlist", "evidence", "conflicts"):
        P(f"   {k} = {src.get(k)!r}")

P("")
P("### inspect_import_html 场景矩阵（原样文件）")


def run(label, path, cases):
    html = load(path)
    for name, addr in cases:
        ins = core.inspect_import_html(html, addr)
        tracks = getattr(ins, "tracks", None) or []
        first = tracks[0].name if tracks else "-"
        last = tracks[-1].name if tracks else "-"
        P(f"[{label}] {name}")
        P(f"    kind={ins.kind!r}  relation={getattr(ins,'address_relation',None)!r}")
        P(f"    tracks={len(tracks)}  first={first!r}  last={last!r}")
        P(f"    matched={getattr(ins,'matched_address',None)!r}  detected={getattr(ins,'detected_url',None)!r}")
        P(f"    message={ins.message!r}")


first_song_2003 = "https://downloads.khinsider.com/game-soundtracks/album/cross-channel-soundtrack-arrange-version-criss-cross-2003/01.%20Airwaves%20Arr.ver..mp3"

run("2003", P2003, [
    ("正确地址", URL2003),
    ("2011 错误地址", URL2011),
    ("空地址", ""),
    ("首曲详情页地址", first_song_2003),
    ("正确地址+查询参数", URL2003 + "?utm=1"),
    ("正确地址+末尾斜杠", URL2003 + "/"),
    ("正确地址+片段", URL2003 + "#songlist"),
])

run("2011", P2011, [
    ("正确地址", URL2011),
    ("2003 错误地址", URL2003),
    ("空地址", ""),
])

P("")
P("### 内容特征")
for label, html in (("2003", html2003), ("2011", html2011)):
    import re
    plinks = re.findall(r'href="([^"]*/album/[^"]*\.mp3)"', html)
    slugs = {}
    for h in plinks:
        m = re.search(r"/album/([^/]+)/", h)
        if m:
            slugs[m.group(1)] = slugs.get(m.group(1), 0) + 1
    has_songlist = ('id="songlist"' in html) or ("id=songlist" in html)
    P("[%s] songlist表=%s  曲目链接=%d  唯一slug=%d  %s"
      % (label, has_songlist, len(plinks), len(slugs), slugs))

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "real_original_verification.txt"), "w", encoding="utf-8") as f:
    f.write(out.getvalue())
print("\n[saved] real_original_verification.txt")
