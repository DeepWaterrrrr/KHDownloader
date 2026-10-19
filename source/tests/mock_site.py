# -*- coding: utf-8 -*-
"""
mock_site.py —— 本地假站点，专门给测试用

为什么需要它？
    真实的 KHInsider 的**专辑页**确实会对普通程序请求下发 Cloudflare 人机校验
    （证据见 EXECUTION_LOG.md）。v1.0.0 起程序用"浏览器 TLS 指纹"（见
    khdownloader_transport.py）就能直接拿到专辑页，但那种走网络的行为不适合
    放进单元测试。所以"下载流程能不能跑通"仍然用一个跑在本机的小服务器来验证 ——
    它按 tests/fixtures/ 里的页面结构提供专辑页、曲目页和 MP3 文件。

    另外还提供 `/flaky` 系列端点：**第一次请求返回人机校验页，之后返回真专辑页**。
    v1.0.0 的"升级梯子"（换指纹重试）就是靠它离线验证的：不需要碰外网，
    也能证明"第一下被拦住 → 换指纹 → 真的救回来了"这条链路。

小技巧：同一个服务器用两个主机名访问
    http://localhost:端口/...    -> 当作"页面站"
    http://127.0.0.1:端口/...    -> 当作"文件站"
这样两边 netloc 不同，就能真实地测出"优先选跨主机的 MP3 链接"这条规则。
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

FIXTURES = Path(__file__).resolve().parent / "fixtures"

ALBUM_SLUG = "mock-album-2020"
ALBUM_NO_TABLE_SLUG = "mock-album-notable"
#: 曲目用的是"慢慢发"的 MP3，专门测下载中途取消。
SLOW_ALBUM_SLUG = "mock-album-slow"
#: 曲目用的 MP3 不声明 Content-Length，专门测"不编造百分比"。
NO_LENGTH_ALBUM_SLUG = "mock-album-nolen"
#: v1.0.0：第一次请求返回人机校验页、之后返回真专辑页（测"升级梯子真的能救回来"）。
FLAKY_ALBUM_SLUG = "mock-album-flaky"
#: `/flaky` 端点在放行之前，先拦几次。
FLAKY_CHALLENGE_HITS = 1

#: 假 mp3 文件大小。做成 150 KB，够验证"分块流式写盘"，又不至于让测试变慢。
FAKE_MP3_SIZE = 150 * 1024

#: "看起来声明了 1 MB、实际只给了 5 KB"的那个坏文件。
TRUNCATED_CLAIMED_SIZE = 1024 * 1024
TRUNCATED_ACTUAL_SIZE = 5 * 1024

#: 慢速文件：切小块慢慢发，给测试留出"下载到一半点取消"的窗口。
#: 大小要明显大于 khdownloader_core.CHUNK_SIZE（64 KB），这样下载会分成**多块**，
#: 才能验证"进度回调拿到递增的字节数"和"取消发生在两块之间"。
SLOW_FILE_SIZE = 512 * 1024
SLOW_CHUNK_SIZE = 16 * 1024
SLOW_CHUNK_DELAY = 0.02

#: 不声明 Content-Length 的文件（验证界面不编造百分比）。
NO_LENGTH_FILE_SIZE = 8 * 1024


def _fake_mp3_bytes(size: int = FAKE_MP3_SIZE) -> bytes:
    """造一段假的"音频"字节：开头带 ID3 标记，后面填 0。

    它当然不能播放，测试只关心"字节数对不对、写盘有没有写全"。
    """
    header = b"ID3\x03\x00\x00\x00\x00\x00\x00"
    return header + b"\x00" * (size - len(header))


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockKHSite/1.0"

    #: `/flaky` 端点已经收到过几次请求（用来实现"先拦后放"）。
    flaky_hits: dict = {}
    flaky_lock = threading.Lock()

    # 关掉默认的访问日志，测试输出才干净
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    # ------------------------------------------------------------------
    def _send_html(self, html: str, status: int = 200, extra_headers=None):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, content_type="audio/mpeg", status=200,
                    claimed_length=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(claimed_length if claimed_length is not None else len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _fixture(self, name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    def _send_slow(self):
        """把慢速文件切成很多小块慢慢发，中间留出停顿。

        用途：给"下载到一半按取消"这件事一个可复现的窗口。
        如果一口气全发出去，测试根本来不及取消。
        """
        import time as _time

        total = SLOW_FILE_SIZE
        body = _fake_mp3_bytes(total)
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(total))
        self.end_headers()
        chunk = SLOW_CHUNK_SIZE
        for offset in range(0, total, chunk):
            try:
                self.wfile.write(body[offset:offset + chunk])
                self.wfile.flush()
            except (ConnectionError, OSError):
                # 客户端中途断开（正是"取消"要测的场景）——安静收工即可，
                # 别往测试输出里喷一堆堆栈。
                self.close_connection = True
                return
            _time.sleep(SLOW_CHUNK_DELAY)  # 让测试有时间按下取消

    def _send_bytes_no_length(self):
        """发一小段音频，但**故意不写 Content-Length**。

        用途：验证界面在"不知道自己总共多大"时，不编造百分比进度。
        这里用 HTTP/1.0 风格：不写 Content-Length，靠关闭连接表示结束。
        """
        body = _fake_mp3_bytes(NO_LENGTH_FILE_SIZE)
        self.protocol_version = "HTTP/1.0"  # 不写长度时，靠关连接表示_body 结束
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        # 刻意不发送 Content-Length
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _base(self, host_kind: str) -> str:
        host = "localhost" if host_kind == "album" else "127.0.0.1"
        return f"http://{host}:{self.server.server_port}"

    # ------------------------------------------------------------------
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 规定的名字
        raw_path = self.path
        path = urlsplit(raw_path).path
        album_base = f"/game-soundtracks/album/{ALBUM_SLUG}"

        # ---- v1.0.0：先拦后放（证明"换指纹重试"真的能把页面救回来）----
        # 头 FLAKY_CHALLENGE_HITS 次请求回人机校验页，之后回真专辑页。
        flaky_base = f"/game-soundtracks/album/{FLAKY_ALBUM_SLUG}"
        if path == flaky_base:
            with self.flaky_lock:
                seen = self.flaky_hits.get(path, 0) + 1
                self.flaky_hits[path] = seen
            if seen <= FLAKY_CHALLENGE_HITS:
                self._send_html(
                    '<!DOCTYPE html><html><head><title>Just a moment...</title></head>'
                    '<body>challenges.cloudflare.com</body></html>',
                    status=403,
                    extra_headers={"cf-mitigated": "challenge"},
                )
                return
            self._send_html(self._fixture("album_page.html"))
            return

        # ---- 专辑页（有 table#songlist）----
        if path == album_base:
            self._send_html(self._fixture("album_page.html"))
            return

        # ---- 专辑页（没有 table#songlist，用来测退化路径）----
        if path == f"/game-soundtracks/album/{ALBUM_NO_TABLE_SLUG}":
            self._send_html(self._fixture("album_page_without_songlist.html"))
            return

        # ---- 曲目详情页：路径以本专辑前缀开头、且不以 change_log 结尾 ----
        if path.startswith(album_base + "/") and not path.endswith("change_log"):
            track_no = path.rsplit("/", 1)[-1][:2]  # "01" / "02" / "03"
            if track_no == "03":
                # 这一页故意没有 MP3 链接
                self._send_html(self._fixture("song_page_no_mp3.html"))
                return
            mp3_map = {
                "01": f"{self._base('files')}/files/ok.mp3",
                "02": f"{self._base('files')}/files/ok-2.mp3",
            }
            html = self._fixture("song_page.html").replace(
                "{{MP3_URL}}", mp3_map.get(track_no, f"{self._base('files')}/files/ok.mp3")
            )
            self._send_html(html)
            return

        # ---- 慢速专辑：曲目页给出的 MP3 是"慢慢发"的那个，用来测中途取消 ----
        slow_album_base = f"/game-soundtracks/album/{SLOW_ALBUM_SLUG}"
        if path == slow_album_base:
            self._send_html(self._fixture("slow_album_page.html"))
            return
        if path.startswith(slow_album_base + "/"):
            html = self._fixture("song_page.html").replace(
                "{{MP3_URL}}", f"{self._base('files')}/files/slow.mp3"
            )
            self._send_html(html)
            return

        # ---- 无长度专辑：MP3 不声明 Content-Length，用来测"不编造百分比" ----
        nolen_album_base = f"/game-soundtracks/album/{NO_LENGTH_ALBUM_SLUG}"
        if path == nolen_album_base:
            self._send_html(self._fixture("no_length_album_page.html"))
            return
        if path.startswith(nolen_album_base + "/"):
            html = self._fixture("song_page.html").replace(
                "{{MP3_URL}}", f"{self._base('files')}/files/no-length.mp3"
            )
            self._send_html(html)
            return

        # ---- 变更日志页（会被解析器当成"非曲目链接"排除掉）----
        if path.endswith("change_log"):
            self._send_html("<html><body>changelog</body></html>")
            return

        # ---- 文件站 ----
        if path in ("/files/ok.mp3", "/files/ok-2.mp3"):
            self._send_bytes(_fake_mp3_bytes())
            return
        if path == "/files/missing.mp3":
            self._send_html("<html><body>404 not found</body></html>", status=404)
            return
        if path == "/files/truncated.mp3":
            # 声明 1 MB，实际只发 5 KB —— 模拟"下到一半断了"
            self._send_bytes(_fake_mp3_bytes(TRUNCATED_ACTUAL_SIZE),
                             claimed_length=TRUNCATED_CLAIMED_SIZE)
            self.close_connection = True
            return

        # ---- 慢速端点：分很多小段慢慢发，专门用来测"下载中途取消" ----
        # 一次发一小块、块与块之间歇一小会儿，这样测试才有机会在传输过程中按下取消。
        if path == "/files/slow.mp3":
            self._send_slow()
            return

        # ---- 声明了长度、但一个字节都不发（测"没有 Content-Length"的行为要另开端点）----
        if path == "/files/no-length.mp3":
            self._send_bytes_no_length()
            return

        # ---- 模拟 Cloudflare 人机校验页 ----
        if path.startswith("/blocked"):
            # 真实环境里最常见的是 403，但托管式校验也可能用别的状态码回来，
            # 所以这里两个端点都留着：程序必须看"页面内容"，不能只看状态码。
            status = 503 if path.startswith("/blocked-503") else 403
            self._send_html(
                '<!DOCTYPE html><html><head><title>Just a moment...</title></head>'
                '<body>challenges.cloudflare.com</body></html>',
                status=status,
                extra_headers={"cf-mitigated": "challenge"},
            )
            return

        # ---- 状态码是 200、但内容其实是校验页（最阴险的一种）----
        if path.startswith("/blocked-200"):
            self._send_html(
                '<!DOCTYPE html><html><head><title>Just a moment...</title></head>'
                '<body>challenges.cloudflare.com</body></html>',
                status=200,
            )
            return

        # ---- v007 新增：普通 403（**没有**任何校验页特征）----
        # 用来验证：软件不能仅凭 403 就判定为 Cloudflare 人机校验。
        if path.startswith("/denied"):
            self._send_html(
                "<html><body><h1>403 Forbidden</h1><p>Access denied.</p></body></html>",
                status=403,
            )
            return

        # ---- v007 新增：HTTP 429 限流 ----
        if path.startswith("/limited"):
            self._send_html(
                "<html><body>429 Too Many Requests</body></html>",
                status=429,
                extra_headers={"Retry-After": "30"},
            )
            return

        # ---- v007 新增：声明 audio/mpeg，但内容其实是 HTML（伪装成 MP3 的错误页）----
        # 用来验证：下载入口必须拒收"不是音频"的内容，绝不把它存成 .mp3。
        if path == "/files/fake-html.mp3":
            self._send_bytes(
                b"<!DOCTYPE html><html><body>not audio at all</body></html>",
                content_type="text/html; charset=UTF-8",
            )
            return
        # 同理，但用 application/json
        if path == "/files/fake-json.mp3":
            self._send_bytes(b'{"error":"not audio"}', content_type="application/json")
            return

        # ---- 其他一律 404 ----
        self._send_html("<html><body>404</body></html>", status=404)


class MockSite:
    """上下文管理器：with MockSite() as site: ... site.album_url ..."""

    def __init__(self):
        self._server = None
        self._thread = None

    def __enter__(self):
        # 端口写 0 = 让系统随便挑一个空闲端口，避免和别的程序撞车
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def port(self) -> int:
        return self._server.server_port

    @property
    def album_url(self) -> str:
        return f"http://localhost:{self.port}/game-soundtracks/album/{ALBUM_SLUG}"

    @property
    def album_no_table_url(self) -> str:
        return f"http://localhost:{self.port}/game-soundtracks/album/{ALBUM_NO_TABLE_SLUG}"

    @property
    def slow_album_url(self) -> str:
        """曲目 MP3 慢慢发的专辑，用于测试下载中途取消。"""
        return f"http://localhost:{self.port}/game-soundtracks/album/{SLOW_ALBUM_SLUG}"

    @property
    def no_length_album_url(self) -> str:
        """曲目 MP3 不声明 Content-Length 的专辑。"""
        return f"http://localhost:{self.port}/game-soundtracks/album/{NO_LENGTH_ALBUM_SLUG}"

    @property
    def flaky_album_url(self) -> str:
        """第一下返回人机校验页、之后返回真专辑页的专辑（v1.0.0 升级梯子用）。"""
        return f"http://localhost:{self.port}/game-soundtracks/album/{FLAKY_ALBUM_SLUG}"

    def reset_flaky(self) -> None:
        """把 `/flaky` 的计数清零，让"先拦后放"能重新来一遍。"""
        with _Handler.flaky_lock:
            _Handler.flaky_hits.clear()

    @property
    def blocked_url(self) -> str:
        return f"http://localhost:{self.port}/blocked/album"

    @property
    def blocked_503_url(self) -> str:
        """状态码 503 的人机校验页（校验不一定只用 403）。"""
        return f"http://localhost:{self.port}/blocked-503/album"

    @property
    def blocked_200_url(self) -> str:
        """状态码 200、但内容其实是校验页——最容易骗过程序的那种。"""
        return f"http://localhost:{self.port}/blocked-200/album"

    @property
    def denied_url(self) -> str:
        """普通 403（没有任何校验页特征）——不应被判成 Cloudflare。"""
        return f"http://localhost:{self.port}/denied/album"

    @property
    def limited_url(self) -> str:
        """HTTP 429 限流。"""
        return f"http://localhost:{self.port}/limited/album"

    @property
    def fake_html_mp3_url(self) -> str:
        """声明是 .mp3，但实际返回 text/html —— 用来验证"拒收非音频内容"。"""
        return f"http://127.0.0.1:{self.port}/files/fake-html.mp3"

    @property
    def fake_json_mp3_url(self) -> str:
        """声明是 .mp3，但实际返回 application/json。"""
        return f"http://127.0.0.1:{self.port}/files/fake-json.mp3"

    def url(self, path: str) -> str:
        return f"http://localhost:{self.port}{path}"

    def files_url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"
