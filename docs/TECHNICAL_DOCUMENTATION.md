# TECHNICAL_DOCUMENTATION.md — KHDownloader 技术文档

PROJECT_ID: KHDownloader
最近更新：v006（打包 / 分发轮）

> 本文件只写**已经实现、或者已经实际观察验证过**的东西。
> 凡是"计划做但还没做"或"推测但没验证"的，都会明确标注。

---

## 0. 发布状态（Release Status）—— v005 审计结论

| 项目 | 结论 |
| --- | --- |
| **发布就绪结论** | **Release Ready with known limitations（发布就绪，带已知限制）** |
| 验证平台 | Windows 10/11 + 系统 Python |
| 验证 Python 版本 | **3.11.9**（`tkinter` 8.6）。文档要求 3.11+ |
| 当前测试总数 | **179 项**（v1.0.0：新增 `TestFingerprintTransport` / `TestTransportResponseAdapter` / `TestEscalationLadder` / `TestSystemBrowserFallback`），全绿 |
| 编译 / 导入检查 | `py_compile` 全部通过；核心模块在**屏蔽 tkinter** 时仍可导入 |
| 第三方依赖 | 必需 `requests`、`beautifulsoup4`、**`curl_cffi`（v1.0.0 新增，提供浏览器网络指纹）**；可选 `playwright`（源码版第 3 级用；EXE 不含） |
| 真实站点验证状态 | **v1.0.0 起本网络出口可正常读取专辑页**：`tools/verify_cloudflare_bypass.py` 三组对照 14/14、真站 30 首全部解析、真实 MP3 直链下载 4,930,262 字节；解析能力仍由**真实页面回归夹具**锁定 |
| 安全底线 | **不做**：破解/代答人机校验、打码接口、伪造 `cf_clearance`、复用他人会话、绕过登录或限流。v1.0.0 的传输层是"用真浏览器一致的网络指纹读公开页面"，三级/四级升级开**全新临时浏览器会话**，不读用户浏览器配置（详见 §13） |
| 启动方式 | `python -m pip install -r requirements.txt` → `python khdownloader_gui.py` |

**已知限制（均为非阻塞，已获 Strategy 认可）**：仅 MP3 / 无并发 / 无断点续传 /
无失败重试 / 站点是否下发校验仍随出口浮动（v1.0.0 已能在被挑战时自动升级）/
Windows 专用 `os.startfile` / 取消为协作式（非硬中断）。

> 依据：`stage-handoff/reports/KHDownloader_STAGE_EXECUTION_REPORT_v005.md`（审计轮）。

---

## 0.1 打包 / 分发状态（Packaging Status）—— v006 结论

| 项目 | 结论 |
| --- | --- |
| **打包结论** | **Packaged Release Ready（已打包、可分发）** |
| 打包工具 | **PyInstaller 6.22.3** |
| 打包形态 | **单文件（onefile）+ 无控制台（windowed）** Windows GUI |
| 产物 | `dist\KHDownloader.exe` → 分发件 `release\KHDownloader\KHDownloader.exe` |
| 产物体积 | **21,809,102 字节**（约 20.8 MiB） |
| 产物指纹 | SHA-256 `dbfa426febbf6f0a1ee13b12cfb3e826316db601c413765e58b8443716c2db16` |
| 运行前提 | **无需 Python、无需安装任何库**（tkinter / Tcl-Tk 8.6、requests 2.34.2、bs4 4.15.0 均已打入） |
| 冻结运行时自检 | **10/10 全通过**（tkinter/Tcl 版本、`Tk()` 初始化、依赖 import、核心解析、GUI 控件实例化） |
| 干净目录验证 | **通过**：仅复制 EXE 到项目树之外的系统临时目录，可正常启动 |
| 无 Python 验证 | **通过**：PATH 收窄为 `System32 + Windows`（无任何 Python）后，自检仍 10/10，GUI 仍能启动 |
| 冻结下载 / 取消验证 | **通过**：冻结态下真实下载本地假站点文件（文件落盘、无 `.part` 残留）；取消抛 `CancelledError` 且不留半成品 |
| 源码测试 | **95/95** 全绿（打包前后各复现一次） |
| 打包环境 | Windows 10 10.0.19045 / AMD64 / Python 3.11.9（64 位） |

**已知限制（非阻塞）**：EXE 未做数字签名 → 首次运行可能触发 SmartScreen 提示（属未签名程序常规提示）；
单文件 EXE 每次启动会解压到 `%TEMP%\_MEIxxxxxx`（正常机制）；仅 64 位 Windows 可运行。

> 依据：`stage-handoff/reports/KHDownloader_STAGE_EXECUTION_REPORT_v006.md`（打包轮）。

---

## 0.2 真实使用可用性状态（Usability Status）—— v007 结论

| 项目 | 结论 |
| --- | --- |
| **本轮裁决** | 见 `stage-handoff/reports/KHDownloader_STAGE_EXECUTION_REPORT_v007.md` |
| 用户遇到的阻塞 | 输入真实专辑 URL 后收到 **HTTP 403 + `cf-mitigated: challenge`**（Cloudflare 人机校验），无法继续 |
| 阻塞位置 | **只在 `downloads.khinsider.com` 的 HTML 层**（专辑页/详情页）；**MP3 主机未被拦**（实测 200 + `audio/mpeg`） |
| 本轮新增能力 | ① **导入专辑页面（HTML）** 入口——恢复"曲目发现"；② **下载单个链接** 入口——恢复"单个 MP3 下载" |
| 错误分类增强 | 校验页 / 普通 403（`AccessDeniedError`）/ 429（`RateLimitError`）/ 超时 / 解析失败 **分别报出** |
| 文件安全增强 | 下载入口新增 **Content-Type 守卫**：拒收把 HTML/JSON 当 MP3 保存（`NotAudioContentError`） |

> 两个恢复入口**互不冒充**："导入专辑页面"解决曲目发现，"下载单个链接"只处理**一个** MP3 链接。
> 二者都复用既有下载引擎（`.part` → 原子改名、取消、跳过、文件名净化）。

---

## 1. 项目结构

```text
khdownloader/
├─ khdownloader_core.py        核心逻辑：HTTP 请求 + 页面解析 + 文件下载（不含界面）
├─ khdownloader_gui.py         图形界面：tkinter 窗口 + 后台线程 + 消息队列
├─ requirements.txt            第三方依赖清单
├─ README.md                   新手用户指南（怎么装、怎么用、常见问题）
├─ REQUIREMENTS.md             需求说明（用户想要什么）
├─ TECHNICAL_DOCUMENTATION.md  本文件（实际怎么实现的）
├─ EXECUTION_LOG.md            执行过程与证据记录
├─ tests/
│  ├─ mock_site.py             本地假站点（测试用，不联网）
│  ├─ test_core.py             核心逻辑测试（77 项）
│  ├─ test_gui_smoke.py        界面冒烟测试（18 项）
│  └─ fixtures/
│     ├─ *.html（合成）         模仿站点结构的合成页面，带各种干扰链接
│     ├─ slow_album_page.html  合成："慢速专辑"，MP3 一小块一小块发（测中途取消）
│     ├─ no_length_album_page.html 合成：MP3 不声明 Content-Length（测"不编造百分比"）
│     ├─ real_album_page.html  **真实专辑页**夹具（30 首，脱敏后，锁住真实结构）
│     └─ real_song_page.html   **真实详情页**夹具（脱敏后，锁住真实结构）
├─ tools/
│  ├─ inspect_site.py          站点结构排查工具（存 HTML + 打印摘要）
│  ├─ verify_real_album.py     真实站点端到端验证：专辑页->30 首->下载第 1 首（手动跑）
│  ├─ verify_real_download.py  真实站点单曲下载验证（手动跑）
│  └─ evidence/                排查过程中存下来的真实页面（作为证据）
├─ build_exe.bat               一键打包脚本（v006，Windows）
├─ KHDownloader.spec           PyInstaller 构建描述（v006，固定打包配置）
├─ build/                      PyInstaller 中间缓存（v006，可丢弃）
├─ dist/                       PyInstaller 产物：KHDownloader.exe（v006）
├─ release/KHDownloader/       分发给最终用户的目录（v006）：EXE + 使用说明
└─ stage-handoff/              阶段交接：briefs/ 与 reports/
```

### 为什么把核心逻辑和界面分开

`khdownloader_core.py` **完全不 import tkinter**。好处有三个：

1. 能单独测试，不用起图形界面；
2. 新手可以先把核心逻辑跑通、看懂，再去看界面部分；
3. 以后想做命令行版本或其他界面，核心逻辑可以直接复用。

---

## 2. 依赖

