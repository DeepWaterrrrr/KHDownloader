# -*- coding: utf-8 -*-
"""khdownloader_transport.py —— 网络传输层（v1.0.0 新增）

## 这个文件为什么存在

`downloads.khinsider.com` 的**专辑页**挂在 Cloudflare 后面，对"程序化访问"下发
人机校验（HTTP 403 + `cf-mitigated: challenge`）。实测（2026-10-15，同一出口 IP、
同一时刻、同一 URL）：

| 客户端 | TLS/HTTP2 指纹 | 结果 |
|---|---|---|
| 纯 `requests`（Python + OpenSSL） | Python 的指纹 | **403 + `cf-mitigated: challenge`** |
| `curl_cffi` 以 Chrome 指纹请求 | 真 Chrome 的指纹 | **200 + 完整专辑页（含曲目表）** |

结论：被站点识别的是**客户端的网络指纹**（TLS 握手的 JA3/JA4、HTTP/2 帧设置等），
**不是** `User-Agent` 那几行请求头。v008 试过"换完整浏览器请求头"，仍 403 —— 正是这个
原因。所以本层做的事情是：**让程序发出与真浏览器一致的 TLS/HTTP2 握手**。

## 分层策略（从便宜到贵，逐级升级）

| 层 | 做法 | 代价 | 依赖 |
|---|---|---|---|
| L0 | `curl_cffi` + `impersonate="chrome"`（默认就走这层） | 毫秒级 | `curl_cffi` |
| L1 | 换其它浏览器指纹重试（chrome136 / safari184 / firefox135 …） | 毫秒级 | `curl_cffi` |
| L2 | 真浏览器过一次校验，取 `cf_clearance` + UA 塞回 HTTP 层重试 | 数秒 | `playwright` + 浏览器内核 |
| L3 | 真浏览器直接把渲染后的 HTML 交给上层解析 | 数秒 | 同 L2 |
| L4 | 借本机已装的 Chrome/Edge 无头渲染（`--headless=new --dump-dom`） | 数秒 | 系统里有 Chrome/Edge |

L0 在 2026-10-15 的实测里一次就拿到 200，L1–L4 是给"站点以后收紧策略"留的后手。
L4 是**打包成 EXE 之后**唯一还能用的兜底层：playwright 的浏览器内核没法打进 EXE，
而用户机器上通常已经有 Edge/Chrome。

## 设计约束（改这个文件前先看）

1. **不改上层语义**：`make_session()` 仍然返回 `requests.Session` 的子类，
   上层原有的 `except requests.Timeout` / `.iter_content()` / `with response:` /
   `response.headers` / `response.encoding` 全部照常工作。
2. **本地与明文 http 一律走原生 requests**：测试用的 `tests/mock_site.py` 是
   `http://127.0.0.1:PORT`，测试行为与 v008 完全一致（也因此测试不碰外网）。
3. **缺依赖不报错**：没装 `curl_cffi` / `playwright` 时自动降级回原生 requests，
   程序照常能跑（只是又会看到那条人机校验提示）。
4. **开关**（环境变量，可临时排查用）：
   - `KHD_TRANSPORT=auto|requests|impersonate`（默认 `auto`）
   - `KHD_IMPERSONATE=chrome136`（指定默认指纹）
   - `KHD_BROWSER_FALLBACK=0`（关掉 L2/L3，只留指纹层）
"""

from __future__ import annotations

import inspect
import os
import threading
import time
from typing import Any, Callable, Iterable, Optional

import requests

# ---------------------------------------------------------------------------
# 开关与可用性探测
# ---------------------------------------------------------------------------

MODE_AUTO = "auto"
MODE_REQUESTS = "requests"
MODE_IMPERSONATE = "impersonate"

#: 指纹升级顺序。第一个是默认值；列表在运行时按"当前 curl_cffi 真的支持哪些"过滤。
PREFERRED_IMPERSONATIONS = (
    "chrome", "chrome136", "chrome145", "chrome131", "safari184", "firefox135", "edge",
)

#: 真浏览器兜底（L2/L3）的最长等待时间，单位毫秒。
BROWSER_NAVIGATE_TIMEOUT_MS = 45_000
BROWSER_CHALLENGE_WAIT_S = 25.0

