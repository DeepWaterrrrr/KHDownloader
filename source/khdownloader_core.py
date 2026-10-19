# -*- coding: utf-8 -*-
"""
khdownloader_core.py —— 下载器的"核心逻辑"（不含界面）

这个文件只做三件事，每件事都写成独立的小函数，方便单独看懂、单独测试：

  1. 发 HTTP 请求        （默认走 khdownloader_transport：浏览器 TLS 指纹，见该文件）
  2. 解析网页找链接      （用 BeautifulSoup）
  3. 把 MP3 下载到硬盘   （分块流式写文件，先写 .part 再改名）

界面代码在 khdownloader_gui.py 里，本文件完全不 import tkinter。

v1.0.0 变更（"防火墙问题"的修复）：请求不再直接由 requests 发出，而是经过
`khdownloader_transport` 这一层 —— 它给 https 请求换上真实浏览器的 TLS/HTTP2 指纹
（站点识别的是网络指纹，不是 User-Agent），并在仍然被拦时逐级升级
（换指纹 → 真浏览器会话 → 直接用浏览器渲染出的页面）。本文件里的
"错误分类"逻辑一行没变：升级之后还是被拦，才照旧抛 SiteBlockedError。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

import khdownloader_transport as transport

# ---------------------------------------------------------------------------
# 全局常量（想调整行为，改这里就够了）
# ---------------------------------------------------------------------------

#: 浏览器标识。用普通的浏览器 UA 是因为站点会拒绝明显是脚本的默认 UA，
#: 这属于"礼貌地表明自己是谁"，不是绕过任何访问控制。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

#: (连接超时, 读取超时) 秒。读取超时给得宽一点，因为大文件传输会慢。
TIMEOUT = (10, 30)

#: 每次从网络读多少字节。64 KB 是常见的大小，够快也不会一次吃满内存。
CHUNK_SIZE = 64 * 1024

#: 两次请求之间歇多久（秒），避免给站点造成压力。
SLEEP_BETWEEN_REQUESTS = 0.4

#: 站点根地址。只用来做默认值和提示，代码不依赖它来做链接拼接。
SITE_ROOT = "https://downloads.khinsider.com"


# ---------------------------------------------------------------------------
# 错误类型：界面拿到这些错误就能直接显示人话
# ---------------------------------------------------------------------------


class DownloaderError(Exception):
    """本工具所有"能读懂"的错误都继承自它。"""


class SiteBlockedError(DownloaderError):
    """站点返回了人机校验（Cloudflare challenge）页面，普通程序请求拿不到内容。

    判据是**明确的校验特征**（`cf-mitigated: challenge` 头，或页面含
    "Just a moment..." / "challenges.cloudflare.com"），**不是**只看 403。
    """


class AccessDeniedError(DownloaderError):
    """站点直接拒绝访问（例如普通 403），但**没有**人机校验页面特征。

    之所以要和 SiteBlockedError 分开：403 可能是"校验页"（要用户去浏览器过验证），
    也可能是"访问被拒绝"（路径不存在、地区限制、被拉黑等）。两者的下一步操作不同，
    不能一律说成"人机校验"。
    """


class RateLimitError(DownloaderError):
    """站点限流（HTTP 429）。提示用户稍后再试，而不是去浏览器过验证。"""


class NotAudioContentError(DownloaderError):
    """下载链接返回的内容不是音频（例如 HTML 错误页/校验页）。

    这是文件安全的关键一环：宁可拒收，也不能把一段 HTML 当成 MP3 存下来。
    """


class NetworkError(DownloaderError):
    """网络层失败：连不上、超时、传输中途断开。"""


class NoTracksFoundError(DownloaderError):
    """专辑页打开成功，但一首曲目都没解析出来。"""


class NoMp3LinkError(DownloaderError):
    """曲目详情页里找不到 MP3 链接。"""


class CancelledError(DownloaderError):
    """用户主动取消了下载。

    它继承 DownloaderError，所以"忘了单独处理取消"的地方也不会崩；
    但界面应当把它和真正的失败区分开 —— 用户自己按的取消，不是出错。
    """


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Track:
    """一首曲目：名字 + 详情页地址。"""

    name: str
    page_url: str


@dataclass
class TrackResult:
    """一首曲目的处理结果，用于最后出总结。

    status 取值（按"这一首最后怎么样了"分类，界面直接照着它计数）：
      "completed" —— 真的下载完成了；
      "skipped"   —— 文件已存在且大小一致，没重复下；
      "failed"    —— 出错了（网络 / HTTP / 解析 / 写盘）；
      "cancelled" —— 用户取消，这一首没做完。
    """

    name: str
    status: str
    message: str
    path: Optional[Path] = None

    @property
    def ok(self) -> bool:
        """这一首算不算"顺利"（完成或跳过都算顺利，不作失败计）。"""
        return self.status in ("completed", "skipped")


#: 汇总用的四个计数键，顺序固定，方便界面直接照着显示。
RESULT_STATUSES = ("completed", "skipped", "failed", "cancelled")


def summarize_results(results: list["TrackResult"]) -> dict[str, int]:
    """把结果列表数成一份 {completed, skipped, failed, cancelled} 计数。

    界面单独用这个函数出总结，就不会出现"把取消算成成功"这种误导。
    """
    counts = {status: 0 for status in RESULT_STATUSES}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# 1. 网络请求
# ---------------------------------------------------------------------------


def make_session(on_log: Optional[Callable[[str], None]] = None) -> requests.Session:
    """建一个带默认请求头的 Session。

    复用同一个 Session 的好处：TCP 连接会被复用，下载多首曲目时更快，
    也给站点留下"是同一个人连续访问"的正常印象。

    v1.0.0：实际由 khdownloader_transport 构造 —— 返回的仍是 requests.Session 的子类，
    但对 https 请求会换上真实浏览器的 TLS/HTTP2 指纹（这就是"防火墙问题"的正解：
    站点拦的是网络指纹，不是 User-Agent）。装上 curl_cffi 就用指纹层，
    没装就自动退回普通的 requests，程序行为与 v008 一致。

    on_log 可选：升级指纹/走浏览器兜底时会往这里发一行说明（界面拿它写日志）。
    """
    session = transport.make_session(on_log=on_log)
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def _looks_like_challenge(response: requests.Response) -> bool:
    """判断这个响应是不是 Cloudflare 的人机校验页。

    两个判据任意命中即可：响应头里的 cf-mitigated: challenge，
    或者页面标题/正文里出现 "Just a moment..." / "challenges.cloudflare.com"。
    """
    if response.headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    head = response.text[:2000].lower()
    return "<title>just a moment" in head or "challenges.cloudflare.com" in head


def _looks_like_challenge_html(html: str, headers: Optional[dict] = None) -> bool:
    """同上的判断，但对**已经拿到的 HTML 文本**做（用于导入本地保存的页面）。

    导入的 HTML 没有 requests 的响应对象，所以单独提供这个纯文本版本。
    headers 可选，若调用方还留着原始响应头，可以一并判断 cf-mitigated。
    """
    if headers and str(headers.get("cf-mitigated", "")).lower() == "challenge":
        return True
    head = (html or "")[:2000].lower()
    return "<title>just a moment" in head or "challenges.cloudflare.com" in head


def _site_blocked_error(status_code: int, tier: str = "") -> "SiteBlockedError":
    """人机校验最终没过去时的错误（说明已经自动试过哪几层）。

    v1.0.0：措辞与 v008 不同 —— v008 说的是"本工具不会去绕过这道保护"，
    而现在程序会**自动**逐级升级（浏览器指纹 → 真浏览器会话 → 浏览器渲染页面）。
    只有三层都没过去，才轮到这条错误。
    """
    suffix = f"\n  （本轮已尝试的升级层：{tier}）" if tier else ""
    return SiteBlockedError(
        f"站点拒绝了来自当前网络环境的程序化访问（Cloudflare 人机校验，HTTP {status_code}）。\n"
        "  请注意，这和“没找到曲目”完全是两回事：\n"
        "    · 没找到曲目 = 页面拿到了，但解析不出曲目；\n"
        "    · 现在这种情况 = 页面根本没拿到，被站点的保护机制挡在门外。\n"
        "  这不是代码写错，也不是断网。程序已经自动试过三层办法，都没过去：\n"
        "    ① 用真实 Chrome 的 TLS/HTTP2 指纹发请求（curl_cffi）；\n"
        "    ② 轮换其它浏览器指纹（Chrome/Safari/Firefox 各版本）；\n"
        "    ③ 启动真浏览器过一次校验，再用它的会话取页面。\n"
        "  剩下的可能：当前网络出口被站点判为高风险（机房 IP / 公共代理 / 云主机出口）。\n"
        "  可用的做法：\n"
        "    · 换一个网络出口（家宽、手机热点）后重试；\n"
        "    · 在正常浏览器里打开该页面，若能看到内容，\n"
        "      再用“导入专辑页面”或“下载单个链接”入口继续；\n"
        "    · 想看逐层诊断明细，运行 tools/verify_cloudflare_bypass.py。"
        f"{suffix}"
    )


def classify_blocked_response(status_code: int, challenge_like: bool) -> Optional[DownloaderError]:
    """按"状态码 + 是否校验页"给出一个**准确**的错误对象，不再一律说成云校验。

    区分四类（Brief §5）：
      · 校验页（无论状态码）      -> SiteBlockedError（真正的人机验证）
      · 429                        -> RateLimitError（限流）
      · 403（不是校验页）          -> AccessDeniedError（普通"访问被拒绝"）
      · 其他非 200                 -> None（交给调用方按通用 HTTP 错误处理）
    """
    if challenge_like:
        return _site_blocked_error(status_code)
    if status_code == 429:
        return RateLimitError(
            "站点返回 HTTP 429（请求太频繁，被限流了）。\n"
            "  稍等一会儿再试即可；这**不是**人机校验，不用去浏览器过验证。"
        )
    if status_code == 403:
        return AccessDeniedError(
            "站点返回 HTTP 403（访问被拒绝）。\n"
            "  这次**没有**看到人机校验页面的特征，所以先按“访问被拒绝”处理：\n"
            "    可能是路径不对、地区限制，或该资源不允许程序化访问。\n"
            "  可以在正常浏览器里核对一下这个地址是否可打开。"
        )
    return None


def _html_text(response: requests.Response) -> str:
    """从响应里取出 HTML 文本，尽量不出现乱码。

    requests 有时会猜错编码（例如把 UTF-8 猜成 ISO-8859-1），
    这里用 apparent_encoding 兜一层，保证日文曲名不乱码。
    """
    if response.encoding is None or str(response.encoding).lower() in ("iso-8859-1", "ascii"):
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def fetch_html(
    session: requests.Session,
    url: str,
    on_log: Optional[Callable[[str], None]] = None,
) -> str:
    """下载一个网页并返回它的 HTML 文本。

    出错时抛出人话错误，而不是把 requests 的原始异常直接甩给用户。

    错误分类（Brief §5）：校验页 / 限流 429 / 普通 403 / 其他非 200 分别报出，
    不再"只要不是 200 就模糊处理"。

    v1.0.0：如果响应是 Cloudflare 人机校验页，先走 khdownloader_transport 的升级梯子
    （换浏览器指纹 → 真浏览器会话 → 直接用浏览器渲染出的页面），只有全都过不去，
    才抛出 SiteBlockedError。分类逻辑本身没有变。
    """
    try:
        response = session.get(url, timeout=TIMEOUT)
    except requests.Timeout:
        raise NetworkError(f"连接超时，网络太慢或对方没响应：{url}")
    except requests.ConnectionError as exc:
        raise NetworkError(f"连不上这个地址，请检查网络：{url}\n（原始信息：{exc}）")

    # 人机校验优先判断，且不只看 403：Cloudflare 的托管式校验也可能用别的状态码返回，
    # 但内容一定是那个 "Just a moment..." 页面。只要页面长那样，就算被拦。
    challenge_like = _looks_like_challenge(response)
    tier = ""
    if challenge_like:
        recovered_response, browser_html, tier = transport.recover_html(
            session, url, on_log=on_log
        )
        if recovered_response is not None:
            response = recovered_response
            challenge_like = _looks_like_challenge(response)
        elif browser_html is not None:
            # 浏览器已经把真正的页面渲染出来了：校验层已经过，直接交给解析流程。
            # 这里不做编码猜测 —— 浏览器给的就是 UTF-8 文本。
            return browser_html

    classified = classify_blocked_response(response.status_code, challenge_like)
    if classified is not None:
        if challenge_like and tier:
            raise _site_blocked_error(response.status_code, tier)
        raise classified

    if response.status_code != 200:
        raise DownloaderError(f"站点返回了 HTTP {response.status_code}（不是 200 成功）：{url}")

    return _html_text(response)


# ---------------------------------------------------------------------------
# 2. 地址与文件名处理
# ---------------------------------------------------------------------------


def normalize_album_url(text: str) -> str:
    """把用户输入的专辑地址整理干净，并做基本校验。

    只做"必须"的校验：非空、是 http/https、有域名。
    不强制要求一定是 khinsider 的域名——这样本地测试服务器也能跑通，
    但调用方可以自己决定要不要给出提示。
    """
    url = (text or "").strip()
    if not url:
        raise DownloaderError("请先填写专辑地址。")
    if not url.lower().startswith(("http://", "https://")):
        raise DownloaderError("地址要以 http:// 或 https:// 开头。")
    parts = urlsplit(url)
    if not parts.netloc:
        raise DownloaderError("这个地址看起来不完整，缺少域名部分。")
    return url


def album_path(url: str) -> str:
    """取出专辑页地址的"路径部分"，末尾不带斜杠。

    例：https://downloads.khinsider.com/game-soundtracks/album/xxx
        -> /game-soundtracks/album/xxx

    这个前缀就是筛选"本专辑自己的曲目链接"的凭据：站点是相对路径结构，
    同一个专辑下的曲目页都以它为开头，别的专辑、导航栏、评论区都不会命中。
    """
    return urlsplit(url).path.rstrip("/")


def album_identity(url: str) -> tuple[str, str]:
    """取一个地址的（主机, 专辑路径），并做**合理规范化**，用于"地址与文件是否相符"的比较。

    规范化规则（Brief v008 §4.2）：
      · 末尾斜杠不计（/album/x 与 /album/x/ 相同）；
      · 查询参数与片段不计（?utm=…、#锚点 不改变专辑身份）；
      · 主机小写比较；"downloads.khinsider.com" 与 "www.khinsider.com" 的
        www 前缀视为等同；
      · **不做**多余的百分号解码循环：%2520 是曲目文件名里的编码，解码它属于
        曲目路径的事，与"是不是同一个专辑"无关 —— 不同专辑绝不能因解码而混同。

    返回 (host, album_path)。输入不是 http(s) 地址时返回 ("", 原样路径)。
    """
    parts = urlsplit((url or "").strip())
    if not parts.scheme and not parts.netloc:
        return "", parts.path
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    return host, path


def same_album(a: str, b: str) -> bool:
    """两个地址是否指向**同一张专辑**（按 album_identity 规范化后比较）。

    只比较"主机 + 专辑路径"；查询参数、片段、末尾斜杠不影响判定。
    不同专辑的 slug 不同，永远不可能被判成相同。
    """
    host_a, path_a = album_identity(a)
    host_b, path_b = album_identity(b)
    if not path_a or not path_b:
        return False
    return host_a == host_b and path_a == path_b


def is_song_page_url(url: str) -> bool:
    """判断一个地址是不是**曲目详情页**（/album/<slug>/NN….mp3）。

    用户常把"点进某一首歌之后"的地址当成专辑地址粘进来 —— 这是明确的
    可识别场景，要单独提示，而不是笼统地说"没有曲目"。
    """
    path = urlsplit((url or "").strip()).path
    return bool(re.match(r"^/game-soundtracks/album/[^/]+/[^/]+\.mp3$", path, re.I))


def sanitize_filename(name: str) -> str:
    """把曲名变成一个 Windows 上安全、可用的文件名。

    处理四件事：
      1. 替换掉 Windows 不允许的字符 < > : " / \\ | ? * 和控制字符；
      2. 去掉首尾空白和结尾的点（Windows 会悄悄吃掉结尾的点，导致找不到文件）；
      3. 躲开 CON / PRN / AUX / NUL / COM1..9 / LPT1..9 这些设备名；
      4. 截断过长的名字（Windows 整条路径上限 260 字符）。
    """
    name = unquote(name or "")
    # 1. 非法字符与控制字符
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    # 2. 首尾空白、结尾的点
    name = name.strip().strip(".").strip()
    if not name:
        name = "untitled"
    # 3. Windows 保留设备名
    stem_upper = name.upper()
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved |= {f"COM{i}" for i in range(1, 10)}
    reserved |= {f"LPT{i}" for i in range(1, 10)}
    if stem_upper in reserved:
        name = "_" + name
    # 4. 截断（保留 .mp3 后缀的空间）
    max_len = 120
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name


def resolve_destination(folder: Path, filename: str, remote_size: int = 0):
    """决定这个文件到底写到哪个路径，并返回 (目标路径, 是否跳过)。

    命名冲突规则（确定性、可预期）：
      - 目标不存在            -> 直接用它，不跳过；
      - 已存在且大小和远端一致  -> 跳过下载（认为上次已经下好了，不重复下）；
      - 已存在但大小不一致      -> 依次试 "名字 (2).mp3"、"名字 (3).mp3" ……直到空位。
    任何时候都不会覆盖已有文件。
    """
    stem = Path(filename).stem
    suffix = Path(filename).suffix or ".mp3"
    candidate = folder / filename

    if not candidate.exists():
        return candidate, False

    if remote_size and candidate.stat().st_size == remote_size:
        return candidate, True

    index = 2
    while True:
        candidate = folder / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate, False
        index += 1
        if index > 999:  # 兜底，避免极端情况下死循环
            raise DownloaderError(f"同名文件太多了，无法为 {filename} 找到可用文件名。")


# ---------------------------------------------------------------------------
# 3. 解析网页
# ---------------------------------------------------------------------------


#: 真实专辑页里曲目表的容器 id（已实测确认）。
#: 证据：tools/evidence/REAL_album_page.html，
#: 回归夹具：tests/fixtures/real_album_page.html
ALBUM_TABLE_ID = "songlist"

#: 只要这个字符串，就说明链接文字是"时长/文件大小"这类数字，不是曲名。
#: 真实页面上，同一行里时长、MP3 大小、FLAC 大小三个格子都挂着同一个链接，
#: 直接取第一个会拿到 "1." 或 "2:27"，所以必须按文字形态把它们排除掉。
_NUMERIC_LABEL_RE = re.compile(r"^[\d\s.,:：\-]+(?:MB|KB|GB)?$", re.IGNORECASE)


def _link_text_looks_numeric(text: str) -> bool:
    """判断一个链接的文字是不是"纯数字型标签"（时长 2:27 / 大小 4.70 MB / 序号 1.）。

    这类文字不可能是曲名，拿它当曲名会得到一堆 "2:27"、"4.70 MB"。
    """
    return bool(_NUMERIC_LABEL_RE.match(text.strip()))


def parse_album_page(html: str, album_url: str) -> list[Track]:
    """从专辑页 HTML 里解析出「有序、去重」的曲目列表。

    解析规则（按优先级）：
      1. 若页面里有 <table id="songlist">，只在这张表里找 —— 天然排除了导航、
         相关专辑、评论、播放器控件等区域的链接；
      2. 找不到 #songlist 时退化为整页查找，但仍然用"本专辑路径前缀"过滤，
         所以依然不会误收别处的链接。

    在表内，一行曲目的结构是（已实测确认）：

        <tr>
          <td>                         播放按钮
          <td align="right">  1.       ← 没有链接
          <td class="clickable-row"><a>曲名</a>       ← 这一列才是曲名
          <td align="right" class="clickable-row"><a>2:27</a>
          <td align="right" class="clickable-row"><a>4.70 MB</a>
          <td align="right" class="clickable-row"><a>17.15 MB</a>
          <td class="playlistDownloadSong"><a>↓图标</a>
          <td class="playlistAddCell">

    同一行里 6 个链接指向**同一个地址**，所以：

      · 先按「本专辑路径前缀 + .mp3 结尾」筛出合格链接；
      · 再看链接文字：像 "2:27"、"4.70 MB" 这种纯数字标签**跳过**，
        剩下的就是曲名（取第一条，即曲名列的链接）；
      · 最后按地址去重（同一首曲目会重复出现 3~4 次）。

    这样即使站点改了 class 名，只要"曲名列是唯一带真实文字的那一列"这个
    语义不变，解析结果就还是对的。
    """
    soup = BeautifulSoup(html, "html.parser")

    scope = soup.find("table", id=ALBUM_TABLE_ID)
    scope_name = f"table#{ALBUM_TABLE_ID}"
    if scope is None:
        scope = soup
        scope_name = f"整页（未找到 table#{ALBUM_TABLE_ID}）"

    prefix = album_path(album_url) + "/"
    tracks: list[Track] = []
    seen_urls: set[str] = set()

    for anchor in scope.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith("#"):
            continue

        full_url = urljoin(album_url, href)
        path = urlsplit(full_url).path

        if not path.startswith(prefix):
            continue
        if not path.lower().endswith(".mp3"):
            continue

        key = full_url.split("#")[0]
        if key in seen_urls:
            continue

        name = anchor.get_text(" ", strip=True)

        # 曲名列的文字一定不是 "2:27" / "4.70 MB" 这种；遇到就跳过，
        # 让同一行的下一个锚点（曲名列）来代表这首曲目。
        if name and _link_text_looks_numeric(name):
            continue

        if not name:
            # 链接没有文字时，退一步用 URL 里的文件名当名字
            name = unquote(path.rsplit("/", 1)[-1])
        if not name:
            continue

        seen_urls.add(key)
        tracks.append(Track(name=name, page_url=key))

    if not tracks:
        raise NoTracksFoundError(
            "这个页面里没有解析到任何曲目链接。\n"
            f"  解析范围：{scope_name}\n"
            "  可能原因：地址不是专辑页、专辑页结构变了，或者该专辑确实没有 MP3。\n"
            "  排查办法：用 tools/inspect_site.py 把页面 HTML 存下来看看。"
        )

    return tracks


def _deep_unquote(text: str) -> str:
    """把百分号编码解开两层。

    站点上的曲目文件名是"编码了两次"的（空格写成 %2520，解一层变 %20，
    再解一层才变空格），所以要连着解两次才能拿它做比较。
    """
    return unquote(unquote(text or ""))


def _filename_of(url: str) -> str:
    """取出地址里最后一段并深度解码，用来比较"是不是同一个文件名"。"""
    return _deep_unquote(urlsplit(url).path.rsplit("/", 1)[-1]).strip().lower()


#: 真实页面上 MP3 下载链接的文字（已实测确认，见 tools/evidence/）。
#: 同一页面还有一条一模一样写法、但结尾是 "as FLAC" 的链接，所以每条规则都要
#: 额外限定 href 以 .mp3 结尾，否则会把 FLAC 当成 MP3。
DOWNLOAD_AS_MP3_TEXT = "download as mp3"

#: 真实页面里下载链接内部的标记类名（已实测确认）。
SONG_DOWNLOAD_LINK_CLASS = "songDownloadLink"


def parse_song_page(html: str, page_url: str) -> str:
    """从曲目详情页 HTML 里解析出真正的 MP3 下载地址。

    按"从具体到宽泛"依次尝试四条规则。**前三条都是在真实页面上实测确认过的**
    （证据：`tools/evidence/02_2520Crisscross.mp3.html` 与 `REAL_album_page.html`，
    回归夹具：`tests/fixtures/real_song_page.html`、`tests/fixtures/real_album_page.html`）：

      规则 A：链接文字里含 "download as MP3"          —— 最直白的语义
      规则 B：链接内部含 class="songDownloadLink"      —— 站点自己的下载标记
      规则 C：页面播放器 <audio src> / <source src>    —— 播放器直接给出了文件地址

    三条都不中时，才退回规则 D（v001 的启发式打分）：
      +2 分：候选链接的文件名（解开两层百分号编码后）与当前曲目页的文件名一致
      +1 分：候选链接在别的主机名上
    它不依赖任何具体 class 名，用来兜住页面改版的情况。

    **重要防护**：真实专辑页的曲目链接，href 也是 `.mp3` 结尾，而且链接文字里
    同样带着 "download as MP3"（那是同一曲目行的下载图标列）。如果不加限制，
    把专辑页误当曲目页传进来，规则 A 会命中曲目链接，返回一个**看起来像 MP3、
    实际是 HTML 详情页**的地址。

    所以这里再加一道结构守卫：**曲目表内的链接一律不算下载地址**。
    真实专辑页的曲目链接全部位于 `<table id="songlist">` 里；而真实曲目详情页
    的下载链接位于该表之外（详情页本身也没有这张表）。这条判据把
    "专辑页里的曲目链接"和"详情页里的下载链接"彻底分开，
    即使两种页面被传混了也不会下错东西。

    四条都不中时抛 NoMp3LinkError（可读错误），不会静默产生空文件。
    """
    soup = BeautifulSoup(html, "html.parser")

    #: 曲目表内的所有链接都不算下载地址（见上面的"结构守卫"说明）。
    songlist = soup.find("table", id=ALBUM_TABLE_ID)
    songlist_hrefs: set[str] = set()
    if songlist is not None:
        for anchor in songlist.find_all("a", href=True):
            songlist_hrefs.add(urljoin(page_url, anchor["href"].strip()).split("#")[0])

    def _is_mp3(candidate: str) -> bool:
        return urlsplit(candidate).path.lower().endswith(".mp3")

    def _is_self_reference(candidate: str) -> bool:
        """候选地址是不是就指向当前页面自己（同主机 + 同路径）。"""
        cand = urlsplit(candidate)
        page = urlsplit(page_url)
        return (cand.netloc.lower() == page.netloc.lower()
                and cand.path.rstrip("/") == page.path.rstrip("/"))

    def _accept(candidate: str) -> Optional[str]:
        if not _is_mp3(candidate):
            return None
        if _is_self_reference(candidate):
            return None
        if candidate.split("#")[0] in songlist_hrefs:
            return None  # 结构守卫：曲目表里的链接不是下载地址
        return candidate

    # ---- 规则 A：链接文字直接写着 "download as MP3" ----
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href:
            continue
        if DOWNLOAD_AS_MP3_TEXT in anchor.get_text(" ", strip=True).lower():
            picked = _accept(urljoin(page_url, href))
            if picked:
                return picked

    # ---- 规则 B：链接内部带 class="songDownloadLink" ----
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href:
            continue
        if anchor.find(class_=SONG_DOWNLOAD_LINK_CLASS) is None:
            continue
        picked = _accept(urljoin(page_url, href))
        if picked:
            return picked

    # ---- 规则 C：页面自带的播放器 ----
    for tag in soup.find_all(["audio", "source"]):
        src = (tag.get("src") or "").strip()
        if not src:
            continue
        picked = _accept(urljoin(page_url, src))
        if picked:
            return picked

    # ---- 规则 D（兜底）：打分启发式 ----
    page_parts = urlsplit(page_url)
    page_file = _filename_of(page_url)

    scored: list[tuple[int, int, str]] = []  # (得分, 出现次序, 地址)
    order = 0

    def consider(full_url: str):
        """给一个候选地址打分并收进来。"""
        nonlocal order
        parts = urlsplit(full_url)
        if not parts.path.lower().endswith(".mp3"):
            return
        # 排除"指向本页自己"的链接
        if (parts.netloc.lower() == page_parts.netloc.lower()
                and parts.path.rstrip("/") == page_parts.path.rstrip("/")):
            return
        # 结构守卫：曲目表里的链接不是下载地址
        if full_url.split("#")[0] in songlist_hrefs:
            return
        score = 0
        if page_file and _filename_of(full_url) == page_file:
            score += 2
        if parts.netloc.lower() != page_parts.netloc.lower():
            score += 1
        order += 1
        scored.append((score, order, full_url))

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith("#"):
            continue
        consider(urljoin(page_url, href))

    if not scored:
        raise NoMp3LinkError(
            "这个曲目详情页里找不到 MP3 链接。\n"
            f"  页面地址：{page_url}\n"
            "  可能原因：该曲目只有 FLAC、页面结构变了，或链接不是 .mp3 结尾。\n"
            "  排查办法：用 tools/inspect_site.py 把这个详情页的 HTML 存下来看看。"
        )

    # 先按分数从高到低，分数一样就按出现顺序（次序小的在前）
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored[0][2]


# ---------------------------------------------------------------------------
# 4. 下载
# ---------------------------------------------------------------------------


def _iter_response_chunks(response: requests.Response, chunk_size: int):
    """把"分块读取网络"变成一个小生成器。

    单独抽出来的原因：传输中途断线时 requests 会抛 RequestException
    （比如 ChunkedEncodingError），这里统一翻译成人话错误，
    免得原始异常漏到界面上变成看不懂的报错。
    """
    try:
        for chunk in response.iter_content(chunk_size=chunk_size):
            yield chunk
    except requests.RequestException as exc:
        raise NetworkError(f"下载中途连接断开：{exc}") from exc


def _is_audio_content_type(content_type: str, url: str = "") -> bool:
    """判断响应的 Content-Type 算不算"音频"。

    接受的形态：
      · audio/*（含 audio/mpeg、audio/mp3）
      · application/octet-stream（许多静态文件托管站给二进制文件就是这个通用类型）
      · 以 .mp3 结尾的 URL 配合 application/x-... 之类的二进制类型

    明确拒绝 text/html、text/plain、application/json、text/xml 等"这明显不是音频"的类型 ——
    这些几乎都是错误页、校验页或 JSON 报错，**绝不能**当成 MP3 存下来。
    """
    ct = (content_type or "").lower()
    if ct.startswith("audio/"):
        return True
    if ct == "application/octet-stream":
        return True
    if ct in ("binary/octet-stream",):
        return True
    # 明显的"非音频"类型，直接否掉
    if ct.startswith("text/") or ct in ("application/json", "application/xml"):
        return False
    # 其余 application/* 且 URL 以 .mp3 结尾，宽松接受
    if ct.startswith("application/") and url.lower().split("?")[0].endswith(".mp3"):
        return True
    # 没有任何 Content-Type 时不武断拦截（交给"字节数一致"校验兜底）。
    # 局限：若服务器既不给 Content-Type、又把 HTML 当 .mp3 返回，本层拦不住——
    # 这种极端情况依赖 .part→最终文件 的完整性与用户复核（见报告"已知限制"）。
    return not ct


def _open_download_response(
    session: requests.Session,
    url: str,
    on_log: Optional[Callable[[str], None]] = None,
):
    """为下载打开一个流式响应，并在被 Cloudflare 拦住时先走一遍升级梯子。

    （v1.0.0）音频主机当前没有被校验，这条路径是保险：万一哪天连音频层也被挑战，
    仍然先用指纹层重试，而不是直接把"人机校验"甩给用户。
    """
    try:
        response = session.get(url, stream=True, timeout=TIMEOUT)
    except requests.Timeout:
        raise NetworkError(f"下载超时：{url}")
    except requests.ConnectionError as exc:
        raise NetworkError(f"下载时连接中断：{url}\n（原始信息：{exc}）")

    if str(response.headers.get("cf-mitigated", "")).lower() != "challenge":
        return response

    recovered, tier = transport.recover_stream(
        session, url, kwargs={"stream": True, "timeout": TIMEOUT}, on_log=on_log
    )
    try:
        response.close()
    except Exception:  # noqa: BLE001
        pass
    if recovered is not None:
        return recovered
    raise _site_blocked_error(
        int(response.status_code or 403), tier or "download-link"
    )


def download_file(
    session: requests.Session,
    url: str,
    folder: Path,
    filename: str,
    on_progress: Optional[Callable[[int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_log: Optional[Callable[[str], None]] = None,
):
    """流式下载一个文件，返回 (最终路径, 写入字节数, 是否跳过)。

    写文件策略：
      - 先写到 "最终名字.part"；
      - 全部写完、且字节数和 Content-Length 对得上，才用 os.replace 原子改名；
      - 中途任何失败，都会把 .part 删掉 —— 绝不让半成品顶着 .mp3 的名字留在硬盘上。

    取消：
      should_cancel 是一个"要不要停了"的询问函数（界面上接的是取消按钮）。
      每个数据块写盘前问一次 —— 这是最自然的安全检查点，既不会卡住很久，
      也不会把一个文件写坏。取消时同样会删掉 .part，并抛 CancelledError。
      已经完成的 .mp3 永远不会被动到。
    """
    folder.mkdir(parents=True, exist_ok=True)
    response = _open_download_response(session, url, on_log=on_log)

    with response:
        # ---- 状态码 ----
        # 注意：这是流式响应，**不能**读 response.text（会把整个 body 拉进内存、
        # 且遇到断流会抛错）。人机校验只看响应头里的 cf-mitigated 信号
        # （上面 _open_download_response 已经处理并尝试过升级梯子了）。
        if response.status_code != 200:
            classified = classify_blocked_response(response.status_code, False)
            if classified is not None:
                raise classified
            raise DownloaderError(f"下载链接返回 HTTP {response.status_code}：{url}")

        # ---- 文件安全：确认返回的是音频，绝不能把 HTML 错误页/校验页存成 .mp3 ----
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type and not _is_audio_content_type(content_type, url):
            # 把"到底收到的是什么"写清楚，方便排查；同时绝不落盘。
            raise NotAudioContentError(
                f"这个链接返回的不是音频内容（Content-Type: {content_type}），已拒收。\n"
                f"  地址：{url}\n"
                "  常见原因：链接指向的是详情页/错误页/校验页，而不是真正的 MP3 文件。\n"
                "  本工具不会把这种内容当成音频保存。"
            )

        total = int(response.headers.get("Content-Length") or 0)
        dest_path, should_skip = resolve_destination(folder, filename, total)
        if should_skip:
            return dest_path, 0, True

        part_path = Path(str(dest_path) + ".part")
        written = 0
        try:
            with open(part_path, "wb") as handle:
                for chunk in _iter_response_chunks(response, CHUNK_SIZE):
                    # 安全检查点：下一个数据块落盘之前先问要不要取消。
                    if should_cancel is not None and should_cancel():
                        raise CancelledError(f"已取消：{filename}")
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    if on_progress is not None:
                        on_progress(written, total)

            if total and written != total:
                raise DownloaderError(
                    f"文件不完整：应该 {total} 字节，实际收到 {written} 字节，已丢弃。"
                )

            os.replace(part_path, dest_path)  # 原子改名，成功后才是"完成"
        except BaseException:
            part_path.unlink(missing_ok=True)  # 失败或取消都不留半成品
            raise

    return dest_path, written, False


def download_album(
    album_url: str,
    folder: Path,
    on_event: Callable[[str, dict], None],
    session: Optional[requests.Session] = None,
    pause: float = SLEEP_BETWEEN_REQUESTS,
    should_cancel: Optional[Callable[[], bool]] = None,
):
    """把整张专辑按顺序下载到 folder。

    on_event 是一个回调：核心逻辑通过它把进度告诉界面。
    核心逻辑不碰界面对象，只发消息，所以线程安全完全由界面那侧保证。

    should_cancel 是取消询问函数（界面上接取消按钮）。它会在两个地方被问到：
      - 每首曲目开始之前 —— 取消了就不再开新的一首；
      - 每个数据块落盘之前 —— 下载到一半也能停下来。
    取消之后，已经下好的 .mp3 原样保留，没下完的 .part 会被删掉，
    剩下没处理的曲目一律记为 "cancelled"。

    事件类型：
      ("log",           {"text": str})
      ("tracks",        {"total": int})
      ("track_start",   {"index": int, "total": int, "name": str})
      ("phase",         {"index": int, "total": int, "phase": str})
                        phase 取值： "resolving"（正在解析曲目页）/ "downloading"（正在下载）
      ("file_progress", {"written": int, "total": int})
                        total 为 0 表示对方没给 Content-Length ——
                        这时界面**不要**编一个百分比出来，显示"未知大小"即可。
      ("track_done",    {"index": int, "total": int, "name": str, "path": str,
                         "skipped": bool, "bytes": int})
      ("track_fail",    {"index": int, "total": int, "name": str, "error": str})
      ("cancelled",     {"index": int, "total": int})
                        用户取消时发一次，之后是 ("done", ...)。
      ("done",          {"counts": dict, "results": list[TrackResult]})
                        counts = {completed, skipped, failed, cancelled}。
    """
    session = session or make_session()
    folder = Path(folder)
    results: list[TrackResult] = []
    last_index = 0  # 记一下处理到第几首了，取消时要用

    def cancelled() -> bool:
        return should_cancel is not None and should_cancel()

    on_event("log", {"text": f"打开专辑页：{album_url}"})
    html = fetch_html(
        session, album_url, on_log=lambda text: on_event("log", {"text": f"  · {text}"})
    )
    tracks = parse_album_page(html, album_url)
    on_event("log", {"text": f"找到 {len(tracks)} 首曲目。"})
    on_event("tracks", {"total": len(tracks)})

    was_cancelled = False

    for index, track in enumerate(tracks, start=1):
        # ---- 曲目之间的取消检查点：取消了就干脆不要再开下一首 ----
        if cancelled():
            was_cancelled = True
            break

        last_index = index
        on_event("track_start", {"index": index, "total": len(tracks), "name": track.name})
        try:
            on_event("phase", {"index": index, "total": len(tracks), "phase": "resolving"})
            page_html = fetch_html(
                session,
                track.page_url,
                on_log=lambda text: on_event("log", {"text": f"  · {text}"}),
            )
            mp3_url = parse_song_page(page_html, track.page_url)

            filename = sanitize_filename(track.name)
            if not filename.lower().endswith(".mp3"):
                filename += ".mp3"

            on_event("phase", {"index": index, "total": len(tracks), "phase": "downloading"})
            dest, written, skipped = download_file(
                session,
                mp3_url,
                folder,
                filename,
                on_progress=lambda done, total: on_event(
                    "file_progress", {"written": done, "total": total}
                ),
                should_cancel=should_cancel,
                on_log=lambda text: on_event("log", {"text": f"  · {text}"}),
            )

            if skipped:
                message = f"已存在且大小一致，跳过：{dest.name}"
                on_event("log", {"text": f"  [跳过] {message}"})
                results.append(TrackResult(track.name, "skipped", message, dest))
            else:
                message = f"完成（{written / 1024 / 1024:.2f} MB）-> {dest.name}"
                on_event("log", {"text": f"  [完成] {message}"})
                results.append(TrackResult(track.name, "completed", message, dest))

        except CancelledError:
            # 用户按了取消：不记为失败，单独记一类，然后立刻收尾。
            was_cancelled = True
            results.append(TrackResult(track.name, "cancelled", "已取消"))
            break
        except DownloaderError as exc:
            on_event("track_fail", {"index": index, "total": len(tracks),
                                    "name": track.name, "error": str(exc)})
            results.append(TrackResult(track.name, "failed", str(exc)))
        except OSError as exc:
            message = f"写文件失败：{exc}"
            on_event("track_fail", {"index": index, "total": len(tracks),
                                    "name": track.name, "error": message})
            results.append(TrackResult(track.name, "failed", message))

        on_event("track_done", {"index": index, "total": len(tracks), "name": track.name})
        if pause and index < len(tracks):
            time.sleep(pause)  # 礼貌一点，别把站点打疼

    # ---- 取消之后，把「还没轮到」的曲目补记为 cancelled，好让汇总总数对得上 ----
    if was_cancelled:
        for track in tracks[len(results):]:
            results.append(TrackResult(track.name, "cancelled", "未处理（已取消）"))
        on_event("cancelled", {"index": last_index, "total": len(tracks)})

    counts = summarize_results(results)
    on_event("done", {"counts": counts, "results": results})
    return results


# ---------------------------------------------------------------------------
# 5. 恢复入口（v007 新增）
#
# 背景：站点对 `downloads.khinsider.com` 的程序化访问启用了 Cloudflare 人机校验，
# 现有"输入专辑 URL 自动下载"在部分网络出口下会被挡在门外（403 + cf-mitigated）。
# 但 MP3 托管在**另一个子域**，实测未被拦截（见 v007 报告）。因此提供两个
# **明确、互不冒充**的恢复入口：
#   · 导入专辑页面 HTML  —— 恢复"专辑/曲目发现"这一层；
#   · 下载单个链接       —— 恢复"单个 MP3 的下载"这一层。
# 两者都复用既有下载引擎（.part → 原子改名、取消、跳过、文件名净化）。
# ---------------------------------------------------------------------------


#: 导入 HTML 分类结果。
IMPORT_KIND_ALBUM = "album"            #: 正常专辑页，能解析出曲目
IMPORT_KIND_CHALLENGE = "challenge"    #: 人机校验页
IMPORT_KIND_ERROR = "error"            #: 错误页 / 空白页 / 明显不是内容页
IMPORT_KIND_NO_TRACKS = "no_tracks"    #: 拿到了页面，但没有解析到曲目

#: v008：页面像专辑页（有曲目表结构），但因为来源无法可靠识别而不能确定曲目
IMPORT_KIND_UNRESOLVED = "unresolved"
#: v008：文件格式不支持（例如浏览器另存为的 MHTML 单文件归档）
IMPORT_KIND_UNSUPPORTED_FORMAT = "unsupported_format"


#: KHInsider 曲目详情页的路径前缀（用于识别"页面属于哪个专辑"）。
#: 曲目页形如 /game-soundtracks/album/<slug>/NN....mp3，专辑页形如
#: /game-soundtracks/album/<slug>，两者共用同一个栏目路径。
KHINSIDER_ALBUM_ROOT = "/game-soundtracks/album/"


def detect_mhtml(content: str) -> bool:
    """判断一份文本是不是 MHTML（浏览器"网页归档，单文件 .mht/.mhtml"）。

    MHTML 是 MIME 多部分容器（RFC 2557），不是 HTML：纯文本解析器看不到
    `table#songlist`，所以必须提前识别并明确告诉用户"这个格式不支持"，
    而不是报"没有曲目"。
    """
    head = (content or "").lstrip()[:400].lower()
    if head.startswith("from:") or head.startswith("mime-version:"):
        return True
    if "content-type: multipart/related" in head:
        return True
    return False


def _page_title(html: str) -> str:
    """取出 <title> 文字（仅用于展示识别结果，不参与 slug 推算）。"""
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.S | re.I)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()


def identify_album_source(html: str) -> dict:
    """在**按地址过滤之前**，先从页面本身识别"这份 HTML 属于哪个专辑"。

    这是 v008 的核心修正：先识别文件、再核对地址，顺序不能反。
    （v007 的问题：先按用户输入地址过滤 → 地址错了就把所有曲目滤光 → 误报"没有曲目"。）

    识别依据（按可靠度）：
      1. table#songlist 里指向曲目详情页的链接（/game-soundtracks/album/<slug>/….mp3），
         取其中重复最多的专辑 slug —— 这是站点自己的结构，最可靠；
      2. 页面 <link rel="canonical"> / og:url（辅助信号；slug 与第 1 条冲突时以曲目表为准，
         并记录冲突）；
      3. 都没有时，无法识别来源 —— 返回 identified=False 并说明原因，
         **不**从 <title> 推算 slug，**不**采集推荐专辑/导航/评论区的链接。

    返回 dict 字段：
      identified      是否可靠识别出唯一专辑来源
      album_slug      识别出的专辑 slug（如 cross-channel-…-2011）
      album_url       由 slug 构成的候选专辑地址（https://downloads.khinsider.com/...）
      title           页面 <title> 文字（仅展示用）
      track_count     songlist 里唯一曲目详情页链接数（未按地址过滤）
      track_hrefs     唯一曲目详情页相对路径列表（保持出现顺序）
      evidence        人话描述的识别依据
      conflicts       冲突/疑点列表（比如 canonical 与曲目表不一致）
      host            曲目链接所属主机（root-relative 时为 khinsider 官方域名）
    """
    ALBUM_PAGE_HOST = "https://downloads.khinsider.com"
    soup = BeautifulSoup(html or "", "html.parser")
    conflicts: list[str] = []

    # ---- 依据 1：songlist 表内的曲目详情页链接 ----
    table = soup.find("table", id=ALBUM_TABLE_ID)
    slugs: list[str] = []
    track_hrefs: list[str] = []
    if table is not None:
        seen: set[str] = set()
        for anchor in table.find_all("a", href=True):
            href = (anchor["href"] or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            parts = urlsplit(href)
            path = parts.path
            if not path or not path.lower().endswith(".mp3"):
                continue
            # 只认站内相对路径或 khinsider 主机的链接；外站 mp3 链接（如 CDN）
            # 不能用来判断页面归属 —— 它们可能指向任何来源。
            if parts.netloc and "khinsider.com" not in parts.netloc.lower():
                conflicts.append(f"曲目表中发现外站链接（不用于识别来源）：{href[:80]}")
                continue
            m = re.match(r"^/game-soundtracks/album/([^/]+)\.mp3$", path)
            if not m:
                # /album/<slug>/NN….mp3（曲目详情页）才是要找的形态
                m = re.match(r"^/game-soundtracks/album/([^/]+)/[^/]+\.mp3$", path)
            if not m:
                continue
            slug = m.group(1)
            slugs.append(slug)
            if path not in seen:
                seen.add(path)
                track_hrefs.append(path)

    # 取出现最多的 slug（同一页面的曲目表只会属于一个专辑；重复即一致性的证据）
    slug = None
    if slugs:
        from collections import Counter

        top_slug, top_count = Counter(slugs).most_common(1)[0]
        if len(set(slugs)) > 1:
            conflicts.append(
                "曲目表中的链接指向多个不同专辑（{}），无法确定唯一来源。".format(
                    "、".join(sorted(set(slugs))[:4])
                )
            )
        else:
            slug = top_slug

    # ---- 依据 2：canonical / og:url（辅助） ----
    canonical = ""
    canonical_tag = soup.find("link", rel=lambda v: v and "canonical" in v)
    if canonical_tag and canonical_tag.get("href"):
        canonical = canonical_tag["href"].strip()
    if not canonical:
        og = soup.find("meta", attrs={"property": "og:url"})
        if og and og.get("content"):
            canonical = og["content"].strip()

    canonical_slug = None
    canonical_host_ok = False
    if canonical:
        cparts = urlsplit(canonical)
        cpath = cparts.path.rstrip("/")
        m = re.match(r"^/game-soundtracks/album/([^/]+)$", cpath)
        if m:
            canonical_slug = m.group(1)
            canonical_host_ok = (not cparts.netloc) or ("khinsider.com" in cparts.netloc.lower())
        elif cparts.netloc and "khinsider.com" not in cparts.netloc.lower():
            conflicts.append(f"canonical 指向其它站点：{canonical[:80]}")

    if slug and canonical_slug and slug != canonical_slug:
        conflicts.append(
            f"canonical 指向的专辑（{canonical_slug}）与曲目表所属专辑（{slug}）不一致，"
            "以曲目表为准。"
        )

    title = _page_title(html)
    evidence = ""
    if slug:
        evidence = f"曲目表 table#{ALBUM_TABLE_ID} 中的 {len(track_hrefs)} 个唯一曲目链接都属于专辑「{slug}」"
        if canonical_slug == slug:
            evidence += "；canonical 一致"
    elif canonical_slug and canonical_host_ok:
        # 页面自称是这个专辑，但没有曲目表佐证 —— 不可靠，交给用户核对
        evidence = f"页面 canonical 标记指向「{canonical_slug}」，但曲目表中没有可确认的曲目链接"

    identified = slug is not None and len(set(slugs or [slug])) == 1
    if not slug and not canonical_slug:
        evidence = "页面里既没有曲目表，也没有可识别的专辑地址标记（canonical/og:url）"

    # root-relative 曲目链接只给出了"专辑路径"，没给出主机。
    # 识别出的 album_url 默认按 khinsider 官方域名构造；若调用方随后发现
    # 用户地址的**路径 slug** 与识别 slug 一致（仅主机不同，如本地镜像/mock 站），
    # 应以用户地址为主机（见 inspect_import_html 中的 host 适配）。
    album_url = f"{ALBUM_PAGE_HOST}{KHINSIDER_ALBUM_ROOT}{slug}" if slug else ""

    return {
        "identified": identified,
        "album_slug": slug,
        "album_url": album_url,
        "canonical_url": canonical,
        "title": title,
        "track_count": len(track_hrefs),
        "track_hrefs": track_hrefs,
        "evidence": evidence,
        "conflicts": conflicts,
        "has_songlist": table is not None,
    }


@dataclass
class ImportInspection:
    """对"用户导入的专辑 HTML"的分类结果（v008 扩展：带来源识别信息）。"""

    kind: str                       #: IMPORT_KIND_* 之一
    tracks: list[Track]             #: kind == "album" 时的曲目列表，否则空
    message: str                    #: 给用户看的人话说明

    # ---- v008 新增：来源识别与地址核对（保持旧构造调用兼容：都有默认值）----
    detected_url: str = ""          #: 从页面里识别出的专辑地址（识别不到为空）
    page_title: str = ""            #: 页面 <title>（仅展示）
    evidence: str = ""              #: 识别依据（人话）
    conflicts: list = None          #: 冲突/疑点列表（默认 None，惰性建 []）
    matched_address: bool = False   #: 用户地址与文件来源是否相符
    address_relation: str = ""      #: match / different_album / song_page / empty / unresolved

    def __post_init__(self):
        if self.conflicts is None:
            self.conflicts = []

    @property
    def ok(self) -> bool:
        return self.kind == IMPORT_KIND_ALBUM and bool(self.tracks)


def inspect_import_html(html: str, album_url: str,
                        headers: Optional[dict] = None) -> ImportInspection:
    """判断用户导入的 HTML 到底是"正常专辑页"还是别的，并核对地址。

    v008 流程（顺序很重要）：**先识别文件，再核对地址**。
      1. 格式检查：MHTML / JSON / 空文件 → 直接分类，不进入解析；
      2. 人机校验页检查；
      3. 从页面自身识别专辑来源（identify_album_source）；
      4. 用识别结果与用户地址核对，分五种关系处理（Brief v008 §4.2）；
      5. 只在"文件可识别且地址相符（或用户确认换地址）"时才按地址解析曲目。

    明确区分：验证页 / 错误页 / 来源不匹配 / 曲目详情页地址 / 无曲目页 /
    来源无法识别 —— 各给不同的、用户可执行的下一步。

    headers 可选：若调用方还留着原始响应头（例如用程序下载后另存），
    可传入以用 `cf-mitigated` 一并判断；只有 HTML 文件时留空即可。

    注意：导入 HTML **不会**自动恢复浏览器的验证状态；这一步只解决"曲目发现"，
    详情页仍可能被拒（界面会据此给出下一步操作）。
    """
    # ---- 0) 格式守卫：MHTML 不是 HTML ----
    if detect_mhtml(html or ""):
        return ImportInspection(
            IMPORT_KIND_UNSUPPORTED_FORMAT,
            [],
            "这个文件是 MHTML 网页归档（.mht/.mhtml），不是普通 HTML，本版本无法解析。\n"
            "请在浏览器另存网页时选择「网页，仅 HTML」（.html），不要选「网页，单个文件」。",
        )

    # ---- 1) 人机校验页 ----
    if _looks_like_challenge_html(html, headers):
        return ImportInspection(
            IMPORT_KIND_CHALLENGE,
            [],
            "这个文件看起来是站点的**人机校验页**（“Just a moment...”），不是专辑页。\n"
            "  请在正常浏览器里完整打开专辑页、等到**曲目列表真正出现**之后，\n"
            "  再“另存为”保存 HTML（不是保存校验中的那个页面）。",
        )

    # ---- 2) 明显不是内容页 ----
    low = (html or "")[:2000].lower()
    if not html or len(html) < 200:
        return ImportInspection(
            IMPORT_KIND_ERROR, [], "这个文件太小了，看起来不是完整的专辑页 HTML。"
        )
    if low.lstrip().startswith(("{", "[")):
        return ImportInspection(
            IMPORT_KIND_ERROR, [], "这个文件是 JSON，不是网页，无法从中解析曲目。"
        )
    if "<html" not in low and "<table" not in low and "<a " not in low:
        return ImportInspection(
            IMPORT_KIND_ERROR, [], "这个文件不像网页 HTML，无法解析曲目。"
        )

    # ---- 3) 先识别文件自身属于哪个专辑（与用户地址无关）----
    src = identify_album_source(html)
    detected_url = src["album_url"]

    # 主机适配：页面里的曲目链接多为 root-relative（没有主机信息）。
    # 若用户地址的**路径 slug** 与识别出的 slug 一致、仅主机不同
    # （本地镜像 / mock 站 / www 前缀），以**用户地址**为主机 —— 它才是
    # "这份页面当初真正的来源"；只有 slug 不同才算"不同专辑"。
    #
    # "different_album" 时也保留这个 slug 一致性检查的意义：用户地址的
    # **主机**就是页面真正来源的候选（root-relative 链接无法自证主机），
    # 所以回填"网页中的专辑地址"时用它 —— 用户点确认后，地址框得到的是
    # 用户自己环境里可用的地址（例如本地镜像），而不是想当然的官方域名。
    address_try = (album_url or "").strip()
    user_host = ""
    if address_try:
        try:
            normalize_album_url(address_try)
            user_host = f"{urlsplit(address_try).scheme}://{urlsplit(address_try).netloc}"
        except DownloaderError:
            user_host = ""
    if detected_url and user_host:
        # 用户主机 ≠ 官方主机且 slug 与用户地址路径一致 → 用用户主机
        if src["album_slug"] and f"/{src['album_slug']}" in urlsplit(address_try).path.rstrip("/"):
            detected_url = f"{user_host}{urlsplit(detected_url).path}"
        elif src["album_slug"] and not same_album(address_try, detected_url):
            # 不同专辑：检测地址也按用户主机回填（slug 用页面识别出的）
            detected_url = f"{user_host}{urlsplit(src['album_url']).path}"

    base_kwargs = {
        "detected_url": detected_url,
        "page_title": src["title"],
        "evidence": src["evidence"],
        "conflicts": src["conflicts"],
    }

    def _resolved_address() -> str:
        """本次核对实际用于解析的地址：优先用页面识别出的，其次用户输入的。"""
        return detected_url or (album_url or "").strip()

    address = (album_url or "").strip()
    if address:
        try:
            normalize_album_url(address)
        except DownloaderError:
            address = ""   # 地址本身不合法：按空地址路径走，稍后给出提示

    # ---- 4) 地址与文件来源核对 ----
    relation = ""
    if not address:
        relation = "empty"
    elif detected_url and same_album(address, detected_url):
        relation = "match"
    elif is_song_page_url(address):
        relation = "song_page"
    elif detected_url:
        relation = "different_album"
    else:
        relation = "unresolved"

    def _make(kind: str, tracks: list, message: str) -> ImportInspection:
        """统一出口：带上 v008 的来源核对信息，避免遗漏 address_relation。"""
        return ImportInspection(
            kind, tracks, message,
            detected_url=detected_url,
            page_title=src["title"],
            evidence=src["evidence"],
            conflicts=src["conflicts"],
            matched_address=(relation == "match"),
            address_relation=relation,
        )

    # ---- 5) 分类与解析 ----
    if relation == "song_page":
        # 用户填的是曲目详情页地址。若文件本身能识别出专辑，可以自动纠正；
        # 否则让用户改填专辑地址。
        if detected_url:
            return _make(
                IMPORT_KIND_NO_TRACKS,
                [],
                "你填写的是一个**曲目详情页**地址（某首歌的页面），不是专辑页。\n"
                f"这个文件看起来属于专辑：{detected_url}\n"
                "确认后将从该专辑地址解析曲目。",
            )
        return _make(
            IMPORT_KIND_NO_TRACKS,
            [],
            "你填写的是一个**曲目详情页**地址（某首歌的页面），不是专辑页。\n"
            "请改填专辑页地址：形如 https://downloads.khinsider.com/game-soundtracks/album/<专辑名>。\n"
            "（无法从文件里自动识别出专辑地址，需要你提供。）",
        )

    if relation == "different_album":
        return _make(
            IMPORT_KIND_NO_TRACKS,
            [],
            "地址框里的专辑与导入的网页**不是同一张专辑**。\n"
            f"  地址框：{address}\n"
            f"  网页属于：{detected_url}\n"
            "请确认用哪一个；这不是「页面没有曲目」。",
        )

    if relation == "unresolved":
        # 页面自身识别不出来源，地址也给不出可靠对照 —— 不能猜，不启动解析
        reason = src["evidence"] or "页面里没有曲目表，也没有专辑地址标记"
        return _make(
            IMPORT_KIND_UNRESOLVED,
            [],
            "无法确定这个网页属于哪个专辑。\n  " + reason +
            "\n请核对保存的是否是专辑页；或先在地址框填好专辑地址再导入。",
        )

    # relation in {"empty", "match"}：可以解析了。
    # 关键：用"识别出的专辑地址"解析（它一定与页面同源），而不是用户输入的地址 ——
    # 这样"地址填错"永远不会把曲目全部滤光。
    parse_url = _resolved_address()
    try:
        tracks = parse_album_page(html, parse_url)
    except NoTracksFoundError as exc:
        if src["has_songlist"]:
            return _make(
                IMPORT_KIND_NO_TRACKS,
                [],
                "页面里有曲目表，但没有解析出属于该专辑的曲目链接。\n" + str(exc),
            )
        return _make(IMPORT_KIND_NO_TRACKS, [], str(exc))

    prefix = "（地址框为空，已按网页识别的专辑解析）" if relation == "empty" else ""
    return _make(
        IMPORT_KIND_ALBUM,
        tracks,
        f"已从导入的页面中解析出 {len(tracks)} 首曲目。{prefix}",
    )


def download_single_link(
    url: str,
    folder: Path,
    on_event: Callable[[str, dict], None],
    filename: Optional[str] = None,
    session: Optional[requests.Session] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> TrackResult:
    """下载**一个**公开的 MP3 链接到 folder（v007 新增的"单曲/链接下载"入口）。

    这是"单曲恢复"，**不是**整张专辑恢复 —— 它只处理用户粘进来的这一个链接。
    复用既有下载引擎：分块流式写盘、`.part` → 原子改名、已有文件跳过、
    文件名净化、协作式取消。返回一个 TrackResult 供界面汇总。

    filename 为空时，从 URL 末段（深度解码）推断；推断不出就用 "download.mp3"。
    """
    session = session or make_session()
    folder = Path(folder)

    try:
        link = normalize_album_url(url)  # 复用同一套 http/https + 域名校验
    except DownloaderError as exc:
        on_event("single_fail", {"url": url, "error": str(exc)})
        return TrackResult(name=url, status="failed", message=str(exc))

    # 推断文件名：URL 末段 → 净化 → 保证 .mp3 后缀
    if not filename:
        tail = unquote(urlsplit(link).path.rsplit("/", 1)[-1])
        filename = sanitize_filename(tail) or "download.mp3"
    if not filename.lower().endswith(".mp3"):
        filename += ".mp3"

    on_event("log", {"text": f"下载单个链接：{link}"})
    on_event("log", {"text": f"保存为：{filename}"})

    try:
        dest, written, skipped = download_file(
            session,
            link,
            folder,
            filename,
            on_progress=lambda done, total: on_event(
                "file_progress", {"written": done, "total": total}
            ),
            should_cancel=should_cancel,
            on_log=lambda text: on_event("log", {"text": f"  · {text}"}),
        )
    except CancelledError as exc:
        on_event("single_cancelled", {"url": link})
        return TrackResult(name=filename, status="cancelled", message=str(exc))
    except DownloaderError as exc:
        on_event("single_fail", {"url": link, "error": str(exc)})
        return TrackResult(name=filename, status="failed", message=str(exc))
    except OSError as exc:
        message = f"写文件失败：{exc}"
        on_event("single_fail", {"url": link, "error": message})
        return TrackResult(name=filename, status="failed", message=message)

    if skipped:
        message = f"已存在且大小一致，跳过：{dest.name}"
        on_event("log", {"text": f"  [跳过] {message}"})
        result = TrackResult(filename, "skipped", message, dest)
    else:
        message = f"完成（{written / 1024 / 1024:.2f} MB）-> {dest.name}"
        on_event("log", {"text": f"  [完成] {message}"})
        result = TrackResult(filename, "completed", message, dest)

    on_event("single_done", {"path": str(dest), "status": result.status, "bytes": written})
    return result


# ---------------------------------------------------------------------------
# 6. 诊断信息采集（v007 新增，脱敏）
# ---------------------------------------------------------------------------


#: 诊断时**允许**记录的公开响应头白名单。绝不包含 Cookie / Set-Cookie /
#: Authorization / Proxy-Authorization 等任何凭据类字段。
DIAG_HEADER_WHITELIST = (
    "content-type", "content-length", "server", "cf-mitigated", "cf-ray",
    "cf-cache-status", "location", "date", "cache-control", "vary",
    "x-frame-options", "content-encoding", "retry-after",
)

#: 明确**禁止**出现在诊断里的头（双保险，即使白名单误加也会被剔除/打码）。
DIAG_HEADER_DENYLIST = ("cookie", "set-cookie", "authorization", "proxy-authorization")


def summarize_response_for_diagnostics(response: requests.Response) -> dict:
    """把一次响应整理成"可以安全给人看"的诊断字典。

    只保留：状态码、最终 URL、耗时相关不可得（由调用方计时）、
    白名单响应头、以及页面结构特征（是否校验页 / 是否有曲目表）。

    **安全保证**：Cookie / Set-Cookie / Authorization 一律打码为 <redacted>，
    且默认根本不会出现在输出里。绝不返回响应体全文。
    """
    headers = {}
    for key, value in response.headers.items():
        kl = key.lower()
        if kl in DIAG_HEADER_DENYLIST:
            headers[key] = "<redacted>"
        elif kl in DIAG_HEADER_WHITELIST:
            headers[key] = value

    info = {
        "status_code": response.status_code,
        "final_url": response.url,
        "redirected": response.url != getattr(response, "request", None).url
        if getattr(response, "request", None) is not None else None,
        "headers": headers,
        "content_type": (response.headers.get("Content-Type") or "").split(";")[0].strip(),
        "challenge_like": _looks_like_challenge(response),
    }
    return info

