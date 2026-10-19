# KHDownloader

KHInsider 专辑页 MP3 批量下载器，Windows 图形界面，**免安装单文件 EXE**（约 43.6 MB）。

它能干的事很简单：粘一个专辑地址 → 选个目录 → 它把整张专辑的 MP3 按曲序下好，跳过已存在的，取消不留半个文件。

它存在的理由是另一半：**KHInsider 前面挡着 Cloudflare 的人机校验**，普通脚本请求专辑页会拿到 `403 + cf-mitigated: challenge`，于是"填地址自动下载"这条最自然的路线在很多机器上根本走不通。本项目把这条路线修通了 —— 不是靠答题或伪造令牌，而是**用与真浏览器一致的网络指纹去读一张公开页面**，必要时自动逐级升级到真浏览器渲染。

```
$ KHDownloader.exe            # 双击也行
[L1] 用 Chrome 指纹请求 → 200 · 30 首
下载 01/30 ... 完成 30 | 跳过 0 | 失败 0
```

## 快速开始

**用发布版**：到 [Releases](../../releases) 下载 `KHDownloader-v1.0.0-win64.zip`，解压双击 `KHDownloader.exe`。不需要装 Python，不需要装任何库。

**从源码跑**：

```bat
cd source
python -m pip install -r requirements.txt
python khdownloader_gui.py
```

需要 Python 3.11+（开发环境是 3.11.9 / Windows 10 19045）。

**自证修复是否生效**（不下载文件，只取一次页面并打印诊断）：

```bat
KHDownloader.exe --selftest-transport --selftest-out selftest.txt
:: 退出码 0 = 通过；报告里有 transport 层级、状态码、字符数、曲目数、升级过程
```

## 它怎么绕过那道校验

真正被拦的不是 User-Agent。换一整套浏览器请求头、带上前序访问流程、限速重试，仍然是 `403` —— 站点看的是 **TLS 握手与 HTTP/2 帧特征**（密码套件顺序、扩展集、伪头顺序……），这些由**客户端库**决定，换头没用。实测对照见 [`docs/防火墙问题说明.md`](docs/防火墙问题说明.md)。

所以传输层做成一条**四级升级链**，成功即停：

| 层 | 手段 | 依赖 | 典型耗时 |
| --- | --- | --- | --- |
| **L1** | `curl_cffi` + `libcurl-impersonate`，与真 Chrome 同指纹 | 已打进 EXE | 毫秒级 |
| **L2** | 程序自开无头浏览器过一次校验，把凭据交回 HTTP 层重试 | `playwright`（源码版） | 数秒 |
| **L3** | 直接用无头浏览器渲染出的成品 HTML | `playwright`（源码版） | 数秒 |
| **L4** | 调**本机** Chrome / Edge `--headless=new --dump-dom` 渲染一次 | 系统浏览器 | 数秒 |

EXE 版排除 `playwright`（内核太大），实际可用 **L1 → L4**；日志里能看到它停在哪一层（`L1:impersonate=chrome`、`L4:system-browser` 等）。四层都没过时报"被站点人机校验拦住"，并给出可执行建议，**不会**退化成"没找到曲目"。

### 边界（写进代码，也在测试里断言）

- 不答题、不接打码接口、不调用任何验证码服务；
- 不伪造 `cf_clearance`、不硬编码任何凭据、不复用他人会话；
- 不绕过登录、付费墙或限流；
- L2/L3/L4 一律开**全新临时浏览器配置**，不读你浏览器里的 Cookie、书签、密码；
- 诊断日志有响应头白名单，`Cookie` / `Set-Cookie` / `Authorization` 在拒绝清单里。

## 目录结构