try:  # 缺了就降级，不影响程序启动
    from curl_cffi import requests as _cffi
    from curl_cffi.requests import exceptions as _cffi_exceptions

    CURL_CFFI_AVAILABLE = True
except Exception:  # pragma: no cover - 环境相关
    _cffi = None
    _cffi_exceptions = None
    CURL_CFFI_AVAILABLE = False

try:
    # 注意：这里**故意**用 find_spec 而不是 `import playwright`。
    # PyInstaller 会把 `import` 到的包整包打进 EXE（playwright 自带 node 驱动，
    # 体积会翻好几倍），find_spec 只查"装没装"，静态分析看不到，不会误打包。
    from importlib.util import find_spec as _find_spec

    PLAYWRIGHT_AVAILABLE = _find_spec("playwright") is not None
except Exception:  # pragma: no cover - 环境相关
    PLAYWRIGHT_AVAILABLE = False


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def transport_mode() -> str:
    """当前传输模式：auto（默认）/ requests / impersonate。"""
    mode = _env("KHD_TRANSPORT", MODE_AUTO).lower()
    return mode if mode in (MODE_AUTO, MODE_REQUESTS, MODE_IMPERSONATE) else MODE_AUTO


def browser_fallback_enabled() -> bool:
    """L2/L3（真浏览器兜底）是否启用。默认启用，`KHD_BROWSER_FALLBACK=0` 关掉。"""
    return _env("KHD_BROWSER_FALLBACK", "1").lower() not in ("0", "false", "no", "off")


def _available_impersonations() -> tuple[str, ...]:
    """当前 curl_cffi 支持的指纹名（拿不到就用保守默认）。"""
    if not CURL_CFFI_AVAILABLE:
        return ()
    try:
        from typing import get_args

        from curl_cffi.requests.impersonate import BrowserTypeLiteral

        names = tuple(get_args(BrowserTypeLiteral))
        return names or ("chrome",)
    except Exception:  # pragma: no cover - 版本差异
        return ("chrome",)


def impersonation_ladder(default: Optional[str] = None) -> list[str]:
    """返回这一轮要依次尝试的指纹列表（默认指纹排第一）。"""
    available = _available_impersonations()
    preferred = list(PREFERRED_IMPERSONATIONS)
    head = default or _env("KHD_IMPERSONATE") or ""
    if head:
        preferred.insert(0, head)
    ladder: list[str] = []
    for name in preferred:
        if name in ladder:
            continue
        if not available or name in available:
            ladder.append(name)
    if not ladder:
        ladder = [head] if head else ["chrome"]
    return ladder


def describe() -> dict:
    """给诊断工具/日志用的一句话状态。"""
    return {
        "mode": transport_mode(),
        "curl_cffi": CURL_CFFI_AVAILABLE,
        "playwright": PLAYWRIGHT_AVAILABLE,
        "system_browser": find_system_browser() or "",
        "browser_fallback": browser_fallback_enabled(),
        "ladder": impersonation_ladder(),
    }


# ---------------------------------------------------------------------------
# 响应适配：把 curl_cffi 的 Response 包成"requests.Response 的形状"
# ---------------------------------------------------------------------------


class _SyntheticRequest:
    """curl_cffi 的 Response 不一定有 .request，这里补一个最小形状。

    只为了 `summarize_response_for_diagnostics()` 里 `response.request.url` 那句能跑。
    """

    __slots__ = ("url", "method", "headers")

    def __init__(self, url: str, method: str = "GET", headers: Optional[dict] = None):
        self.url = url
        self.method = method
        self.headers = headers or {}


