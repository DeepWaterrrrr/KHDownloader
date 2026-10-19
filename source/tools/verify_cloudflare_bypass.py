# -*- coding: utf-8 -*-
"""verify_cloudflare_bypass.py —— 验证"防火墙（Cloudflare 人机校验）问题"真的修好了

这个脚本回答一个问题：**同样的机器、同样的时刻，凭什么说问题修好了？**
所以它跑三组对照，而且三组用的是同一个专辑地址、同一个会话工厂：

    A 组（对照组，复现老毛病）
        裸 requests + 现在的 User-Agent —— 也就是 v008 的行为。
        期望：403 + cf-mitigated: challenge（就是用户当初被挡住的样子）。

    B 组（修复组）
        项目自己的 make_session() + fetch_html() —— v1.0.0 的正式代码路径。
        期望：200 + 完整曲目表（30 首），并且报告用了哪一级指纹。

    C 组（关闭开关，验证"没装 curl_cffi 时会怎样"）
        KHD_TRANSPORT=requests 强制退回普通请求。
        期望：不崩、不误报"没找到曲目"，而是抛 SiteBlockedError，
              且提示里写清了"指纹 / 真浏览器 / 本机浏览器"三层补救办法。

最后再真下第一首（几 MB），确认换指纹之后**下载通路**也没被牵连。

用法：
    python tools/verify_cloudflare_bypass.py [专辑地址]
    python tools/verify_cloudflare_bypass.py --no-download     # 跳过下载（省流量）

退出码：0 = 全部通过；1 = 有失败项。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

import khdownloader_transport as transport  # noqa: E402
from khdownloader_core import (  # noqa: E402
    DownloaderError,
    NoTracksFoundError,
    SiteBlockedError,
    USER_AGENT,
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
BROWSER_LIKE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    album_url = args[0] if args else DEFAULT_ALBUM
    want_download = "--no-download" not in sys.argv

    checks: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        checks.append((label, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  —— {detail}" if detail else ""))

    print("=" * 78)
    print("防火墙（Cloudflare）修复验证")
    print("专辑：", album_url)
    print("=" * 78)

    info = transport.describe()
    print("\n[0] 当前传输层状态")
    print(f"    模式 mode            : {info['mode']}")
    print(f"    curl_cffi 可用       : {info['curl_cffi']}")
    print(f"    playwright 可用      : {info['playwright']}")
    print(f"    本机 Chrome/Edge     : {info['system_browser'] or '（没找到）'}")
    print(f"    真浏览器兜底开关     : {info['browser_fallback']}")
    print(f"    指纹尝试顺序         : {' -> '.join(info['ladder'])}")
    check("curl_cffi 已就位（这一层就是修复的关键）", bool(info["curl_cffi"]),
          "没装的话：pip install curl_cffi")

    # ---------------------------------------------------------------- A 组
    print("\n[A] 对照组：裸 requests + 普通 UA（= v008 的老行为）")
    bare = requests.Session()
    bare.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    try:
        old = bare.get(album_url, timeout=(10, 30))
        old_status = old.status_code
        mitigated = old.headers.get("cf-mitigated", "")
        challenge_like = transport.looks_like_challenge(old)
    except requests.RequestException as exc:
        old_status, mitigated, challenge_like = 0, "", False
        print(f"    （裸请求直接异常：{type(exc).__name__}: {exc}）")
    finally:
        bare.close()
    print(f"    HTTP {old_status} | cf-mitigated: {mitigated or '(无)'} | 像校验页: {challenge_like}")
    check("老毛病可复现（说明这次修复不是空谈）",
          old_status != 200 or challenge_like,
          "裸 requests 拿不到专辑页" if (old_status != 200 or challenge_like) else "居然能拿到，站点可能放宽了")

    # 顺手验一次"只改 UA 有没有用"——这是最常见的误判。
    try:
        ua_only = requests.Session()
        ua_only.headers.update({"User-Agent": BROWSER_LIKE_UA})
        probe = ua_only.get(album_url, timeout=(10, 30))
        print(f"    （只把 UA 换成浏览器 UA：HTTP {probe.status_code}，"
              f"cf-mitigated: {probe.headers.get('cf-mitigated') or '(无)'}）")
        ua_only.close()
    except requests.RequestException as exc:
        print(f"    （只换 UA 的探测异常：{type(exc).__name__}）")

    # ---------------------------------------------------------------- B 组
    print("\n[B] 修复组：项目自己的 make_session() + fetch_html()")
    tiers: list[str] = []
    session = make_session(on_log=lambda text: print(f"    · {text}"))
    html = ""
    tracks: list = []
    try:
        html = fetch_html(session, album_url)
        tier = getattr(session, "transport_tier", "?")
        tiers.append(tier)
        print(f"    实际使用传输层：{tier}")
        check("专辑页拿到了（HTTP 200 + 真实 HTML）", len(html) > 5000, f"{len(html)} 字符")
        tracks = parse_album_page(html, album_url)
        check("解析出完整曲目表", len(tracks) == EXPECTED_TRACK_COUNT,
              f"{len(tracks)} 首（期望 {EXPECTED_TRACK_COUNT}）")
        check("曲目顺序/曲名正确",
              bool(tracks) and tracks[0].name == "群青のティファレス(GAME Size)"
              and tracks[1].name == "Crisscross",
              repr(tracks[0].name) if tracks else "（空）")
        check("用的是指纹层（而不是碰巧站点放行）", tier.startswith("impersonate:"), tier)

        # 详情页 + MP3 地址：证明"过了防火墙"之后整条链路都通。
        detail_errors: list[str] = []
        mp3_urls: list[str] = []
        for track in tracks:
            try:
                page = fetch_html(session, track.page_url)
                mp3_urls.append(parse_song_page(page, track.page_url))
            except DownloaderError as exc:
                detail_errors.append(f"{track.name}: {exc}")
        check("逐首详情页 -> MP3 地址全部解析成功",
              len(mp3_urls) == len(tracks), f"{len(mp3_urls)}/{len(tracks)}")
        if detail_errors:
            for line in detail_errors[:3]:
                print(f"    ! {line}")

        # ------------------------------------------------------------ C 组
        print("\n[C] 关掉指纹层（KHD_TRANSPORT=requests）：必须给出可执行的提示，而不是崩")
        os.environ["KHD_TRANSPORT"] = "requests"
        os.environ["KHD_BROWSER_FALLBACK"] = "0"  # 别在这组里开浏览器
        plain_session = make_session(on_log=lambda text: print(f"    · {text}"))
        try:
            try:
                fetch_html(plain_session, album_url)
                check("退回普通请求时应当被判成「被站点拦住」", False, "居然成功了？")
            except SiteBlockedError as exc:
                message = str(exc)
                print("    提示原文：")
                for line in message.splitlines():
                    print(f"      {line}")
                check("抛的是 SiteBlockedError（不是 NoTracksFoundError）", True)
                check("提示里写明了指纹这一层", "指纹" in message)
                check("提示里写明了真浏览器兜底", "真浏览器" in message)
                check("提示里没有把 cf_clearance 这种内部名词甩给用户",
                      "cf_clearance" not in message.lower())
            except NoTracksFoundError as exc:
                check("被拦时不能报「没找到曲目」", False, f"误报成 NoTracksFoundError：{exc}")
        finally:
            plain_session.close()
            os.environ.pop("KHD_TRANSPORT", None)
            os.environ.pop("KHD_BROWSER_FALLBACK", None)

        # ------------------------------------------------------------ 下载通路
        if want_download and mp3_urls:
            print("\n[D] 真实下载第一首（确认换指纹没把下载通路搞坏）")
            with tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                name = sanitize_filename(tracks[0].name)
                if not name.lower().endswith(".mp3"):
                    name += ".mp3"
                dest, written, skipped = download_file(session, mp3_urls[0], folder, name)
                size = dest.stat().st_size if dest.exists() else 0
                with open(dest, "rb") as handle:
                    head = handle.read(3)
                check("文件落盘且字节数吻合", dest.exists() and written == size,
                      f"{size} 字节")
                check("文件头是 ID3", head == b"ID3", repr(head))
                check("没有 .part 残留", not list(folder.glob("*.part")))
        elif not want_download:
            print("\n[D] 已按要求跳过真实下载（--no-download）")
    finally:
        session.close()

    passed = sum(1 for _, ok, _ in checks if ok)
    print("\n" + "=" * 78)
    print(f"结果：{passed}/{len(checks)} 项通过")
    if passed != len(checks):
        print("失败项：")
        for label, ok, detail in checks:
            if not ok:
                print(f"  - {label}  {detail}")
    print("=" * 78)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