```
KHDownloader/
├── README.md                      本文件
├── LICENSE                        MIT
├── docs/
│   ├── USER_MANUAL.md             使用者手册（界面、两条路线、文件安全行为）
│   ├── 防火墙问题说明.md           问题界定：现象、证据、触发条件、结论边界
│   ├── REPORT_v1.0.0_防火墙修复.md  修复报告：根因、设计、验证证据
│   ├── TECHNICAL_DOCUMENTATION.md 技术文档（含 §13 传输层全章）
│   ├── USER_RETEST_v1.0.0.md      上手复测清单
│   ├── SECURITY_AUDIT.md          上传前安全自查（密钥 / 隐私 / 不该进仓库的东西）
│   ├── REQUIREMENTS.md            需求与不做清单
│   ├── EXECUTION_LOG.md           开发日志（v001 → v1.0.0）
│   ├── screenshots/               界面截图
│   └── history/                   v008 及更早的存档文档
└── source/
    ├── khdownloader_gui.py        图形界面（tkinter，含 --selftest-transport）
    ├── khdownloader_core.py       解析、命名、下载、错误分类
    ├── khdownloader_transport.py  传输层：指纹 / 升级链 / 浏览器兜底
    ├── requirements.txt           运行依赖
    ├── KHDownloader.spec          PyInstaller 配置（curl_cffi 必须整体收集）
    ├── build_exe.bat              一键打包（→ tools/build_frozen.py）
    ├── assets/                    图标
    ├── tests/                     179 个单元测试 + 本地 mock 站点 + 页面夹具
    └── tools/                     验证与构建工具（见下）
```

## 开发

**跑测试**（离线，全部走本地 mock 站点，不碰外网）：

```bat
cd source
python -m unittest discover -s tests -p "test_*.py"
:: 179 tests, OK
```

**验证工具**（都要打真站点，属于"凭什么说修好了"的证据链）：

| 工具 | 作用 |
| --- | --- |
| `tools/verify_cloudflare_bypass.py` | 四组对照：裸 `requests` 必须仍 `403`（含"只改 UA 也不行"）→ 修复后 `200` 且曲目数正确 → 关开关必须抛 `SiteBlockedError` → 真下第一首并校验 `ID3` 头。14 项全过退出码 0 |
| `tools/verify_real_album.py` | 真实专辑端到端：30 首曲名、30 个详情页、MP3 直链 |
| `tools/scan_secrets.py` | 上传前泄露自查（密钥 / 凭据 / 本机路径 / 体积炸弹） |
| `tools/inspect_site.py` | 把某个地址的响应头、挑战特征、曲目数打出来 |

**打包**：

```bat
cd source
build_exe.bat                :: 构建 + 开包验收 + 冻包自检
build_exe.bat --selftest     :: 只跑冻包自检
```

`tools/build_frozen.py` 做四件事：预检依赖 → 构建期间临时挪走 PyInstaller 拒绝的过时 backport（`pathlib`，不改你的 Python 环境）→ **开包验收** `curl_cffi` 模块、`_wrapper.pyd`、`libcurl-impersonate-*.dll` 是否真的进包 → 跑冻包自检。

两个真实踩过的坑：

1. `pip install pathlib` 装过的话 PyInstaller 会直接拒绝构建（报 obsolete backport）。脚本在构建期间把那个包挪开，`finally` 里还原。
2. spec 里必须 `collect_all("curl_cffi")` 才能把自带的 libcurl 动态库和 cacert 一起打进包；`hiddenimports` 手写不够 —— 否则打包出来的 EXE 会**静默退回普通请求**，只用源码测是发现不了的。

## 版本

| 版本 | 说明 |
| --- | --- |
| **v1.0.0** | 传输层重写（指纹 + 四级升级链）、冻包自检、打包验收。此前内部编号 v009 |
| v0.8 | 导入网页兜底路线、`identify_album_source` 专辑识别、图标与文件安全加固 |

## 文档索引

- 想**用**：[`docs/USER_MANUAL.md`](docs/USER_MANUAL.md) → [`docs/USER_RETEST_v1.0.0.md`](docs/USER_RETEST_v1.0.0.md)
- 想**判断这个结论可不可信**：[`docs/防火墙问题说明.md`](docs/防火墙问题说明.md)（现象与证据） → [`docs/REPORT_v1.0.0_防火墙修复.md`](docs/REPORT_v1.0.0_防火墙修复.md)（修复与验证）
- 想**读代码**：[`docs/TECHNICAL_DOCUMENTATION.md`](docs/TECHNICAL_DOCUMENTATION.md)（§13 是传输层）
- 想**发布**：[`docs/SECURITY_AUDIT.md`](docs/SECURITY_AUDIT.md)

## 许可与声明

代码以 MIT 许可发布，见 [LICENSE](LICENSE)。

程序自身不含任何音频内容，只从你给的公开页面里取链接并下载。请**仅用于你有权获取的内容与个人备份**，并遵守站点条款与当地版权法；由此产生的后果由使用者承担。