class TransportResponse:
    """curl_cffi Response → requests.Response 兼容包装。

    为什么要包一层（而不是直接用 curl_cffi 的 Response）：
      · 它**不支持** `with response:`（上层 `download_file()` 用了），这里补上；
      · 它**没有** `apparent_encoding`（上层 `fetch_html()` 用了），这里补上；
      · 它 `.encoding` 是只读属性，上层会赋值（日文曲名不乱码那一段），这里做成可写；
      · 传输中途断线时它抛的是 curl_cffi 的异常，这里翻译成 requests 的异常族，
        上层的 `except requests.RequestException` 才能照常接住。
    """

    __slots__ = ("_response", "_encoding", "_text", "last_url", "last_method")

    def __init__(self, response: Any, url: str, method: str = "GET"):
        object.__setattr__(self, "_response", response)
        object.__setattr__(self, "_encoding", None)
        object.__setattr__(self, "_text", None)
        object.__setattr__(self, "last_url", url)
        object.__setattr__(self, "last_method", method)

    # ---- 明确定义的属性 ----

    @property
    def status_code(self) -> int:
        return int(self._response.status_code)

    @property
    def headers(self):
        return self._response.headers

    @property
    def content(self) -> bytes:
        try:
            return self._response.content
        except Exception as exc:  # noqa: BLE001
            raise map_transport_exception(exc) from exc

    @property
    def url(self) -> str:
        return str(getattr(self._response, "url", "") or self.last_url)

    @property
    def request(self):
        inner = getattr(self._response, "request", None)
        if inner is not None and getattr(inner, "url", None):
            return inner
        return _SyntheticRequest(self.url, self.last_method)

    @property
    def cookies(self):
        return getattr(self._response, "cookies", None)

    @property
    def history(self):
        return list(getattr(self._response, "history", []) or [])

    @property
    def encoding(self) -> Optional[str]:
        if self._encoding:
            return self._encoding
        return getattr(self._response, "encoding", None)

    @encoding.setter
    def encoding(self, value: Optional[str]) -> None:
        object.__setattr__(self, "_encoding", value)
        object.__setattr__(self, "_text", None)
        try:
            self._response.encoding = value
        except Exception:  # noqa: BLE001 - 只读也没关系，我们自己按 _encoding 解码
            pass

    @property
    def apparent_encoding(self) -> str:
        """requests 有、curl_cffi 没有的属性。用 charset_normalizer 猜（requests 的同一个后端）。"""
        try:
            import charset_normalizer

            best = charset_normalizer.from_bytes(self.content).best()
            if best is not None and best.encoding:
                return best.encoding
        except Exception:  # noqa: BLE001
            pass
        return "utf-8"

    @property
    def text(self) -> str:
        cached = self._text
        if cached is not None:
            return cached
        encoding = self.encoding or self.apparent_encoding or "utf-8"
        try:
            decoded = self.content.decode(encoding, errors="replace")
        except LookupError:
            decoded = self.content.decode("utf-8", errors="replace")
        object.__setattr__(self, "_text", decoded)
        return decoded

    @text.setter
    def text(self, value: str) -> None:
        object.__setattr__(self, "_text", value)

    # ---- 方法 ----

    def iter_content(self, chunk_size: int = 1, decode_unicode: bool = False):
        """分块产出响应体；断流时把 curl 的异常翻成 requests 的异常族。

        注意一个坑：curl_cffi 在**非 stream 请求**上调用 `iter_content()` 会直接
        `assert self.queue and self.curl, "stream mode is not enabled."`，而 requests
        允许（就是把已经缓冲好的 body 分块吐出来）。这里补上后者：断言失败时退化成
        "对已缓冲内容分块"，让上层不用关心这个差别。
        """
        try:
            # 这里故意传 chunk_size=None：curl_cffi 只要收到一个真值就会
            # warnings.warn("chunk_size is ignored…", CurlCffiWarning)。
            # 反正它无论如何都不按这个尺寸切（curl 层没法控制），
            # 与其让这条警告跟着用户的下载日志到处跑，不如直接不触发它。
            for chunk in self._response.iter_content(
                chunk_size=None, decode_unicode=decode_unicode
            ):
                if chunk:
                    yield chunk
            return
        except AssertionError as exc:
            if "stream mode" not in str(exc):
                raise map_transport_exception(exc) from exc
        except Exception as exc:  # noqa: BLE001
            raise map_transport_exception(exc) from exc

        # ---- 退化路径：没有开 stream，就把缓冲好的内容自己切块 ----
        try:
            data = self.content
        except Exception as exc:  # noqa: BLE001
            raise map_transport_exception(exc) from exc
        step = chunk_size if isinstance(chunk_size, int) and chunk_size > 0 else 1
        for offset in range(0, len(data), step):
            yield data[offset:offset + step]

    def iter_lines(self, *args, **kwargs):
        return self._response.iter_lines(*args, **kwargs)

    def json(self, *args, **kwargs):
        return self._response.json(*args, **kwargs)

    def raise_for_status(self):
        return self._response.raise_for_status()

    def close(self) -> None:
        try:
            self._response.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- requests.Response 支持 `with response:`，这里补齐 ----

    def __enter__(self) -> "TransportResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        self.close()
        return False

    # ---- 其它属性全部透传（.ok / .reason / .is_redirect / ...）----

    def __getattr__(self, name: str):
        return getattr(self._response, name)