| 库 | 用途 | 备注 |
| --- | --- | --- |
| `requests` | 发 HTTP 请求、流式下载 | 非标准库，需安装 |
| `beautifulsoup4` | 解析 HTML | 非标准库，需安装 |
| `tkinter` | 图形界面 | **Python 自带**，不用安装 |
| `unittest` | 测试 | **Python 自带** |

HTML 解析用的是 BeautifulSoup **自带的 `html.parser`**，故意没装 `lxml`，
目的就是少一个依赖。实测环境 Python 3.11.9 / Windows，tkinter 8.6。

---

## 3. 站点流程（已实现的部分）

大流程：

```text
专辑页  ->  曲目详情页  ->  真正的 MP3 文件
```

### 3.1 已实测确认的事实

> 说明：v002 纠正轮**拿到了真实曲目详情页的原始 HTML**（本机直连，HTTP 200），
> 因此下面凡是与"曲目页 / MP3 地址"有关的条目都已经从"假设"升级为"已确认"。
> 只有"专辑页"仍然拿不到原始 HTML，见 §3.2。

#### 曲目详情页与 MP3 地址（v002 已确认）

| 结论 | 状态 | 证据 |
| --- | --- | --- |
| 以 `.mp3` 结尾的曲目链接**返回的是 HTML 页面**，不是音频 | **已确认** | 本机直连返回 `HTTP 200` + `Content-Type: text/html; charset=UTF-8`，14,346 字节；页面里有 `Album name` / `Song name` / `Go back to the album` |
| 真正的 MP3 链接**只有一个**，且页面同时还有一个写法几乎一样的 FLAC 链接 | **已确认** | 真实 HTML：`<a href="....mp3">…Click here to download as MP3…</a>` 与 `<a href="....flac">…Click here to download as FLAC…</a>` 并存 |
| MP3 链接文字含稳定短语 **`Click here to download as MP3`** | **已确认** | 同上；`<i class="material-icons">get_app</i>` 后面紧跟该文字 |
| MP3 链接内部有 **`<span class="songDownloadLink">`** | **已确认** | 同上 |
| 页面还有播放器 **`<audio id="audio" src="同一个 MP3 地址">`** | **已确认** | 真实 HTML 中 `<audio controls="" id="audio" preload="auto" src="…">` |
| MP3 地址是**绝对地址** | **已确认** | `https://lambda.vgmtreasurechest.com/...` |
| MP3 地址是**跨主机**的（页面在 `downloads.khinsider.com`，文件在 `lambda.vgmtreasurechest.com`） | **已确认** | 同上 |
| MP3 文件本身可达：`HTTP 200` / `audio/mpeg` / **3,722,211 字节** / **无跳转** | **已确认** | HEAD 请求实测；`Accept-Ranges: bytes` |
| **真实端到端下载成功**（分块流写、`.part`、原子改名、字节数与 `Content-Length` 一致、无 `.part` 残留、文件头为 `ID3`） | **已确认** | `python tools/verify_real_download.py`，10/10 核对项通过 |
| 解析规则可推广到同一专辑的其它曲目 | **已确认** | 第 27 首 `27%2520CROSSING.mp3` 同样 HTTP 200，解析出 `.../27%20CROSSING.mp3`（同一文件主机） |
| 曲目文件名里的空格是**双重编码**的 `%2520`，而文件主机上是单层 `%20` | **已确认** | 详情页 URL `02%2520Crisscross.mp3` ↔ 文件 URL `02%20Crisscross.mp3` |
| 路径不存在时返回 **404**，会被正确识别为"普通 HTTP 错误"而不是"人机校验" | **已确认** | 请求一个不存在的曲目名返回 `HTTP 404`，程序给出可读提示 |

#### 专辑页（v003 已全部确认为实测事实）

| 结论 | 状态 | 证据 |
| --- | --- | --- |
| 专辑页容器是 **`<table id="songlist">`** | **已确认** | 真实 HTML：`<table id="songlist">`，内含 32 个 `<tr>` |
| 表头是 `<tr id="songlist_header">`，表尾是 `<tr id="songlist_footer">` | **已确认** | 表头列名依次为 `#` / `Song Name` / `MP3` / `FLAC` |
| 曲目行共 **30** 行，每行 8 个 `<td>` | **已确认** | 播放按钮 / 序号 / **曲名** / 时长 / MP3 大小 / FLAC 大小 / 下载图标 / 加入播放列表 |
| **曲名在 `<td class="clickable-row">` 里的 `<a>` 文字中** | **已确认** | 例：`<a href=".../01%2520....mp3">群青のティファレス(GAME Size)</a>` |
| 同一行的**曲名 / 时长 / MP3 大小 / FLAC 大小 / 下载图标** 5 个链接指向**同一个地址** | **已确认** | 直接取"第一个 .mp3 链接"会拿到，但会把 `2:27`、`4.70 MB` 当曲名，必须按文字形态排除 |
| 专辑页的曲目链接是**根相对地址**（以 `/` 开头） | **已确认** | 实测 `href="/game-soundtracks/album/<slug>/01%2520....mp3"` |
| 曲目文件名是**双重编码** `%2520` | **已确认** | 实测链接形如 `01%2520%25E7%25BE%25A4...mp3`，二次解码后为日文曲名 |
| 曲名含日文等非 ASCII 字符 | **已确认** | 第 1、28、29、30 首均为日文曲名，解码后正常显示 |
| 专辑页里也有一句 "download as MP3" 字样的链接（下载图标列） | **已确认** | 这是**陷阱**：若把专辑页误当详情页，会返回一个"像 MP3 实为 HTML"的地址 |
| **曲目详情页路径本机可直连（200）** | **已确认** | 见上表 |
| **专辑页路径本机被拦截（403 + `cf-mitigated: challenge`）** | **已确认** | 无代理出口下实测；开启代理出口后专辑页恢复 200（v003） |

### 3.2 尚未验证的部分

**已全部清零。** v003 在开启代理出口后成功取回真实专辑页 HTML（HTTP 200 / 83,062 字节），
专辑页与曲目页两块结构现均已在真实页面上验证过，代码里不再存在"未验证假设"。

真实页面已固化为回归夹具：

- `tests/fixtures/real_album_page.html` —— 真实专辑页（30 首，含日文曲名与全部干扰列）
- `tests/fixtures/real_song_page.html` —— 真实曲目详情页

原始证据（未脱敏）保留在 `tools/evidence/REAL_album_page.html`。

> 说明：v001 与 v002 曾报告专辑页被 Cloudflare 拦截、因此专辑页结构属于假设。
> v003 在用户更换网络出口后重新直连成功，该假设已被实测取代。详见 `EXECUTION_LOG.md`。


---

## 4. 解析规则（实际实现的）

### 4.1 专辑页 → 曲目列表

`parse_album_page(html, album_url)`

1. **限定范围**：优先只在 `table#songlist` 里找链接；找不到这张表时退化为整页查找，
   但仍然带上第 2 步的前缀过滤，所以不会失控。
2. **限定条件**（两个都要满足）：
   - 链接路径以「本专辑路径 + `/`」开头；
   - 链接路径以 `.mp3` 结尾（站点上这种链接是详情页）。
3. **排除数字型标签**：真实专辑页同一行里，**曲名 / 时长 / MP3 大小 / FLAC 大小 /
   下载图标** 5 个链接指向**同一地址**。如果只取第一条，曲名会变成 `2:27` 或 `4.70 MB`。
   所以凡链接文字匹配 `^[\d\s.,:：-]+(MB|KB|GB)?$`（纯数字/时长/文件大小）的一律跳过。
   排版上曲名列排在时长列**之前**，跳过数字标签后，第一个剩下的就是曲名。
4. **顺序 + 去重**：按页面出现顺序收集，同一个地址只保留第一次出现的那个。
5. **曲名**：优先取链接文字；没有文字时退回用 URL 里的文件名（解码后）。

这套规则为什么"收得住"：站点是相对路径结构，同一个专辑下的曲目页都以专辑自己的
路径开头，别的专辑（slug 不同）、字母导航、论坛、变更日志都命不中。而"排除数字标签"
这条不依赖任何 class 名 —— 即使站点改了 class，只要"曲名列是唯一带真实文字的那一列"
这个语义不变，结果就还是对的。


### 4.2 曲目页 → MP3 地址

`parse_song_page(html, page_url)`

按"从具体到宽泛"依次尝试四条规则。**前三条都是在真实页面上实测确认过的**
（原始证据：`tools/evidence/real_song_page_Crisscross.html`；
回归夹具：`tests/fixtures/real_song_page.html`）。

