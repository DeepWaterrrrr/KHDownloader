# -*- coding: utf-8 -*-
"""verify_real_album.py —— 真实站点端到端验证（专辑页 -> 全部曲目 -> 真实 MP3）

这个脚本会真的访问 downloads.khinsider.com：
    1. 打开专辑页，解析出有序、去重的曲目列表；
    2. 逐首打开详情页，解析出真实 MP3 地址；
    3. 只真实下载**第一首**（验证下载通路），其余只解析地址不下载，
       以免给站点造成不必要的压力。

它会自己创建临时目录，跑完自动清理，不在项目里留下任何 mp3。

用法：
    python tools/verify_real_album.py [专辑地址]
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from khdownloader_core import (  # noqa: E402
    DownloaderError,
    download_file,
    fetch_html,
    make_session,
    parse_album_page,
    parse_song_page,
    sanitize_filename,
)

DEFAULT_ALBUM = (
    "https://downloads.khinsider.com/game-soundtracks/album/"
    "cross-channel-in-memory-of-all-people-special-soundtrack-2011"
)

EXPECTED_TRACK_COUNT = 30
EXPECTED_FIRST_TRACK = "群青のティファレス(GAME Size)"
EXPECTED_SECOND_TRACK = "Crisscross"


def main() -> int:
    album_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ALBUM
    session = make_session()
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: str = ""):
        checks.append((label, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  —— {detail}" if detail else ""))

    print("=" * 74)
    print("真实站点端到端验证")
    print("专辑：", album_url)
    print("=" * 74)

    # ---- 第 1 步：专辑页 -> 曲目列表 ----
    print("\n[1] 专辑页 -> 有序曲目列表")
    html = fetch_html(session, album_url)
    tracks = parse_album_page(html, album_url)
    check("专辑页返回真实 HTML 且解析出曲目", len(tracks) > 0, f"{len(tracks)} 首")
    check("曲目数量与站点一致", len(tracks) == EXPECTED_TRACK_COUNT,
          f"期望 {EXPECTED_TRACK_COUNT}，实际 {len(tracks)}")
    check("第 1 首曲名正确", tracks[0].name == EXPECTED_FIRST_TRACK, repr(tracks[0].name))
    check("第 2 首曲名正确", tracks[1].name == EXPECTED_SECOND_TRACK, repr(tracks[1].name))
    check("地址已去重", len({t.page_url for t in tracks}) == len(tracks))
    check("曲名里没有混入时长/大小数字",
          not any(t.name.replace(":", "").replace(".", "").isdigit() for t in tracks))

    # ---- 第 2 步：逐首详情页 -> MP3 地址 ----
    print(f"\n[2] 逐首打开详情页，解析 MP3 地址（共 {len(tracks)} 首，不下载）")
    mp3_urls: list[str] = []
    failures: list[tuple[str, str]] = []
    for i, track in enumerate(tracks, 1):
        try:
            page = fetch_html(session, track.page_url)
            mp3 = parse_song_page(page, track.page_url)
            if not mp3.lower().endswith(".mp3"):
                raise DownloaderError(f"解析出的地址不是 .mp3：{mp3}")
            if urlsplit(mp3).netloc == urlsplit(track.page_url).netloc:
                raise DownloaderError("解析出的地址还在站点主机上，像是详情页而不是音频")
            mp3_urls.append(mp3)
            if i <= 3 or i == len(tracks):
                host = urlsplit(mp3).netloc
                print(f"    {i:3}. {track.name[:36]:38} -> {host}")
        except DownloaderError as exc:
            failures.append((track.name, str(exc).splitlines()[0]))
            print(f"    {i:3}. {track.name[:36]:38} -> 失败：{str(exc).splitlines()[0]}")

    check("全部曲目都解析出 MP3 地址", not failures,
          f"{len(mp3_urls)}/{len(tracks)}" + (f"，失败 {len(failures)} 首" if failures else ""))
    check("所有 MP3 地址都跨主机（不是详情页）",
          all(urlsplit(u).netloc != urlsplit(album_url).netloc for u in mp3_urls))

    # ---- 第 3 步：真实下载第一首 ----
    print("\n[3] 真实下载第一首（验证下载通路）")
    with tempfile.TemporaryDirectory(prefix="khdownloader_verify_") as tmp:
        folder = Path(tmp)
        first = tracks[0]
        filename = sanitize_filename(first.name)
        if not filename.lower().endswith(".mp3"):
            filename += ".mp3"

        data = session.head(mp3_urls[0], timeout=(10, 30), allow_redirects=True)
        remote_size = int(data.headers.get("Content-Length") or 0)
        check("MP3 主机可达且声明了 audio/mpeg",
              data.status_code == 200 and "audio" in (data.headers.get("Content-Type") or ""),
              f"HTTP {data.status_code} / {data.headers.get('Content-Type')} / {remote_size} 字节")

        dest, written, skipped = download_file(session, mp3_urls[0], folder, filename)
        check("文件已完整落盘", dest.exists() and dest.stat().st_size == remote_size,
              f"{dest.stat().st_size} 字节" if dest.exists() else "文件不存在")
        check("写入字节数与远端一致", written == remote_size, f"{written} vs {remote_size}")
        check("没有 .part 残留", not list(folder.glob("*.part")))
        with open(dest, "rb") as handle:
            head = handle.read(3)
        check("文件头是 ID3（真的是 MP3）", head == b"ID3", repr(head))
        check("文件名已净化且带 .mp3 后缀", dest.suffix == ".mp3", dest.name)

    # ---- 汇总 ----
    passed = sum(1 for _, ok, _ in checks if ok)
    print("\n" + "=" * 74)
    print(f"结果：{passed}/{len(checks)} 项通过")
    if failures:
        print("未解析成功的曲目：")
        for name, err in failures:
            print(f"  - {name}: {err}")
    print("=" * 74)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