def map_transport_exception(exc: BaseException) -> requests.RequestException:
    """把 curl_cffi 的异常翻译成 requests 的异常族，让上层 `except` 照常工作。

    curl_cffi 的异常层级是独立的（`curl_cffi.requests.exceptions.*`），
    直接往外抛的话，上层的 `except requests.Timeout` 一句也接不住。
    """
    if isinstance(exc, requests.RequestException):
        return exc
    name = type(exc).__name__
    mro = {cls.__name__ for cls in type(exc).__mro__}
    text = str(exc).lower()
    if "timeout" in name.lower() or "timed out" in text or "(28)" in text:
        return requests.Timeout(str(exc))
    if mro & {"ConnectionError", "DNSError", "CurlError", "ProxyError"}:
        return requests.ConnectionError(str(exc))
    return requests.RequestException(str(exc))


# ---------------------------------------------------------------------------
# 请求指纹：判断"这次要不要换指纹"
# ---------------------------------------------------------------------------


def _is_remote_https(url: str) -> bool:
    """只有 https 的远端请求才值得换指纹。

    明文 http（本地 mock 站点、内网地址）一律走原生 requests —— 一是没有指纹问题，
    二是保证测试不碰外网、行为可复现。

    例外：`KHD_TRANSPORT_ALLOW_LOCAL=1` 时连 http 也走指纹层。这是**测试钩子** ——
    让"升级梯子"能在本地假站点上离线跑一遍（tests/test_core.py 的
    TestEscalationLadder 用它），不必真的去打外部站点。
    """
    if url.lower().startswith("https://"):
        return True
    return _env("KHD_TRANSPORT_ALLOW_LOCAL", "0").lower() in ("1", "true", "yes", "on")


def looks_like_challenge(response: Any, body_available: bool = True) -> bool:
    """这个响应是不是 Cloudflare 人机校验页（与 core 里的判据保持一致）。"""
    try:
        if str(response.headers.get("cf-mitigated", "")).lower() == "challenge":
            return True
    except Exception:  # noqa: BLE001
        pass
    if not body_available:
        return False
    try:
        head = response.text[:2000].lower()
    except Exception:  # noqa: BLE001
        return False
    return "<title>just a moment" in head or "challenges.cloudflare.com" in head


# ---------------------------------------------------------------------------
# requests.Session 兼容层
# ---------------------------------------------------------------------------