| 顺序 | 规则 | 依据 | 为什么需要 `.mp3` 结尾这个附加条件 |
| --- | --- | --- | --- |
| A | 链接文字含 `download as MP3`（不区分大小写） | 真实页面的链接文字就是 `Click here to download as MP3` | 页面上还有一条 `Click here to download as FLAC`，不加限定就会选错格式 |
| B | 链接内部含 `class="songDownloadLink"` | 真实页面下载链接内部就是 `<span class="songDownloadLink">` | 同理，FLAC 链接也带这个 class |
| C | 页面播放器 `<audio src>` / `<source src>` | 真实页面有 `<audio id="audio" src="同一个地址">` | 播放器也可能播别的格式 |
| D | v001 的打分启发式（**仅兜底**） | 不依赖任何 class 名，用来兜住页面改版 | — |

规则 D 的打分（页面改版、A/B/C 全落空时才用）：

| 得分 | 判据 | 为什么这么定 |
| --- | --- | --- |
| +2 | 候选链接的文件名（解开两层百分号编码后）与当前曲目页的文件名一致 | 同一个文件换了个主机存放，文件名通常不变 |
| +1 | 候选链接在**别的主机名**上 | 真正放文件的主机通常不是页面本身的主机 |

同分时按页面出现顺序取第一个。

四条都不中时抛 `NoMp3LinkError`（可读错误），不会静默产生空文件。

> **v001 的启发式打分现在只是兜底**。真实 HTML 到手后，主路径换成了有明确语义的
> 规则 A/B/C —— 这样即使站点改版，也能从报错里一眼看出"是哪条规则没命中"。

---

## 4.3 界面（GUI）—— v004 重做

### 4.3.1 布局

界面刻意保持简单，从上到下依次是：

```text
KHDownloader —— KHInsider MP3 下载器

专辑地址：  [.............................................]
保存到：    [....................................] [选择文件夹…]
            [开始下载] [取消] [打开保存文件夹] [清空日志]

专辑进度
[======================--------]        曲目 12 / 30
当前：Crisscross

当前文件
[==================------------]        1.20 / 3.54 MB（34%）

状态 / 日志：
+---------------------------------------------+
| 找到 30 首曲目。                              |
| [12/30] Crisscross                           |
|   正在解析曲目页…                             |
|   正在下载…                                   |
+---------------------------------------------+

完成 11 | 跳过 0 | 失败 0 | 取消 0
就绪。
```

- **专辑地址**：单行输入框，粘贴友好；使用前 `normalize_album_url()` 会 `strip()` 掉首尾空白；
- **保存目录**：可读路径框 + `选择文件夹…` 按钮；选过的目录在本次会话里一直保留
  （`self.last_folder`），下次点开文件夹选择框会从它开始；
- **两个按钮**：`开始下载`（运行中禁用）与 `取消`（空闲时禁用）；另外保留
  `打开保存文件夹`、`清空日志` 两个辅助按钮；
- **两条进度条**：专辑级（determinate，`current / total` + 当前曲名）与当前文件级
  （有 `Content-Length` 时 determinate 百分比；没有时切 indeterminate，见 §4.3.4）；
- **日志区**：可滚动，逐条记录"解析专辑 / 解析曲目页 / 正在下载 / 跳过 / 完成 /
  失败 / 取消"；
- **完成汇总**：一行 `完成 X | 跳过 X | 失败 X | 取消 X`（见 §4.3.5）。

### 4.3.2 线程模型（不卡死、不跨线程碰控件）

```text
界面线程（主线程）                     工作线程（后台）
     │                                      │
     │  点"开始下载" ─ 校验输入 ─ 起线程 ──────▶│
     │                                      │  download_album(...)
     │                                      │  （只往 queue 塞消息，绝不碰控件）
     │◀──── queue.Queue 里塞 (kind, data) ────│
     │                                      │
     │  root.after(50ms) 轮询队列             │
     │  ─ 取到消息 → _handle_event() 更新控件 ─▶ 界面刷新
```

规则：

- **工作线程绝不直接改 tkinter 控件**。它唯一的输出是 `queue.Queue`；
  测试 `test_worker_thread_does_not_touch_widgets` 会记录所有调用界面更新函数的
  线程 id，断言只有主线程出现过；
- 界面线程用 `root.after(50, self._pump_events)` 每 50 ms 取一次队列，
  取完队列、若仍在运行则再排下一次；
- 这让界面在整个下载过程中一直在响应事件（测试用一个"心跳"计时器统计
  事件循环有没有停）。

### 4.3.3 取消机制（协作式，核心层不依赖界面）

取消**不是硬杀线程**，而是"在安全点主动停下"：

1. 界面上 `取消` 按钮 → `self.cancel_requested.set()`（`threading.Event`）；
2. 工作线程把 **`self.cancel_requested.is_set` 这个函数**（注意：传函数，不是传 Event）
   作为 `should_cancel` 参数交给 `download_album()`；
3. 核心层在两个安全点问它"要不要停"：
   - **每首曲目开始之前** —— 取消了就不再开新的一首；
   - **每个数据块落盘之前** —— 下载到一半也能停下来（见 §4.4）；
4. 取消一旦发生：
   - 当前曲目记为 `cancelled`，抛出的 `CancelledError` 被 `download_album()` 单独接住，
     **不记为失败**；
   - 还没轮到的曲目统一补记为 `cancelled`（"未处理（已取消）"），让汇总总数对得上；
   - 未下完的 `.part` 被删掉；**已经下好的 `.mp3` 原封不动**；
   - 依次发出 `cancelled` 事件与 `done` 事件。

`CancelledError` 继承自 `DownloaderError`，所以"忘了单独处理取消"的地方也不会崩，
但界面会把它和真正的失败区分开 —— 用户自己按的取消不是出错。

### 4.3.4 进度事件模型

| 事件 | 何时发出 | 数据 | 界面怎么处理 |
| --- | --- | --- | --- |
| `log` | 每一步 | `{"text": str}` | 追加到日志区 |
| `tracks` | 曲目解析完成 | `{"total": int}` | 设置专辑进度条最大值与 `0 / N` |
| `track_start` | 开始处理某首 | `{"index","total","name"}` | 更新 `n / N` 与"当前：曲名"；文件进度归零 |
| `phase` | 每首进入解析 / 下载阶段 | `{"index","total","phase"}`，phase = `resolving` / `downloading` | 状态栏切换"正在解析曲目页…"/"正在下载…" |
| `file_progress` | 每个数据块 | `{"written": int, "total": int}` | 见下方"关键" |
| `track_done` | 某首结束 | `{"index","total","name"}` | 推进专辑进度条 |
| `track_fail` | 某首失败 | `{"index","total","name","error"}` | 日志记一行失败原因，继续下一首 |
| `cancelled` | 用户取消时 | `{"index","total"}` | 状态栏"已取消"，日志记一行 |
| `done` | 全部结束 | `{"counts": dict, "results": list}` | 出汇总（见 §4.3.5） |

**关键（进度诚实性）**：`file_progress` 的 `total` 为 **0** 表示"对方没给
`Content-Length`"。这时界面**绝不编造百分比**，而是把当前文件进度条切成
indeterminate（来回滚动），并显示"已下载 X MB（对方未提供文件大小）"。
只有 `total` 不为 0 时，才算真实百分比 `written * 100 // total`。

### 4.3.5 结果与汇总模型

每首曲目处理完得到一个 `TrackResult(name, status, message, path)`，
`status ∈ {completed, skipped, failed, cancelled}`：

| status | 含义 | `.ok` |
| --- | --- | --- |
| `completed` | 真的下载完成了 | ✅ |
| `skipped` | 已存在且大小一致，没重复下 | ✅ |
| `failed` | 出错了（网络 / HTTP / 解析 / 写盘） | ❌ |
| `cancelled` | 用户取消，这一首没做完 | ❌ |

`summarize_results(results)` 把它数成 `{completed, skipped, failed, cancelled}`
四个计数，`download_album()` 通过 `done` 事件把 `counts` 和完整 `results` 一起交给界面。

界面出总结时**不会在有失败/取消时还说"全部完成"**：
- 有 `failed` → 弹 `showwarning`（"下载结束（有失败）"）；
- 无失败但有 `cancelled` → 弹 `showinfo`（"已取消"，并说明已下好的文件都保留着）；
- 只有全部干净（全是 `completed`/`skipped`）才弹 `showinfo`（"下载完成"）。

---

## 5. 线程与事件模型（核心层视角）

---

## 6. 下载与文件名行为

### 流式写盘 + 原子改名

```text
打开 "曲名.mp3.part"  →  分块下载写入  →  字节数校验  →  os.replace 改名为 "曲名.mp3"
       ↑                                        ↓ 校验不过 / 中途出错 / 用户取消
   每块落盘前问一次"要不要取消"                删掉 .part，报错或抛 CancelledError
```

