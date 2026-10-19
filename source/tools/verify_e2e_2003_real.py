# -*- coding: utf-8 -*-
"""v008 真实网络端到端：用**用户原样 2003 页面**走"识别 → 核对 → 预览 → 下载首曲"。

与 GUI `_import_worker` 同一条管线（fetch_html → parse_song_page → download_file），
只取第 1 首，验证：
  1. 导入识别 = 19 首（用户原样文件，非合成）；
  2. 下载过程中**不再请求专辑页**（用请求计数器证明"用预览曲目、不再请求专辑页"）；
  3. 首曲真实落盘：大小 / ID3 头 / SHA-256；
  4. 临时产物用完即删，不碰用户任何目录。
"""
import hashlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import khdownloader_core as core  # noqa: E402

URL_2003 = ("https://downloads.khinsider.com/game-soundtracks/album/"
            "cross-channel-soundtrack-arrange-version-criss-cross-2003")
FIXTURE = ROOT / "tests" / "fixtures" / "user_original_2003_album_page.html"

lines = []


def P(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    lines.append(s)


class CountingSession:
    """包一层 requests.Session，记录每个请求 URL（用于证明没有重复请求专辑页）。"""

    def __init__(self, inner):
        self._inner = inner
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self._inner.get(url, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


html = FIXTURE.read_text(encoding="utf-8-sig", errors="replace")
P("样本：", FIXTURE.name, os.path.getsize(FIXTURE), "bytes（用户原样保存页面）")

insp = core.inspect_import_html(html, URL_2003)
P("识别： kind=%s  relation=%s  曲目数=%d" % (insp.kind, insp.address_relation, len(insp.tracks)))
P("首曲：", insp.tracks[0].name, "| 末曲：", insp.tracks[-1].name)
assert insp.kind == core.IMPORT_KIND_ALBUM and len(insp.tracks) == 19
assert insp.tracks[0].name == "Airwaves Arr.ver."

# ---- 用"预览确认后的曲目对象"启动下载（与 GUI _start_confirmed_import 同语义）----
confirmed = {
    "album_url": URL_2003,
    "tracks": list(insp.tracks),
    "title": insp.page_title,
    "source_file": str(FIXTURE),
}

session = CountingSession(core.make_session())
tmp = Path(tempfile.mkdtemp(prefix="khd_v008_2003_"))
P("临时目录：", tmp)
try:
    track = confirmed["tracks"][0]
    P("")
    P("== 第 1 首：%s ==" % track.name)
    page_html = core.fetch_html(session, track.page_url)
    mp3_url = core.parse_song_page(page_html, track.page_url)
    P("  详情页 200，直链主机：", mp3_url.split("/")[2])
    filename = core.sanitize_filename(track.name)
    if not filename.lower().endswith(".mp3"):
        filename += ".mp3"
    dest, written, skipped = core.download_file(session, mp3_url, tmp, filename)
    data = dest.read_bytes()
    P("  结果：status=%s  写入=%d bytes  跳过=%s" % ("completed" if not skipped else "skipped", written, skipped))
    P("  文件：%s  大小=%d  头=%r" % (dest.name, len(data), data[:3]))
    P("  SHA-256：", hashlib.sha256(data).hexdigest())

    P("")
    P("== 请求轨迹（证明没有重复请求专辑页）==")
    for u in session.urls:
        P("  -", u)
    album_page_hits = [u for u in session.urls if u.rstrip("/") == URL_2003.rstrip("/")]
    P("  专辑页请求次数：", len(album_page_hits), "（预期 0：下载只用预览确认的曲目）")
    P("  详情页请求次数：", sum(1 for u in session.urls if "/album/" in u and u.endswith(".mp3") and "//" in u))
finally:
    for p in tmp.iterdir():
        p.unlink()
    tmp.rmdir()
    P("")
    P("临时文件已清理：", not tmp.exists())

P("")
P("== 结论 ==")
P("  用户原样 2003 导入识别：19 首，OK")
P("  预览曲目 → 首曲真实下载：%s" % ("成功" if len(data) > 100_000 and data[:3] == b"ID3" else "未确认"))
P("  下载流程未再次请求专辑页：%s" % ("是" if not album_page_hits else "否"))

out = Path(__file__).resolve().parent / "real_original_2003_download.txt"
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n[saved]", out)