class ImpersonatingSession(requests.Session):
    """https 请求走浏览器指纹，其余走原生 requests；对外完全是个 requests.Session。

    用法与 requests.Session 一致：`session.get(url, timeout=(10, 30))`。
    额外记录 `session.transport_tier`（这一次实际用了哪层），供日志/诊断使用。
    """

    def __init__(
        self,
        impersonate: Optional[str] = None,
        *,
        mode: Optional[str] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ):
        super().__init__()
        self.impersonate = impersonate or _env("KHD_IMPERSONATE") or "chrome"
        self.transport_mode = mode or transport_mode()
        self.on_log = on_log
        self.transport_tier = "requests"
        self._curl_sessions: dict[str, Any] = {}
        self._lock = threading.Lock()

    # ---- 内部 ----

    def log(self, text: str) -> None:
        if self.on_log is not None:
            try:
                self.on_log(text)
            except Exception:  # noqa: BLE001 - 日志永远不该影响下载
                pass

    def _curl_session(self, impersonate: str):
        with self._lock:
            session = self._curl_sessions.get(impersonate)
            if session is None:
                session = _cffi.Session(impersonate=impersonate)
                self._curl_sessions[impersonate] = session
            return session

    def _should_impersonate(self, url: str) -> bool:
        if self.transport_mode == MODE_REQUESTS:
            return False
        if not _is_remote_https(url):
            return False
        if not CURL_CFFI_AVAILABLE:
            if self.transport_mode == MODE_IMPERSONATE:
                self.log("未安装 curl_cffi（pip install curl_cffi），本次退回普通请求。")
            return False
        return True

    def _merged_headers(self, extra: Optional[dict] = None) -> dict:
        headers = dict(self.headers)
        if extra:
            headers.update(extra)
        return headers

    def _sync_cookies_in(self, curl_session) -> None:
        """把 requests 这一侧的 cookie 交给 curl 这一侧（手工导入 cf_clearance 时要用）。"""
        try:
            jar = requests.utils.dict_from_cookiejar(self.cookies)
        except Exception:  # noqa: BLE001
            return
        for key, value in (jar or {}).items():
            try:
                curl_session.cookies.set(key, value)
            except Exception:  # noqa: BLE001
                pass

    def _sync_cookies_out(self, curl_session) -> None:
        """把 curl 这一侧新拿到的 cookie 回灌给 requests 这一侧（诊断/复用要看）。"""
        try:
            jar = curl_session.cookies.get_dict()
        except Exception:  # noqa: BLE001
            return
        for key, value in (jar or {}).items():
            try:
                self.cookies.set(key, value)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _curl_kwargs(kwargs: dict) -> dict:
        """按 curl_cffi 真正接受的参数名过滤（例如 requests 的 hooks/cert 要去掉）。"""
        allowed = _CURL_KWARGS
        return {key: value for key, value in kwargs.items() if key in allowed}

    def _curl_request(self, method: str, url: str, impersonate: str, kwargs: dict):
        curl_session = self._curl_session(impersonate)
        self._sync_cookies_in(curl_session)
        call_kwargs = self._curl_kwargs(kwargs)
        call_kwargs["headers"] = self._merged_headers(call_kwargs.get("headers"))
        response = curl_session.request(method, url, impersonate=impersonate, **call_kwargs)
        self._sync_cookies_out(curl_session)
        return TransportResponse(response, url=url, method=method)

    # ---- 公开接口（与 requests.Session 同名）----

    def request(self, method: str, url: str, **kwargs):  # type: ignore[override]
        if not self._should_impersonate(url):
            self.transport_tier = "requests"
            return super().request(method, url, **kwargs)
        try:
            response = self._curl_request(method, url, self.impersonate, kwargs)
        except requests.RequestException:
            raise
        except BaseException as exc:  # noqa: BLE001 - 统一翻成 requests 异常族
            raise map_transport_exception(exc) from exc
        self.transport_tier = f"impersonate:{self.impersonate}"
        return response

    def raw_impersonated_request(  # noqa: D401 - 内部/工具用
        self,
        method: str,
        url: str,
        *,
        impersonate: str,
        headers: Optional[dict] = None,
        cookies: Optional[dict] = None,
        timeout=(10, 30),
        stream: bool = False,
    ):
        """用指定指纹 + 指定 cookie 发一次请求（升级梯子 L1/L2 用）。"""
        if not CURL_CFFI_AVAILABLE:
            raise requests.RequestException("curl_cffi 不可用")
        curl_session = self._curl_session(impersonate)
        if cookies:
            for key, value in cookies.items():
                try:
                    curl_session.cookies.set(key, value)
                except Exception:  # noqa: BLE001
                    pass
        self._sync_cookies_in(curl_session)
        response = curl_session.request(
            method,
            url,
            impersonate=impersonate,
            headers=self._merged_headers(headers),
            timeout=timeout,
            stream=stream,
            allow_redirects=True,
        )
        self._sync_cookies_out(curl_session)
        self.transport_tier = f"impersonate:{impersonate}"
        return TransportResponse(response, url=url, method=method)

    def imported_cookies(self) -> dict:
        """当前会话里的 cookie（含浏览器兜底拿到、或用户手工导入的 cf_clearance）。"""
        try:
            return requests.utils.dict_from_cookiejar(self.cookies) or {}
        except Exception:  # noqa: BLE001
            return {}

    def close(self) -> None:
        with self._lock:
            sessions = list(self._curl_sessions.values())
            self._curl_sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
        return super().close()


#: curl_cffi Session.request 真正接受的参数名（启动时探测一次）。
try:  # pragma: no cover - 依赖版本
    _CURL_KWARGS = {
        name
        for name in inspect.signature(_cffi.Session.request).parameters
        if name not in {"self", "method", "url"}
    }
except Exception:  # pragma: no cover
    _CURL_KWARGS = {
        "params", "data", "json", "headers", "cookies", "files", "auth", "timeout",
        "allow_redirects", "max_redirects", "proxies", "proxy", "proxy_auth", "verify",
        "referer", "accept_encoding", "content_callback", "default_headers", "stream",
    }