- 每次从网络读 64 KB，不会把整首曲子一次性读进内存；
- 只有**写完并且字节数与 `Content-Length` 一致**时才会改名成 `.mp3`；
- 任何失败（含用户取消）都会先删掉 `.part` —— 硬盘上不会出现"半成品顶着 .mp3 名字"的情况；
- 服务器没给 `Content-Length` 时跳过字节数校验（仍然保留 `os.replace` 这一步）；
- `on_progress(written, total)` 每写一块就回调一次，`total` 为 0 表示对方没给长度。

### 重名规则（确定性）

| 情况 | 行为 |
| --- | --- |
| 目标文件不存在 | 直接用 `曲名.mp3` |
| 已存在，且大小和远端 `Content-Length` 相同 | **跳过下载**，记为"已存在且大小一致，跳过" |
| 已存在，但大小不同 | 依次试 `曲名 (2).mp3`、`曲名 (3).mp3`……直到空位 |

任何时候都**不会覆盖**已有文件。

### 文件名净化

`sanitize_filename()` 处理：

1. `<>:"/\|?*` 和控制字符 → 替换成 `_`；
2. 去掉首尾空白与结尾的点（Windows 会吃掉结尾的点，导致文件"找不到"）；
3. `CON`/`PRN`/`AUX`/`NUL`/`COM1..9`/`LPT1..9` 这些设备名前面加 `_`；
4. 超过 120 字符截断（Windows 整条路径上限 260 字符）；
5. 先做一层 `unquote`，把 `%E5%8C%97` 这类编码还原成中文。

---

## 7. 已知限制

### 7.1 站点拦截（v1.0.0 起：能自动升级，不再是阻塞项）

**站点按请求的客户端指纹下发 Cloudflare 人机校验**：不是按路径永久封锁，
也不是整站封锁，更不是"限流"。

| 路径 | 直连结果（v001–v1.0.0 累计观察） |
| --- | --- |
| `/game-soundtracks/album/<slug>`（**专辑页**） | 裸 `requests` 命中 **`403` + `cf-mitigated: challenge`**；同一次会话换指纹后 **`200`**（v1.0.0 实测 30/30 次） |
| `/game-soundtracks/album/<slug>/<曲目>.mp3`（**曲目详情页**） | **始终 `200` + `text/html`**（站点按路径风险分级发放校验） |
| 站点首页 `/` | 也按出口浮动：v003 换出口后首页反而 `403`，专辑页却 `200` |
| `/search`、`/robots.txt` | `200` 正常 |

**根因（v1.0.0 实测定的）**：判定依据是 **TLS 握手 / HTTP2 帧层指纹** ——
密码套件顺序、扩展集合与顺序、曲线偏好、ALPN、伪头顺序、SETTINGS 帧，由**客户端库**决定。
改 User-Agent 无法改变它。排除项（每一条都实测过）：

| 假设 | 实测 |
| --- | --- |
| 缺请求头 | 补齐 UA/Accept/Sec-Fetch-*/sec-ch-ua 等完整浏览器头 → **仍 403** |
| 缺 Cookie 或 Referer | 首页 → 搜索 → 专辑页（带 Referer，站点**未下发任何 Cookie**）→ **仍 403** |
| 频率触发限流 | 每 2 秒重试 3 次 → **仍 403**（**不是 429**） |
| 系统代理污染 | `session.trust_env = False` 直连 → **仍 403** |

**处理**：传输层四级升级阶梯（见 §13）。被拦且所有层都失败时，`fetch_html()` 仍抛出可读的
`SiteBlockedError`，提示分三层（换指纹 / 真浏览器 / 本机浏览器）并列出已试指纹，
且明确区分"页面没拿到（被拦）"和"页面拿到了但解析不出曲目"。

**边界**：不破解或代答校验题、不调打码接口、不伪造 `cf_clearance`、不复用他人会话、
不绕过登录与限流；需要浏览器时开全新临时会话与一次性临时配置目录，不读用户浏览器数据。

### 7.2 其它限制

- ~~专辑页的解析规则未在真实页面上验证过~~ —— **v003 已解决**：真实专辑页 HTML 已取回，
  解析规则已在真实页面上验证，并固化为回归夹具 `tests/fixtures/real_album_page.html`；
- 只支持单张专辑、顺序下载；
- 只支持 MP3（页面上的 FLAC 链接会被正确排除，但不会去下 FLAC）；
- **没有暂停/续传**：取消后再开始，会从头重新下载未完成的那一首（已下好的那几首
  因"大小一致"会被跳过）；不做 range 请求；
- 没有失败重试（单首失败只记一行日志，继续下一首）；
- 取消是**协作式**的：检查点在"两块之间 / 两首之间"，所以点取消后要等当前
  数据块（最多 64 KB）落盘才会真正停下 —— 不会损坏文件，也不是立刻停；
- `"打开保存文件夹"` 按钮用了 Windows 专用的 `os.startfile`，换系统需要改这里；
- 进度条百分比按 `written * 100 // total` 取整，不显示小数（对新手够用）。

### 7.3 真实站点端到端验证（v002 已完成）

`python tools/verify_real_download.py` 的实测输出（2026-10-01）：

| 核对项 | 结果 |
| --- | --- |
| 曲目详情页 HTTP 状态 | `200`，`Content-Type: text/html; charset=UTF-8`（**证明 .mp3 结尾的链接是 HTML 页**） |
| 解析出的 MP3 地址 | `https://lambda.vgmtreasurechest.com/soundtracks/<专辑 slug>/<随机串>/02%20Crisscross.mp3`（绝对、跨主机） |
| MP3 的 HTTP 状态 / 类型 / 长度 | `200` / `audio/mpeg` / `3,722,211` 字节 |
| 是否发生跳转 | 0 次 |
| 写入字节数 / 文件实际大小 | `3,722,211` / `3,722,211` |
| 与 `Content-Length` 一致 | 是 |
| `.part` 残留 | 无 |
| 最终 `.mp3` 就位 | 是 |
| 文件头 | `b'ID3\x04'`（确实是音频，不是错误页） |

跑完临时目录自动删除。交叉验证：同一专辑第 27 首同样解析正确。

### 7.4 真实站点全专辑端到端验证（v003 已完成）

`python tools/verify_real_album.py` 的实测输出（2026-10-07）：

| 阶段 | 核对项 | 结果 |
| --- | --- | --- |
| 专辑页 | 解析出曲目数 / 与站点一致 | 30 / 30 |
| 专辑页 | 第 1、2 首曲名 | `群青のティファレス(GAME Size)` / `Crisscross` |
| 专辑页 | 地址去重 / 无数字混入曲名 | 通过 |
| 详情页 | 逐首解析出 MP3 地址 | **30/30** |
| 详情页 | 地址全部跨主机（不是详情页当作音频） | 通过 |
| 下载 | MP3 主机状态 / 类型 / 长度 | `200` / `audio/mpeg` / `4,930,262` 字节 |
| 下载 | 落盘大小 / 写入字节 / 与 `Content-Length` 一致 | `4,930,262` / `4,930,262` / 一致 |
| 下载 | `.part` 残留 / 文件头 / 文件名 | 无 / `b'ID3'` / `群青のティファレス(GAME Size).mp3` |

**14/14 通过。** 这是"专辑页 → 全部曲目 → 真实 MP3 地址 → 真实下载"的完整链路实测。

---

## 8. 怎么运行

### 安装依赖

```bat
pip install -r requirements.txt
```

### 启动界面

```bat
python khdownloader_gui.py
```

用法：粘贴专辑地址 → 选保存文件夹 → 点"开始下载" → 看进度条和日志。

### 跑测试

```bat
cd source
python -m unittest discover -s tests -p "test_*.py"   :: 全量 179 项，不需要联网
```

（v1.0.0 起推荐用 `unittest discover` 一次跑完；单文件也可用
`python tests/test_core.py`、`python tests/test_gui_smoke.py`。）

测试都跑在 `tests/mock_site.py` 这个本地假站点上，**不访问真实网站**，
所以没网、或者站点拒绝访问时照样能跑。
`TestRealSiteEvidence` 与 `TestRealAlbumPageEvidence` 这两组用的是**真实页面**夹具
（`tests/fixtures/real_song_page.html`、`tests/fixtures/real_album_page.html`），
但同样是离线读取，不发网络请求。**这两组是防改版的关键**：站点哪天改结构，它们会先炸。

v1.0.0 新增的传输层测试组（`tests/mock_site.py` 的 `flaky` 端点专门造"先挑战后放行"）：

