# -*- coding: utf-8 -*-
"""
inspect_site.py —— 站点结构排查小工具

用途：当下载器报"解析不到曲目"或"找不到 MP3 链接"时，用这个脚本把
      页面原始 HTML 存下来，看看站点到底返回了什么。

用法：
    python tools/inspect_site.py <页面地址> [输出文件名]
    python tools/inspect_site.py --probe <专辑地址>     # 只探测连通性，不存文件

例：
    python tools/inspect_site.py "https://downloads.khinsider.com/game-soundtracks/album/xxx"

它会把 HTML 存到 tools/evidence/ 目录下，并打印一份结构摘要：
HTTP 状态码、Content-Type、有没有跳转、页面里的链接形态统计，
以及专辑页/曲目页各自能被解析出什么。

注意：本工具只做"观察"，不含任何绕过访问控制的手段。
     如果站点返回 Cloudflare 人机校验页，它会明确告诉你，并把那一页存下来作为证据。
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from re import sub
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from khdownloader_core import (  # noqa: E402
    NoMp3LinkError,
    NoTracksFoundError,
    _looks_like_challenge,
    make_session,
    parse_album_page,
    parse_song_page,
)

EVIDENCE_DIR = Path(__file__).resolve().parent / "evidence"


def _safe_name(url: str) -> str:
    """把地址变成一个安全的文件名。"""
    tail = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] or "page"
    return sub(r"[^A-Za-z0-9._-]", "_", tail)[:60] + ".html"


def probe(url: str) -> int:
    """请求一个地址，打印观察结果，并把 HTML 存下来。"""
    session = make_session()
    print(f"请求：{url}")
    try:
        response = session.get(url, timeout=(10, 30))
    except Exception as exc:  # noqa: BLE001
        print(f"  请求失败：{type(exc).__name__}: {exc}")
        return 1

    print(f"  HTTP 状态码      : {response.status_code}")
    print(f"  Content-Type     : {response.headers.get('Content-Type')}")
    print(f"  最终地址         : {response.url}")
    print(f"  发生跳转         : {bool(response.history)}"
          + (f" -> {[h.status_code for h in response.history]}" if response.history else ""))
    print(f"  cf-mitigated     : {response.headers.get('cf-mitigated')}")
    print(f"  字节数           : {len(response.content)}")

    blocked = response.status_code == 403 and _looks_like_challenge(response)
    if blocked:
        print("  >>> 这是 Cloudflare 人机校验页（普通程序请求被拦截），不是页面本身。")

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EVIDENCE_DIR / _safe_name(url)
    out_path.write_bytes(response.content)
    print(f"  已保存到         : {out_path.relative_to(ROOT)}")

    if blocked:
        return 2

    html = response.text
    try:
        import bs4  # noqa: F401
    except ImportError:
        print("  （没装 beautifulsoup4，跳过结构分析）")
        return 0

    # ---- 链接形态统计：看清站点用的是绝对地址还是根相对地址 ----
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    hrefs = [a["href"].strip() for a in soup.find_all("a", href=True) if a["href"].strip()]
    shapes = Counter()
    for href in hrefs:
        if href.startswith("http://") or href.startswith("https://"):
            shapes["绝对地址 (http...)" ] += 1
        elif href.startswith("/"):
            shapes["根相对地址 (/...)" ] += 1
        else:
            shapes["相对地址 (不含 /)" ] += 1
    print(f"  页面链接总数     : {len(hrefs)}")
    for shape, count in shapes.most_common():
        print(f"      {shape:22} {count}")

    print(f"  是否含 #songlist : {soup.find('table', id='songlist') is not None}")

    # ---- 试着按专辑页 / 曲目页各解析一次 ----
    try:
        tracks = parse_album_page(html, url)
        print(f"  [专辑页解析] 成功，共 {len(tracks)} 首：")
        for index, track in enumerate(tracks[:5], start=1):
            print(f"      {index:2}. {track.name}  ->  {track.page_url}")
        if len(tracks) > 5:
            print(f"      ...（后面还有 {len(tracks) - 5} 首）")
    except NoTracksFoundError:
        print("  [专辑页解析] 没解析到曲目（这可能本来就是曲目页，不是专辑页）")

    try:
        mp3 = parse_song_page(html, url)
        print(f"  [曲目页解析] 成功，MP3 地址：{mp3}")
    except NoMp3LinkError:
        print("  [曲目页解析] 这个页面里没有 MP3 链接")

    return 0


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--probe"]
    if not args:
        print(__doc__)
        return 1
    return probe(args[0])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