def make_session(
    impersonate: Optional[str] = None,
    *,
    on_log: Optional[Callable[[str], None]] = None,
) -> requests.Session:
    """建一个带浏览器指纹的 Session（缺依赖时自动退回原生 requests）。"""
    if transport_mode() == MODE_REQUESTS or not CURL_CFFI_AVAILABLE:
        session = requests.Session()
        session.on_log = on_log if on_log is not None else None  # type: ignore[attr-defined]
        return session
    return ImpersonatingSession(impersonate, on_log=on_log)


# ---------------------------------------------------------------------------
# 真浏览器兜底（L2 / L3）
# ---------------------------------------------------------------------------


class BrowserBridge:
    """用真浏览器过一次 Cloudflare 校验，把会话交回给 HTTP 层。

    `harvest(url)` 返回：
        {"cookies": {...}, "user_agent": str, "html": str} —— 成功
        None —— 没装 playwright / 没装浏览器内核 / 校验没过去（调用方按原样报错即可）
    """

    def __init__(
        self,
        headless: bool = True,
        navigate_timeout_ms: int = BROWSER_NAVIGATE_TIMEOUT_MS,
        challenge_wait_s: float = BROWSER_CHALLENGE_WAIT_S,
    ):
        self.headless = headless
        self.navigate_timeout_ms = navigate_timeout_ms
        self.challenge_wait_s = challenge_wait_s

    @staticmethod
    def _challenge_gone(html: str, cookies: dict) -> bool:
        head = (html or "")[:4000].lower()
        looks_like_gate = "<title>just a moment" in head or "challenges.cloudflare.com" in head
        return bool(cookies.get("cf_clearance")) and not looks_like_gate

    def harvest(self, url: str, on_log: Optional[Callable[[str], None]] = None) -> Optional[dict]:
        if not PLAYWRIGHT_AVAILABLE:
            return None

        def log(text: str) -> None:
            if on_log is not None:
                try:
                    on_log(text)
                except Exception:  # noqa: BLE001
                    pass

        try:
            from playwright.sync_api import sync_playwright
        except Exception:  # noqa: BLE001
            return None

        log("改用真浏览器过一次人机校验（首次可能要几秒）…")
        try:
            with sync_playwright() as driver:
                browser = driver.chromium.launch(headless=self.headless)
                try:
                    context = browser.new_context(
                        locale="en-US",
                        viewport={"width": 1366, "height": 900},
                    )
                    page = context.new_page()
                    page.goto(url, wait_until="domcontentloaded",
                              timeout=self.navigate_timeout_ms)
                    deadline = time.time() + self.challenge_wait_s
                    cookies: dict = {}
                    html = ""
                    while True:
                        cookies = {c["name"]: c["value"] for c in context.cookies()}
                        html = page.content()
                        if self._challenge_gone(html, cookies):
                            break
                        if time.time() >= deadline:
                            break
                        page.wait_for_timeout(1000)
                    user_agent = page.evaluate("navigator.userAgent")
                    return {"cookies": cookies, "user_agent": user_agent, "html": html}
                finally:
                    try:
                        browser.close()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001 - 兜底路径，失败就退回原错误
            log(f"浏览器兜底不可用（{type(exc).__name__}: {exc}），按原样报告拦截。")
            return None


def system_browser_enabled() -> bool:
    """L4（直接用系统里已有的 Chrome/Edge 渲染页面）是否启用。默认启用。"""
    return _env("KHD_SYSTEM_BROWSER", "1").lower() not in ("0", "false", "no", "off")