| 测试组 | 覆盖 |
| --- | --- |
| `TestFingerprintTransport` | 会话仍是 `requests.Session`；http / 本地地址**不走**指纹层；`describe()` 字段齐全；`KHD_SYSTEM_BROWSER=0` 能关掉四级；transport 与 core 的校验判据一致 |
| `TestTransportResponseAdapter` | `with response:` / `encoding` 可写 / `apparent_encoding` / 非流式下 `iter_content` 仍可迭代 / `stream=True` 真流式 / `.request.url` |
| `TestEscalationLadder` | 本地 flaky 端点：裸请求被挑战 → 换指纹救回；全部失败时 tier 以 `L1-failed:` 开头并列出试过的指纹 |
| `TestSystemBrowserFallback` | 本机 Chrome/Edge `--dump-dom` 能出内容；**校验页不会被当成内容**返回 |

v004 新增的取消 / 进度 / 汇总测试组：

| 测试组 | 覆盖 |
| --- | --- |
| `TestCancellationCore` | 首首之前取消、首与首之间取消、下载中途取消删 `.part`、已完成文件不受影响 |
| `TestProgressSemantics` | 进度字节数严格递增且末值等于文件大小、缺 `Content-Length` 时 `total == 0` |
| `TestSummaryModel` | 四类状态计数、`skipped` 算顺利但不算完成、部分失败不产生"全部成功" |
| `TestCancelledEventSequence` | `cancelled` 事件先于 `done`、`done` 带正确计数、`phase` 事件成对 |
| `TestBlockedStillDistinguishable` | 整条流程里 Cloudflare 拦截仍是 `SiteBlockedError`，不会退化成"没找到曲目" |
| `tests/test_gui_smoke.py` | 界面控件齐全、取消按钮状态、主线程更新、取消后无假 `.mp3`、进度递增、汇总计数 |

### 真实站点端到端验证（手动跑）

```bat
python tools/verify_real_album.py [专辑地址]     :: 专辑页 -> 30 首 -> 下载第 1 首（14 项核对）
python tools/verify_real_download.py [曲目地址]  :: 单曲下载核对（10 项核对）
```

两个脚本都会真的访问站点做核对，各自用临时目录、跑完自动清理，不在项目里留 `.mp3`。
`verify_real_album.py` 只真实下载第 1 首（其余仅解析地址），避免给站点造成不必要压力。
注意：能否跑通取决于**出口 IP 的信誉**（站点对程序化访问有 Cloudflare 人机校验）。

### 排查站点结构

```bat
python tools/inspect_site.py "<页面地址>"
```

会把页面 HTML 存到 `tools/evidence/`，并打印状态码、`Content-Type`、有无跳转、
链接形态统计、是否含 `#songlist`，以及试着按专辑页/曲目页各解析一次。

---

## 9. 计划中但**尚未实现**的功能

以下都还没写，列在这里避免误会：

- 断点续传（`.part` 文件目前失败即删；取消后重来会从头下未完成的那首）；
- 失败自动重试（单首失败只记一行日志，继续下一首）；
- FLAC 下载；
- 多专辑队列；
- 并发下载（当前严格顺序，一次一首）；
- 命令行版本（核心逻辑已经可以复用）；
- 暂停（只能在"取消"这一档粒度上停）；
- 代码签名（EXE 目前未签名，见 §10.4）。

> v004 起，v002/v003 遗留的"专辑页解析规则待校准"已不再存在：真实专辑页与曲目页
> 两块结构都已在真实页面上验证并固化为回归夹具（见 §3.1、§7.1、§7.4）。
> v006 起，"打包成 `.exe`"已经从本条移出 —— 已完成，见 §10。

---

## 10. 打包与分发（v006 新增）

把源码变成**一个双击即用的 EXE**，是这个版本新增的能力。它不改动任何运行逻辑 ——
只是把已经验证过的程序"装箱"。

### 10.1 打包方式

| 项目 | 值 |
| --- | --- |
| 工具 | **PyInstaller 6.22.3** |
| 形态 | **单文件（`--onefile`）+ 无控制台（`--windowed`）** |
| 入口 | `khdownloader_gui.py` |
| 名称 | `KHDownloader` |
| 产物 | `dist\KHDownloader.exe` |

**为什么选 PyInstaller**：它是 Windows 上最成熟、最不需要额外配置的打包器，
对 tkinter 的支持是内置的（自动收集 Tcl/Tk 数据文件），不需要手写 hook。
本版本没有换用 Nuitka / cx_Freeze / Briefcase —— 因为 PyInstaller 正常工作，
按任务书要求不无谓更换。

### 10.2 构建命令（可复现）

**统一走 spec**：

```bat
build_exe.bat
```

其内部等价于：

```bat
python -m PyInstaller --noconfirm --clean KHDownloader.spec
```

- 用 `python -m PyInstaller` 而不是裸 `pyinstaller`：保证用的是**当前这个 Python** 里的 PyInstaller，
  避免多版本环境下装错地方。
- `--noconfirm`：不交互确认；`--clean`：先清缓存，保证结果可复现。
- **onefile / windowed / 名称 / 图标 / datas 全部写在 `KHDownloader.spec` 里**，
  不再走命令行参数。原因：PyInstaller **不会**让命令行的 `--name` / `--icon` 覆盖 spec 里的字段，
  两者混用会出现"两个不同的最终程序名"。

### 10.2a 应用图标与资源路径（v008 §10.1 新增）

| 文件 | 规格 | 作用 |
|---|---|---|
| `assets/KHDownloader.ico` | 16/24/32/48/64/128/256 七种，全部 32bpp，91,809 B | spec 的 `icon=` → 编进 EXE 的 PE 资源表（资源管理器 / 快捷方式 / 任务栏读这一份）；运行时也读它设窗口图标 |
| `assets/KHDownloader.png` | 1254×1254，8bit RGBA，1,308,564 B | `apply_window_icon()` 的 PNG 兜底分支；后续维护用 |

spec 关键字段：

```python
datas=[
    (str(ASSETS / "KHDownloader.ico"), "assets"),
    (str(ASSETS / "KHDownloader.png"), "assets"),
],
...
name="KHDownloader",
icon=str(ASSETS / "KHDownloader.ico"),
```

**运行时资源定位**（`khdownloader_gui.py`）刻意**不使用 `os.getcwd()`** ——
双击 EXE 时 cwd 可能是任意目录，用 cwd 找资源是最容易在别人机器上失效的做法：

```python
def resource_roots() -> list[Path]:
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)          # onefile 冻结态
    if meipass:
        roots.append(Path(meipass))
    roots.append(Path(__file__).resolve().parent)    # 源码态
    ...

def apply_window_icon(root: tk.Tk) -> str:
    ico = resource_path(ICON_ICO_NAME)               # 优先 ICO
    if ico is not None:
        try:
            root.iconbitmap(default=str(ico))
            return f"ico:{ico}"
        except tk.TclError:
            pass
    png = resource_path(ICON_PNG_NAME)               # 退到 PNG
    ...
    return "none"                                    # 都没有：用系统默认，程序照常跑
```

- 包内目标目录 `assets` 与代码里的 `ASSET_SUBDIR` **必须一致**；
- 图标缺失时 `apply_window_icon()` 返回 `"none"`，**程序照常启动**（有意降级，不是静默失败）；
- spec 在缺图标时直接 `SystemExit`，**不会**产出"看着有图标、实际是默认图标"的程序。

> **改图标必须重新构建**：把旧 EXE 改名**不算是**换了图标 —— 资源管理器读的是 EXE 内部
> PE 资源表。验证方式见 §12：`tools/verify_exe_icon.py` 解析资源表逐字节比对。

### 10.3 打包环境（构建时需记录的事实）

| 项目 | 值 |
| --- | --- |
| 操作系统 | Windows 10（10.0.19045） |
| 架构 | 64 位（AMD64） |
| Python | 3.11.9（64 位） |
| tkinter / Tcl / Tk | 8.6 / 8.6 / 8.6 |
| requests | 2.34.2 |
| beautifulsoup4 | 4.15.0 |
| PyInstaller | 6.22.3 |

> **PyInstaller 不进 `requirements.txt`。** 它是**打包用的开发工具**，不是程序运行时依赖。
> 使用者（跑 EXE 的人）不需要它；只有"想自己重新打包"的人才需要单独安装它。

### 10.4 打包后的行为约束（重要）

1. **不假设 `__file__` 指向项目目录。** 打包后 `__file__` 指向临时解压目录，
   程序里的路径假设只有一处 —— `resource_roots()` **显式优先 `sys._MEIPASS`**
   （v008 §10.1 新增，用于定位应用图标），除此之外**不依赖** cwd、
   也不依赖 `tests/`、`tools/`、`stage-handoff/`、文档。
2. **默认下载目录**：`%USERPROFILE%\Downloads\KHInsider`，与项目目录无关。
3. **运行时只写两处**：用户选择的下载目录；以及系统 `%TEMP%\_MEIxxxxxx` 解压目录（退出自动清理）。
4. **SmartScreen**：EXE **未做数字签名**，首次运行可能弹出"Windows 已保护你的电脑"。
   这是未签名程序的**常规**提示，不代表程序有问题。处理方式：点"更多信息"→"仍要运行"。
   本项目**不会**、也不需要你关闭杀毒软件或修改系统安全设置。

