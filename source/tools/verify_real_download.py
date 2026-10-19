# -*- coding: utf-8 -*-
"""
verify_real_download.py —— 真实站点的端到端下载验证（手动跑，不在自动测试里）

为什么单独做一个脚本，而不是写进 tests/？
    因为它要真的把一首曲子从网上下载下来（约 3.5 MB）。自动化测试每次都跑一遍
    既慢又不礼貌，所以只在需要取证的时候手动跑一次。

它验证的是 v001 没能完成的那一件事：**真实站点上，一首曲子能不能完整下载下来。**

用法：
    python tools/verify_real_download.py [曲目详情页地址]

不传地址就用默认的参考曲目（CROSS†CHANNEL 专辑第 2 首）。

流程：
    1. 打开真实曲目详情页，记录 HTTP 状态与 Content-Type；
    2. 用 parse_song_page() 解析出真实 MP3 地址；
    3. 先看一眼 MP3 的响应头（不下载内容）；
    4. 用 download_file() 真正下载到一个临时目录；
    5. 逐项核对：字节数、Content-Length 是否一致、有没有 .part 残留、最终文件在不在；
    6. 跑完把临时目录删掉，不留东西。

本脚本不使用任何绕过手段：如果站点返回人机校验，它会如实报告并退出。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from khdownloader_core import (  # noqa: E402
    DownloaderError,
    download_file,
    fetch_html,
    make_session,
    parse_song_page,
)

DEFAULT_PAGE = (
    "https://downloads.khinsider.com/game-soundtracks/album/"
    "cross-channel-in-memory-of-all-people-special-soundtrack-2011/02%2520Crisscross.mp3"
)


def _report(label: str, value, ok: bool | None = None):
    mark = "" if ok is None else ("  [通过]" if ok else "  [不通过]")
    print(f"  {label:22}: {value}{mark}")


def main(argv: list[str]) -> int:
    page_url = argv[1] if len(argv) > 1 else DEFAULT_PAGE
    session = make_session()
    checks: list[bool] = []

    print("=" * 70)
    print("真实站点端到端下载验证")
    print("=" * 70)
    print(f"曲目详情页：{page_url}\n")

    # ---- 1. 打开详情页 ----
    print("[1] 打开曲目详情页")
    try:
        html = fetch_html(session, page_url)
    except DownloaderError as exc:
        print(f"  失败：{exc}")
        return 2
    _report("页面取到", f"{len(html)} 字符", True)
    checks.append(True)
    # 这一条顺带证明：以 .mp3 结尾的详情页地址，返回的确实是 HTML 而不是音频
    is_html = "<html" in html[:200].lower()
    _report("返回的是 HTML（非音频）", is_html, is_html)
    checks.append(is_html)

    # ---- 2. 解析 MP3 地址 ----
    print("\n[2] 解析真实 MP3 地址")
    try:
        mp3_url = parse_song_page(html, page_url)
    except DownloaderError as exc:
        print(f"  失败：{exc}")
        return 2
    _report("MP3 地址", mp3_url)
    from urllib.parse import urlsplit

    page_host = urlsplit(page_url).netloc
    mp3_host = urlsplit(mp3_url).netloc
    absolute = mp3_url.startswith(("http://", "https://"))
    _report("是绝对地址", absolute, absolute)
    _report("是否跨主机", f"{mp3_host} (页面在 {page_host})", mp3_host != page_host)
    checks += [absolute, mp3_host != page_host]

    # ---- 3. 看一眼 MP3 的响应头（先不下内容）----
    print("\n[3] 查看 MP3 响应头（HEAD，不下内容）")
    try:
        head = session.head(mp3_url, timeout=(10, 30), allow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  HEAD 失败：{type(exc).__name__}: {exc}")
        return 2
    _report("HTTP 状态", head.status_code, head.status_code == 200)
    _report("Content-Type", head.headers.get("Content-Type"))
    declared = int(head.headers.get("Content-Length") or 0)
    _report("Content-Length", f"{declared:,} 字节")
    _report("是否发生跳转", f"{len(head.history)} 次")
    checks.append(head.status_code == 200)

    # ---- 4. 真正下载 ----
    print("\n[4] 真实下载（分块流式 + .part + 原子改名）")
    tmp_dir = Path(tempfile.mkdtemp(prefix="khdl-verify-"))
    try:
        try:
            dest, written, skipped = download_file(
                session, mp3_url, tmp_dir, "verify-real-track.mp3"
            )
        except DownloaderError as exc:
            print(f"  失败：{exc}")
            return 2

        _report("是否跳过（已存在）", skipped, skipped is False)
        _report("写入字节数", f"{written:,}")
        _report("文件实际大小", f"{dest.stat().st_size:,}")
        size_ok = written == dest.stat().st_size
        _report("写入数 == 文件大小", size_ok, size_ok)
        declared_ok = (declared == 0) or (written == declared)
        _report("与 Content-Length 一致", declared_ok, declared_ok)

        part = Path(str(dest) + ".part")
        no_part = not part.exists()
        _report("无 .part 残留", no_part, no_part)

        mp3_exists = dest.exists() and dest.name.endswith(".mp3")
        _report("最终 .mp3 已就位", f"{dest.name}", mp3_exists)

        # 读前几个字节，确认拿到的是真音频而不是错误页
        magic = dest.open("rb").read(4)
        looks_audio = magic[:3] == b"ID3" or magic[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xfa")
        _report("文件头像是音频", f"{magic!r}", looks_audio)

        checks += [size_ok, declared_ok, no_part, mp3_exists, looks_audio]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print(f"\n  临时目录已删除：{tmp_dir}")

    # ---- 5. 结论 ----
    print("\n" + "=" * 70)
    passed = sum(1 for c in checks if c)
    print(f"核对项：{passed} / {len(checks)} 通过")
    if passed == len(checks):
        print("结论：真实站点的端到端下载 —— 成功。")
        return 0
    print("结论：存在未通过的核对项，见上方标注。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