def find_system_browser() -> Optional[str]:
    """找本机的 Chrome / Edge 可执行文件（找不到返回 None）。

    这一层存在的意义：**打包成 EXE 之后也能用**。playwright 的浏览器内核没法
    一起打进 EXE（体积太大），但用户的 Windows 上通常本来就装了 Edge/Chrome，
    直接借用它跑一次 `--headless=new --dump-dom` 就能拿到过完校验的页面。
    """
    candidates: list[str] = []
    local = os.environ.get("LOCALAPPDATA") or ""
    pf = os.environ.get("PROGRAMFILES") or r"C:\Program Files"
    pf86 = os.environ.get("PROGRAMFILES(X86)") or r"C:\Program Files (x86)"
    for root in (pf, pf86, local):
        if not root:
            continue
        candidates += [
            os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(root, "Chromium", "Application", "chrome.exe"),
        ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def render_with_system_browser(
    url: str,
    on_log: Optional[Callable[[str], None]] = None,
    budget_ms: int = 20_000,
) -> Optional[str]:
    """借本机 Chrome/Edge 无头渲染页面，返回渲染后的 HTML（失败返回 None）。"""
    import subprocess
    import tempfile

    if not system_browser_enabled():
        return None
    executable = find_system_browser()
    if not executable:
        return None

    def log(text: str) -> None:
        if on_log is not None:
            try:
                on_log(text)
            except Exception:  # noqa: BLE001
                pass

    log(f"改用本机浏览器渲染页面（{os.path.basename(executable)}）…")
    with tempfile.TemporaryDirectory(prefix="khd_browser_") as profile:
        command = [
            executable,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            f"--user-data-dir={profile}",
            f"--virtual-time-budget={int(budget_ms)}",
            "--dump-dom",
            url,
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=90,
                creationflags=creationflags,
            )
        except Exception as exc:  # noqa: BLE001 - 兜底路径，失败就退回原错误
            log(f"本机浏览器渲染失败（{type(exc).__name__}: {exc}）。")
            return None
    html = (completed.stdout or b"").decode("utf-8", errors="replace")
    if not html.strip():
        log("本机浏览器没吐出内容，按原样报告拦截。")
        return None
    return html


# ---------------------------------------------------------------------------
# 升级梯子：L1 指纹轮换 → L2 浏览器 cookie → L3 浏览器 HTML → L4 本机浏览器
# ---------------------------------------------------------------------------


def _escalate_targets(session: requests.Session) -> list[str]:
    default = getattr(session, "impersonate", None)
    return impersonation_ladder(default)


def recover_html(
    session: requests.Session,
    url: str,
    *,
    headers: Optional[dict] = None,
    method: str = "GET",
    on_log: Optional[Callable[[str], None]] = None,
) -> tuple[Optional[Any], Optional[str], str]:
    """专辑页/详情页被校验拦住时，逐级升级取回真正的 HTML。

    返回 `(response, html, tier)`：
      · response 不为 None → 用这个响应（已通过校验，正常走后面的解析流程）
      · html 不为 None     → 浏览器已经渲染好了页面，直接用这段 HTML
      · 两个都是 None      → 所有层都没过，调用方按原样抛出人机校验错误
    """
    def log(text: str) -> None:
        if on_log is not None:
            try:
                on_log(text)
            except Exception:  # noqa: BLE001
                pass

    if not _is_remote_https(url):
        return None, None, "skipped:not-https"
    tried: list[str] = []
    if not CURL_CFFI_AVAILABLE:
        if not PLAYWRIGHT_AVAILABLE or not browser_fallback_enabled():
            return None, None, "skipped:no-curl_cffi"
    else:
        # ---- L1：换浏览器指纹 ----
        for candidate in _escalate_targets(session):
            tried.append(candidate)
            try:
                response = session.raw_impersonated_request(  # type: ignore[attr-defined]
                    method, url, impersonate=candidate, headers=headers,
                )
            except Exception as exc:  # noqa: BLE001
                log(f"指纹 {candidate} 请求失败：{type(exc).__name__}")
                continue
            if not looks_like_challenge(response):
                log(f"已切换浏览器指纹（{candidate}）通过站点的程序化访问校验。")
                return response, None, f"L1:impersonate={candidate}"
        log("指纹轮换没能通过校验，尝试真浏览器兜底…")

    if not browser_fallback_enabled():
        return None, None, "L1-failed:" + "/".join(tried or ["（未尝试）"])

    bridge = BrowserBridge()
    harvested = bridge.harvest(url, on_log=on_log)
    if harvested:
        cookies = harvested.get("cookies") or {}
        user_agent = harvested.get("user_agent") or ""
        html = harvested.get("html") or ""

        # ---- L2：拿浏览器的 cf_clearance + UA 回到 HTTP 层重试 ----
        if CURL_CFFI_AVAILABLE and cookies:
            extra = {"User-Agent": user_agent} if user_agent else None
            for candidate in _escalate_targets(session):
                try:
                    response = session.raw_impersonated_request(  # type: ignore[attr-defined]
                        method, url, impersonate=candidate, headers=extra, cookies=cookies,
                    )
                except Exception as exc:  # noqa: BLE001
                    log(f"带浏览器会话重试（{candidate}）失败：{type(exc).__name__}")
                    continue
                if not looks_like_challenge(response):
                    log("已用浏览器会话（cf_clearance）通过校验。")
                    return response, None, f"L2:browser-cookie+{candidate}"

        # ---- L3：浏览器已经渲染出真页面，直接用它的 HTML ----
        if html and not looks_like_challenge(_HtmlProbe(html)):
            log("已用真浏览器渲染页面，直接交给解析层。")
            return None, html, "L3:browser-html"

    # ---- L4：本机 Chrome/Edge 无头渲染（打包成 EXE 后唯一还能用的一层）----
    html = render_with_system_browser(url, on_log=on_log)
    if html and not looks_like_challenge(_HtmlProbe(html)):
        log("已用本机浏览器渲染页面，直接交给解析层。")
        return None, html, "L4:system-browser"

    return None, None, "failed:" + "/".join(tried or ["（未尝试）"])


def _classify_after_recovery(response: Any) -> bool:
    """升级之后再看一眼：还是校验页吗？（core 里的分类就靠这个）"""
    return looks_like_challenge(response)


class _HtmlProbe:
    """把一段 HTML 包成"有 .headers/.text"的形状，好复用同一套判据。"""

    __slots__ = ("headers", "text")

    def __init__(self, html: str):
        self.headers: dict = {}
        self.text = html or ""


def recover_stream(
    session: requests.Session,
    url: str,
    *,
    kwargs: Optional[dict] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> tuple[Optional[Any], str]:
    """下载阶段（音频二进制）被拦住时的升级：只做 L1/L2，返回 `(response, tier)`。

    音频主机（`lambda.vgmtreasurechest.com`）当前没有被校验，这条路径是保险。
    这里**不带 stream** 返回的响应就是普通响应，调用方照常 `iter_content` 读。
    """
    def log(text: str) -> None:
        if on_log is not None:
            try:
                on_log(text)
            except Exception:  # noqa: BLE001
                pass

    if not _is_remote_https(url):
        return None, "skipped:not-https"

    call_kwargs = dict(kwargs or {})
    if CURL_CFFI_AVAILABLE:
        for candidate in _escalate_targets(session):
            try:
                response = session.raw_impersonated_request(  # type: ignore[attr-defined]
                    "GET", url, impersonate=candidate,
                    headers=call_kwargs.get("headers"),
                    timeout=call_kwargs.get("timeout", (10, 30)),
                    stream=bool(call_kwargs.get("stream", False)),
                )
            except Exception as exc:  # noqa: BLE001
                log(f"下载链路指纹 {candidate} 失败：{type(exc).__name__}")
                continue
            if not looks_like_challenge(response, body_available=False):
                log(f"下载链路已切换浏览器指纹（{candidate}）。")
                return response, f"L1:impersonate={candidate}"

    if not browser_fallback_enabled():
        return None, "L1-failed"

    bridge = BrowserBridge()
    harvested = bridge.harvest(url, on_log=on_log)
    if not harvested:
        return None, "L2-unavailable"
    cookies = harvested.get("cookies") or {}
    user_agent = harvested.get("user_agent") or ""
    if not CURL_CFFI_AVAILABLE or not cookies:
        return None, "L2-failed"
    extra = {"User-Agent": user_agent} if user_agent else None
    for candidate in _escalate_targets(session):
        try:
            response = session.raw_impersonated_request(  # type: ignore[attr-defined]
                "GET", url, impersonate=candidate, headers=extra, cookies=cookies,
                timeout=call_kwargs.get("timeout", (10, 30)),
                stream=bool(call_kwargs.get("stream", False)),
            )
        except Exception as exc:  # noqa: BLE001
            log(f"下载链路带浏览器会话重试失败：{type(exc).__name__}")
            continue
        if not looks_like_challenge(response, body_available=False):
            log("下载链路已用浏览器会话（cf_clearance）通过校验。")
            return response, f"L2:browser-cookie+{candidate}"
    return None, "failed"


def iter_cookie_names(values: Iterable[str]) -> list[str]:
    """诊断用：只列 cookie 名字，不打印值。"""
    return sorted({str(name) for name in values})