### 10.5 分发件

`release\KHDownloader\` 是**给最终用户**的集中交付目录（**无阶段尾号**，唯一正式入口）：

```text
release/KHDownloader/
├─ KHDownloader.exe                          单文件可执行程序（14,928,399 字节，带新图标）
├─ README.md                                 面向"双击即用"用户的说明
├─ STAGE_EXECUTION_REPORT_v008.md
├─ _MANIFEST.json                            逐文件大小 + SHA-256
├─ source/                                   完整可复现源码（含 assets/）
└─ docs/                                     USER_RETEST / REQUIREMENTS / 技术文档 / 执行记录 / 截图
```

- 早期版本保留在 `release\history\` 与 `release\KHDownloader_v008\`，**不是当前入口**；
- **不含**任何测试缓存、`__pycache__`、`.workbuddy`、`.part`、`.mp3`、构建缓存、密钥；
- **不含** `LICENSE.txt` —— 因为项目此前**没有**许可证文件；任务书明确要求
  "不得凭空捏造许可证"，故不添加。

### 10.6 构建缓存与中间产物

| 路径 | 说明 | 处置 |
| --- | --- | --- |
| `KHDownloader.spec` | 打包配置（手写维护，不是生成物） | **保留**（程序名/图标/资源的唯一来源） |
| `build/` | 中间缓存 | 可丢弃（`--clean` 会重建） |
| `dist/` | 生成产物所在 | 生成物，可重建 |
| `assets/` | 应用图标 | **保留**（打包必需） |
| `release/KHDownloader/` | 集中交付目录 | **保留**（对外交付） |

> 未删除任何历史文件。

### 10.7 打包后的验证证据（摘要）

> 完整证据见 §12 与各阶段执行报告。

| 验证项 | 做法 | 结果 |
| --- | --- | --- |
| 源码测试 | `unittest discover`（项目根 `source/`） | **179/179 通过**（v1.0.0 现测，33.2 s，退出码 0） |
| **冻包传输层自检** | 交付 EXE 自身 `KHDownloader.exe --selftest-transport --selftest-out …`（v1.0.0 新增） | **PASS**：`curl_cffi: True`；真站专辑页 200 / 82,918 字符 / 30 首；**无需升级**，直接 `L1:impersonate=chrome` |
| **冻包依赖验收** | `tools/build_frozen.py` 构建后自动开包核对 `curl_cffi` / `_wrapper*.pyd` / `libcurl-impersonate-*.dll` | **全部在包内**（缺任一项即构建失败） |
| 冻结运行时自检 | 自检 EXE 检查 tkinter/Tcl、requests/bs4、核心解析、GUI 控件、**图标资源定位与窗口图标** | **14/14 通过** |
| 干净目录验证 | 只把 EXE 复制到项目树外的空目录（**目录里只有这一个文件**）+ 精简 PATH 启动 | **启动成功，窗口图标生效** |
| 图标真实嵌入 | `tools/verify_exe_icon.py` 解析 PE 资源表，图标组 7 个规格与源 ICO **逐字节**比对 | **7/7 一致** |
| 窗口标题栏图标 | `PrintWindow` 抓非客户区 + `GetClassLongPtrW(GCLP_HICON)` + 像素统计 | **句柄非零；16×16 蓝底白内容清晰可辨** |
| 资源管理器文件图标 | `ExtractIconExW` 取 32×32 位图，与源 ICO 及旧 EXE 逐像素比对 | **与源 ICO 平均色差 0；与旧 EXE 差异 87.5%** |
| 真站端到端（v1.0.0） | `tools/verify_cloudflare_bypass.py` 三组对照 + `tools/verify_real_album.py` | **14/14 通过**（裸 requests 仍 403 反证；修复后 30 首；真下载 4,930,262 字节、头 `ID3`） |

> ⚠️ **注意 `WM_GETICON` 返回 0 不代表没有图标**：Tk 的 `wm iconbitmap` 走的是
> `SetClassLongPtr`（**类图标**）路径，正确查法是 `GetClassLongPtrW(hwnd, GCLP_HICON)`。
> 只查 `WM_GETICON` 会得出"没有图标"的错误结论。

---

## 11. 恢复流程与错误分类（v007 新增）

### 11.1 三层请求的错误分类

`fetch_html()` 与 `download_file()` 现在按证据**分别**报错，不再"不是 200 就模糊处理"：

| 情形 | 判据 | 异常类型 | 用户的下一步 |
| --- | --- | --- | --- |
| Cloudflare 人机校验 | `cf-mitigated: challenge` 头，或页面含 `Just a moment...` / `challenges.cloudflare.com` | `SiteBlockedError` | **v1.0.0：程序已自动换指纹并逐级升级**（§13），通常不需要人工介入；仍失败时才用"导入 HTML / 单个链接" |
| 普通 403 | 是 403，但**没有**校验页特征 | `AccessDeniedError` | 核对地址；这不是"人机校验" |
| 429 限流 | 状态码 429 | `RateLimitError` | 稍后再试（不是人机校验） |
| 超时 / 连不上 | `requests.Timeout` / `ConnectionError` | `NetworkError` | 检查网络 |
| 内容是 HTML/JSON 而非音频 | `Content-Type` 为 `text/*` / `application/json` 等 | `NotAudioContentError` | 链接指向的不是 MP3，换链接 |

> **关键**：`AccessDeniedError` 与 `SiteBlockedError` 是**不同的类**。软件**只凭 403** 时
> 会归入 `AccessDeniedError`（"访问被拒绝"），**不会**武断说成 Cloudflare —— 只有当出现
> 校验页证据时才归人机校验。这回应了 Brief §2.4 的存疑点。

### 11.2 恢复入口一：导入专辑页面（HTML）

- 用户从**正常浏览器**保存专辑页 HTML，程序读取并在**工作线程**解析曲目；
- `inspect_import_html()` 把导入内容分成四类：**正常专辑页 / 验证页 / 错误页 / 无曲目页**，
  **绝不**把验证页当成功；
- 导入的页面多为相对链接，因此**必须先填原始专辑 URL**，用它把相对链接拼成绝对地址；
- 导入成功后逐首走"详情页 → MP3"的既有管线（顺序、可取消、`.part` 安全、已有文件跳过）；
- **局限**：导入 HTML **不会**恢复浏览器的验证状态；**详情页仍可能被拒**，此时需改用入口二。

### 11.2a 导入工作流 v008：先识别文件，再核对地址（重要修正）

**v007 的缺陷**（用户实测确认）：`parse_album_page` 按 `album_path(用户地址)/` 过滤曲目链接，
用户把 2003 专辑的 HTML 导入、地址框却留着 2011 的地址时，所有曲目被过滤光，
程序报"没有解析到任何曲目链接" —— 把**地址不匹配**误报成了**页面没曲目**。

**v008 流程**：获取与保存网页指引 → 导入检查 → 自动识别专辑 → 核对来源地址
→ 曲目预览 → 用户主动开始下载。

新增核心组件：

- `identify_album_source(html)`：在按地址过滤**之前**，从页面自身识别来源。
  依据优先级：`table#songlist` 中曲目详情页链接的专辑 slug（主证据，取众数并核实唯一）
  → canonical / og:url（辅助，冲突时以曲目表为准并记录）。**不**从 `<title>` 推算 slug，
  **不**采集推荐专辑/导航/评论区的链接；外站 mp3 链接不参与识别。
- `album_identity(url)` / `same_album(a, b)`：地址比较按（主机小写去 www，路径去尾斜杠）；
  查询参数与片段不计；**不**循环解码 `%2520` —— 不同专辑绝不能因解码混同。
- `is_song_page_url(url)`：识别 `/album/<slug>/NN….mp3` 形态（歌曲详情页地址）。
- `detect_mhtml(content)`：识别 MHTML 归档（`From:`/`MIME-Version:` 开头或
  `multipart/related`）→ 明确报"格式不支持"，而不是"没有曲目"。
- `ImportInspection` 扩展字段（向后兼容，均有默认值）：`detected_url` / `page_title` /
  `evidence` / `conflicts` / `matched_address` / `address_relation`。
- 新增导入类别：`unresolved`（无法可靠识别来源，不猜、不解析）与
  `unsupported_format`（MHTML 等归档格式）。

**地址与文件来源五种关系**（`address_relation`）：

| relation | 触发 | 行为 |
| --- | --- | --- |
| `match` | 地址与文件同专辑 | 直接解析出曲目 |
| `empty` | 地址框为空、文件可识别 | 按**识别出的地址**解析；界面回填地址请用户确认 |
| `different_album` | 地址是另一张专辑 | 报"不是同一张专辑"并列出双方地址；界面提供"使用网页中的专辑地址"确认框；**不静默替换、不报无曲目** |
| `song_page` | 地址是曲目详情页 | 说明填的是歌曲页；能识别专辑时提供一键切换 |
| `unresolved` | 文件识别不出来源 | 保留输入，请用户核对；**不猜、不启动下载** |

解析时一律用**识别出的地址**（`detected_url`，主机适配：root-relative 曲目链接无法自证主机，
用户地址的 slug 与识别 slug 一致时以用户地址的主机为准 —— 本地镜像/mock 站点因此可用）。
这样"地址填错"**永远不会**把曲目全部滤光。

### 11.2b 导入预览与主动开始（GUI）

- 导入检查通过后**不立即下载**：`_preview_and_confirm()` 弹预览
  （专辑名 / 确认地址 / 曲目数与顺序 / 保存目录 / 识别依据 / 冲突），
  用户点确认才 `_start_confirmed_import()`；
- 预览与下载使用**同一份确认对象**（`self.confirmed_import`），**不**回头读地址框；
- 主窗口常驻来源标记 `source_var`：`来源：直接请求（…）` / `来源：已导入网页（<地址>）`；
- 切换来源必须显式：确认导入后点"开始下载"或"下载单个链接"会**作废**导入任务并复位标记，
  绝不出现"显示新地址、下载旧专辑"；
- 预览取消不创建网络会话、不发任何请求。

### 11.2c 保存网页指引（GUI，轻量）

- `show_save_guide()`：分步指引（打开专辑页 → 等曲目列表出现 → `Ctrl+S` 另存为
  「网页，仅 HTML」→ 回来导入）；明确说明 `.mht/.mhtml` 归档不受支持、改扩展名无用；
- 地址为空或填了歌曲页时给出针对性提示；整合既有"用浏览器打开"，不堆叠重复按钮；
- **不**把 requests 请求包装成"保存当前浏览器网页"，**不**声称能读取外部浏览器标签页。

### 11.2d 回归样本来源（v008 修订补充）

`tests/fixtures/` 下的样本**必须区分来源**，逐文件清单与哈希见 `tests/fixtures/SOURCES.md`：

| 类别 | 文件 | 说明 |
|---|---|---|
| **用户原样保存页面** | `user_original_2003_album_page.html`（64,309 B）<br>`user_original_2011_album_page.html`（93,766 B） | 用户在正常浏览器中"另存为『网页，仅 HTML』"得到，按字节原样复制、未脱敏未改写；已扫描确认不含 Cookie / 凭据 / 本地绝对路径 |
| **合成样本** | `album_page_2003_synthetic.html` | 按 Strategy AI 审查数据结构对齐构造；**不是**用户原文件，任何报告中都不得称其为"用户原样文件" |
| 历史样本 | `album_page.html`、`real_album_page.html`、`real_song_page.html` 等 | v001–v003 期采集/构造，来源见各阶段报告 |

对应的回归测试：`tests/test_v008_user_original.py`（14 例，用原样文件）
与 `tests/test_v008_import_workflow.py`（20 例，合成 + mock 站）。
另有 `tools/verify_user_original_v008.py`（场景矩阵实跑）与
`tools/verify_e2e_2003_real.py`（原样 2003 导入 → 首曲真实下载 + 请求轨迹）两个可复跑的验证脚本，
两者都**优先从 `tests/fixtures/` 读样本**，因此在交付目录内也能直接跑。

### 11.3 恢复入口二：下载单个链接

- 用户粘贴**一个**公开 MP3 链接，`download_single_link()` 复用 `download_file` 引擎下载；
- 明确标注为**"单曲/链接下载"**，**不是**整张专辑恢复；
- 同样受 Content-Type 守卫保护，且取消/跳过/重名规则与专辑下载一致。

### 11.4 事件与状态

核心层新增事件：`single_fail` / `single_cancelled` / `single_done`。
界面状态覆盖：**正在获取页面 / 等待用户处理验证 / 页面已就绪 / 开始下载 / 完成 / 失败 / 取消**，
只在**确实观察到**时显示，不伪造"正在验证"的假进度。

### 11.5 诊断信息脱敏（`summarize_response_for_diagnostics`）

采集诊断时**只**保留白名单响应头（`content-type` / `cf-mitigated` / `cf-ray` …），
`Cookie` / `Set-Cookie` / `Authorization` **一律打码为 `<redacted>`** 且默认不出现；
**绝不**返回响应体全文、绝不记录浏览器凭据。

---

## 12. 验证工具（`tools/`）

这些脚本**不随程序分发逻辑**，是"证明某件事真的成立"的工具。全部可在交付目录
`release\KHDownloader\source\tools\` 内直接复跑。

### 12.1 图标验证（v008 §10.1 新增）

| 脚本 | 验证什么 | 怎么做的 |
| --- | --- | --- |
| `check_icon_source_mode.py` | 源码态图标可用 | `resource_path()` 找得到两个文件；`apply_window_icon()` 返回非 `"none"`；**切到别的 cwd 后仍能找到**（证明不依赖 cwd） |
| `verify_exe_icon.py` | 图标**真的编进 EXE** | **解析 PE 资源目录**：取 `RT_GROUP_ICON` 里每个尺寸的 `RT_ICON` 数据，与源 `assets/KHDownloader.ico` **逐字节**比对；再用 `CArchiveReader` 确认 ico/png 在 onefile 包内 |
| `verify_window_icon.py` | 窗口标题栏/任务栏图标 | 启动 EXE → `PrintWindow(PW_RENDERFULLCONTENT)` 抓非客户区 → 读 `GetClassLongPtrW(GCLP_HICON / GCLP_HICONSM)` → 统计标题栏左侧像素，输出 ASCII 图 |
| `verify_exe_file_icon.py` | 资源管理器显示的文件图标 | `ExtractIconExW` 取 32×32 位图 → 与源 ICO 比平均色/蓝像素比例 → 与参照旧 EXE **逐像素**比对 |
| `verify_frozen_selftest.py` | 冻结态资源定位与窗口图标 | 打包成 console EXE，在 `sys._MEIPASS` 环境下跑上述检查（14 项） |

**三个曾经踩过的坑，写在这里避免重蹈**：

1. **PE32 vs PE32+**：PyInstaller 默认产出 64 位 PE（optional header magic `0x20B`），
   data directory 起点是 `optional header + 112`，不是 `+ 96`。用错会读不到资源表，
   得出"没有图标"的假结论。
2. **资源树是三层** `TYPE → NAME → LANGUAGE`：查 `RT_ICON` 时键必须取**中间层 NAME**。
   取 `LANGUAGE` 会导致"图像数据缺失"的假阴性。
3. **只查 `WM_GETICON` 会误报**：Tk 的 `wm iconbitmap` 走 `SetClassLongPtr`（类图标），
   `WM_GETICON` 返回 0，但 `GetClassLongPtrW(hwnd, GCLP_HICON)` 非零。

**还有一条方法论上的教训**：最初"蓝像素占比 > 阈值"的判定对**新旧 EXE 都通过** ——
因为 PyInstaller 的默认应用图标同样是蓝色系。**判据必须有区分度**
（逐像素比对 / 与已知来源精确对照），否则等于没验证。
同理，ICO 内 PNG 用 filter=4（Paeth），反滤波只实现 0/1/2 时结构比对被静默跳过，
但结论文字却写成"已比对"—— 现在加了 `compared_with_source` 标志，
比对没真正成功时结论只敢说"部分通过"。

### 12.2 导入工作流验证（v008）

| 脚本 | 作用 |
| --- | --- |
| `verify_user_original_v008.py` | 对**用户原样** 2003 / 2011 页面实跑场景矩阵（正确地址 / 错误地址 / 空地址 / 歌曲页 / 各种规范化变体） |
| `verify_e2e_2003_real.py` | 原样 2003 导入 → **真实网络**下载首曲，并记录请求轨迹（证明"下载时不再请求专辑页"） |
| `inspect_site.py` | 单页诊断（拼 URL、判类型、列候选、导出脱敏摘要） |

两者都**优先从 `tests/fixtures/` 读样本**，因此在交付目录内也能直接跑，
不依赖任何本机绝对路径。

### 12.3 传输层与人机校验验证（v1.0.0 新增）

| 脚本 | 验证什么 | 关键设计 |
| --- | --- | --- |
| `verify_cloudflare_bypass.py` | **凭什么说"防火墙问题解决了"** | 四组对照：**[A] 反证组**（裸 `requests` 必须仍被 403 挑战，且额外验证"只改 UA 也不行"）→ **[B] 修复组**（换指纹后 200 / 曲目数 / 首曲名 / tier）→ **[C] 关闭开关**（必须抛 `SiteBlockedError`，提示里含做法、不含 `cf_clearance`，且不退化成 `NoTracksFoundError`）→ **[D] 真下载**（字节数 / `ID3` 头 / 无 `.part` 残留）。用法：`python tools/verify_cloudflare_bypass.py [专辑地址] [--no-download]`；14 项全通过退出码 0 |
| `verify_real_album.py` | 真实专辑端到端（v003 起） | 30 首全部解析 → 30 个详情页全部取到 MP3 直链 → 真实下载第一首并校验头部 |
| `build_frozen.py` | **冻包里到底带没带修复**（构建 + 验收一体） | 见 §12.4 |

### 12.4 打包驱动 `build_frozen.py`（v1.0.0 新增）

`build_exe.bat` 现在只是它的壳。它做四件事，任何一步不满足就**拒绝产出**：

1. **临时挪走过时的标准库 backport**：环境里若存在 `pathlib`/`enum34`/`typing` 这类
   旧包（PyInstaller 6.x 会直接拒绝构建），构建期间把它们挪进影子目录，结束（含异常）还原。
   只在 `site.getsitepackages()` / `getusersitepackages()` 内按名查找，**越界路径直接跳过**
   —— 这一条是被真事故逼出来的（见下）。
2. `python -m PyInstaller --noconfirm --clean KHDownloader.spec`。
3. **开包验收**：读 onefile 归档，确认 `curl_cffi` 模块、`_wrapper*.pyd`、
   `libcurl-impersonate-*.dll` 全在包里 —— **缺任一项即失败**。
   理由是：依赖静默丢失时程序"看起来正常"，只会悄悄退回普通请求，
   用户看到的仍是"被拦住"，而在 GUI（`console=False`）里没有任何输出可看。
4. **跑一次冻包传输层自检**（`KHDownloader.exe --selftest-transport`），把结果写盘并回显。

> **真事故（值得记住）**：第一版驱动用 `importlib.metadata.distribution().locate_file()`
> 反推 site-packages 路径，多算了一级，把**标准库**的 `Lib\pathlib.py` 搬走了，
> `import pathlib` 于是落到那个坏 backport 上，构建以
> `ImportError: cannot import name 'Sequence' from 'collections'` 失败。
> 现在的实现只在 site-packages 里查找并打印 `[跳过] <越界路径>`。
> 事后逐项核对过：`Lib\pathlib.py` 49,972 字节在位、`import pathlib` 解析到标准库、无影子目录残留。

这两个脚本加上前面的验证工具，构成 v1.0.0 主张"已解决"的全部证据链。

---

## 13. 传输层与四级升级阶梯（v1.0.0 新增）

> 这一节回答"防火墙问题到底怎么修的"。改动集中在**新模块** `source/khdownloader_transport.py`
> （37,518 字节），核心解析/命名/跳过/取消逻辑**一行未动**。

### 13.1 为什么要动传输层

站点判定的是**客户端库的网络指纹**（§7.1 列的排除项证明：请求头、Cookie、Referer、
频率、代理统统不是变量），所以修法只能落在"说话方式"上 —— 让发出的 TLS/HTTP2
流量与真浏览器一致，而这由 `curl_cffi` 的 `libcurl-impersonate` 完成。

### 13.2 阶梯（失败才升级，成功即停）

| 层 | 手段 | 可用条件 | 代码位置 |
| --- | --- | --- | --- |
| L1 | `curl_cffi` + `impersonate=<指纹>`，真浏览器网络指纹 | 装了 `curl_cffi`（EXE 内置） | `ImpersonatingSession` (`khdownloader_transport.py:401`) |
| L2 | 真浏览器过校验拿 Cookie，再交回 HTTP 层带指纹请求 | Playwright 可用 | `BrowserBridge.harvest` (`khdownloader_transport.py:622`) |
| L3 | 直接用无头浏览器渲染出的成品 HTML | Playwright 可用 | `_HtmlProbe` (`khdownloader_transport.py:855`) |
| L4 | **借本机** Chrome/Edge 无头渲染（`--headless=new --dump-dom`） | 系统里有 Chrome/Edge | `render_with_system_browser` (`khdownloader_transport.py:703`) |

- 主入口：`recover_html()` (`khdownloader_transport.py:767`) → `(response, html, tier)`；
  下载阶段用 `recover_stream()` (`khdownloader_transport.py:865`)。
- 指纹顺序 `PREFERRED_IMPERSONATIONS`（`khdownloader_transport.py:67`）：
  `chrome, chrome136, chrome145, chrome131, safari184, firefox135, edge`，
  运行时按"当前 `curl_cffi` 真的支持哪些"过滤（`_available_impersonations`, `:111`）。
- `tier` 直接写进日志与报错，用户看得见走了哪条路：
  `L1:impersonate=chrome` / `L1-failed:<试过的指纹列表>` / `L2:browser-cookie+chrome` /
  `L3:browser-html` / `L4:system-browser` / `failed:<试过的指纹列表>` / `skipped:not-https`。
- **不打扰本地**：`_is_remote_https()` (`khdownloader_transport.py:365`) 保证
  `http://` 与本地 mock 站点走原生 `requests`（测试离线、不开浏览器）。
  测试钩子 `KHD_TRANSPORT_ALLOW_LOCAL=1` 可以强制本地也走指纹层（专门用来验阶梯）。

### 13.3 四级为什么是这个顺序（以及为什么必须有 L4）

Playwright 的浏览器内核**没法一起打进 EXE**（体积会翻几倍），所以冻包版本用不了 L2/L3；
但用户的 Windows 上通常本来就装了 Edge 或 Chrome，于是 L4 用
`--headless=new --dump-dom --virtual-time-budget=<ms>` 借它渲染一次，把成品 HTML 拿回来。
这让"EXE 也能自动处理挑战"成立。L4 用**一次性临时配置目录**
（`--user-data-dir=<TemporaryDirectory(prefix="khd_browser_")>`，`khdownloader_transport.py:726`），
**不读**用户浏览器 profile。

### 13.4 与老接口的兼容（`TransportResponse`）

`curl_cffi.Response` 与 `requests.Response` 不完全同形，上层却按后者写。适配类补齐差异
（`khdownloader_transport.py:175`）：

| 差异 | 适配 |
| --- | --- |
| 不支持 `with response:` | `__enter__` / `__exit__` (`:329`) |
| `.encoding` 只读 | 改成可写属性 (`:238`) |
| 没有 `.apparent_encoding` | 用 `charset_normalizer` 兜底 (`:247`) |
| **非流式请求**上调 `iter_content()` 会 `assert self.queue and self.curl, "stream mode is not enabled."` | 捕获该 `AssertionError`，退化成"把已缓冲的 `content` 自己切块" (`:278`) |
| 其余异常族（`Timeout`/`DNSError`/…） | `map_transport_exception()` (`:342`) 翻译成 `requests` 的异常，`download_file()` 的既有错误分类不用改 |
| `.request.url` 等零散属性 | `__getattr__` 透传，缺的用 `_SyntheticRequest` (`:161`) 补 |

### 13.5 开关与回退

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `KHD_TRANSPORT` | `auto` | `requests` = **完全退回旧行为**；`impersonate` = 强制指纹层 |
| `KHD_IMPERSONATE` | `chrome` | 首选指纹名 |
| `KHD_BROWSER_FALLBACK` | `1` | `0` 关掉 L2/L3 |
| `KHD_SYSTEM_BROWSER` | `1` | `0` 关掉 L4 |
| `KHD_TRANSPORT_ALLOW_LOCAL` | 未设 | 仅测试：让本地 http 也走指纹层 |

### 13.6 界面与冻包自检

- 三个入口（专辑页、曲目页、下载）都接受可选 `on_log`，界面日志里能看到升级过程。
- 被拦时的对话框文案已从"请手工用浏览器打开再导入 HTML"改为"已自动逐级升级"。
- 隐藏自检：`KHDownloader.exe --selftest-transport [--selftest-out <路径>]`
  （`khdownloader_gui.py:1071`）。它跑 `describe()` + 真站取一次页面，写报告并给退出码，
  让 `console=False` 的 GUI 程序也能被脚本验收。双击启动时 argv 为空，行为不变。

### 13.7 新增的硬性依赖

`requirements.txt` 里 `curl_cffi>=0.7` **是必需的**（注释说明：不装也能跑，但会退回普通请求、
重新被拦）。打包侧，`KHDownloader.spec` 用 `collect_all("curl_cffi")` 把包自带的
`libcurl-impersonate` 动态库与 `cacert.pem` 一起收进去，并 `excludes=["playwright"]`。
**依赖静默丢失是最危险的失败模式**（程序照跑、只是又被拦），所以
`tools/build_frozen.py` 会在构建后开包核对，缺项即失败（§12.4）。
