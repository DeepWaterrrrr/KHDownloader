# 上传前安全自查（密钥 / 隐私 / 不该进仓库的东西）

- 自查日期：2026-10-19
- 自查对象：`D:\WorkSpace\agent\ds\KHDownloader` 全目录（含隐藏目录）
- 自查方式：脚本化正则扫描（`source/tools/scan_secrets.py`）+ 疑似长串人工核对 + 图片目视 + 打包产物开包检查
- 结论（一句话）：**没有任何可用的密钥、口令、Cookie 值或私钥**；发现 1 个第三方**公开** token（Cloudflare 统计 beacon）、若干可识别信息（Windows 用户名路径、本机代理端口），以及一大票**不该进仓库的中间产物**。

---

## 1. 检查了什么

| 类别 | 关注对象 | 判据 |
| --- | --- | --- |
| 云/平台密钥 | GitHub `ghp_/gho_/ghs_`、AWS `AKIA*`、OpenAI `sk-*` | 前缀 + 长度 |
| 通用凭据赋值 | `api_key` / `secret` / `password` / `token` 后跟引号字符串 | 正则 |
| 私钥 | `-----BEGIN * PRIVATE KEY-----` | 文本块 |
| 站点凭据 | `cf_clearance`、`__cf_bm`、`Set-Cookie`、`Cookie:` 头 | 是否带**值** |
| 会话标识 | `PHPSESSID` / `JSESSIONID` / `session_id` / `access_token` / `refresh_token` | 是否带**值** |
| JWT | `eyJ...eyJ...` 三段式 | 结构 |
| 长十六进制串 | 32 位以上 `[0-9a-f]` | 疑似 token/哈希 |
| 个人身份 | 邮箱、真实姓名、Git 提交身份 | 文本 + git config |
| 设备/网络指纹 | `C:\Users\<名>`、`IP:端口`、主机名 | 正则 |
| 二进制/大文件 | `*.exe`、`*.zip`、`*.pyz`、`build/`、`dist/`、日志 | 体积与用途 |
| 图片 | `docs/screenshots/*.png` | 目视是否含路径/账号 |

扫描范围：`*.py *.md *.txt *.json *.bat *.spec *.ps1 *.html *.log *.pyz`，跳过 `.git`、`__pycache__`、`build`、`dist`，单文件上限 6 MB。

---

## 2. 结果：密钥类 —— 全部 0 命中

| 项 | 命中 |
| --- | --- |
| GitHub token (`ghp_/gho_/ghs_`) | **0** |
| AWS 凭据 (`AKIA…`) | **0** |
| `sk-…` 类密钥 | **0** |
| 私钥块 `BEGIN … PRIVATE KEY` | **0** |
| `api_key`/`secret`/`password` 赋值 | **0** |
| JWT | **0** |
| `Set-Cookie` 头 / 带值的 `Cookie:` | **0** |

> 说明：本机 `gh` 的登录令牌存在 Windows 凭据管理器（keyring）里，**不在项目目录**，仓库里也没有 `.env`、`.netrc`、`*.pem`、`id_rsa` 之类文件。

## 3. 结果：站点凭据 —— 只有“提到”，没有“值”

`cf_clearance` 共出现 **60 次**，逐条核对后全部是**文档/注释/断言里的词**，例如：

- `source/khdownloader_transport.py:620` —— `return bool(cookies.get("cf_clearance")) and not looks_like_gate`（判断"浏览器兜底有没有拿到凭据"）
- `README.md:151` —— "程序**不会**……伪造 `cf_clearance`"
- `docs/TECHNICAL_DOCUMENTATION.md:937` —— 验证工具"提示里含做法、不含 `cf_clearance`"

**没有任何 cookie 名值对**被写进文件；测试用例里也没有真实 Cookie 快照。

## 4. 结果：长十六进制串 —— 全是哈希，外加 1 个第三方公开 token

32 位以上十六进制串逐条看过，来源只有三类：

1. **文件 SHA-256**：`_MANIFEST.json`、报告、`SOURCES.md`、`real_original_2003_download.txt`、`test_v008_user_original.py` 的断言（下载文件哈希）。
2. **专辑 slug**：`cross-channel-in-memory-of-all-people-special-soundtrack-2011`（公开网址的一部分）。
3. ⚠️ **Cloudflare 统计 beacon token**：`878420a4bf494e799df6d4e0060d3931`，出现在两个用户保存的网页快照里（`source/tests/fixtures/user_original_2003_album_page.html`、`user_original_2011_album_page.html`），形如：

   ```html
   data-cf-beacon='{"version":"2024.11.0","token":"878420a4bf494e799df6d4e0060d3931","spa":2}'
   ```

   这是 **Cloudflare 给全站访问者下发的前端统计标识**（每个访问者拿到的是同一个站点级值，不是个人凭据），任何人都能在页面源码里看到。保留无害，但它长得像密钥，特此记录，避免以后被误判为泄露。

## 5. 结果：个人可识别信息

| 项 | 位置 | 处理 |
| --- | --- | --- |
| Windows 用户名出现在绝对路径 | `docs/EXECUTION_LOG.md`（2 处）、`docs/history/…v008.md`（1 处）、`source/tools/real_original_2003_download.txt`（1 处）、`.diag/` 内多处 | 发布前已统一替换为 `C:\Users\<用户名>`；`.diag/` 不进仓库 |
| 本机端口 `127.0.0.1:5389` / `127.0.0.1:7890` | `docs/EXECUTION_LOG.md`（2 处，历史调试记录） | 保留（仅回环地址，无外泄风险）；`7890` 是常见代理端口，若有洁癖可删 |
| 截图里的保存路径 `C:\Users\<用户名>\Downloads\KHInsider` | `docs/screenshots/v008_gui_frozen_launch.png` | 已保留（只露用户名，无其他信息）；如需彻底干净可换图 |
| 真实姓名 / 邮箱 | 全项目文本 **0 命中** | — |
| Git 提交身份 | 全局配置是真实姓名 + QQ 邮箱 | **仓库内单独设成 noreply**，避免公开仓库把邮箱写进 commit |

## 6. 结果：不该进仓库的东西（体积 / 中间产物）

| 路径 | 体积 | 为什么不进仓库 |
| --- | --- | --- |
| `.diag/`（48 个文件） | ≈ 24 MB | 一次性调试产物：`_frozen_PYZ.pyz` 10.8 MB、`retest_hang_probe.txt` 637 KB、`retest_flake_full.txt` 481 KB、含内部推演的 `b1_recall.md` |
| `release/` | ≈ 74 MB | 打包产物：`KHDownloader_RELEASE_v1.0.0.zip` 44.5 MB、`KHDownloader.exe` 43.6 MB、旧版 `v008.exe` 14.9 MB |
| `source/dist/`、`source/build/` | ≈ 87 MB | PyInstaller 中间物（`.pkg`、`PYZ-00.pyz`、`_internal/`） |
| `__pycache__/` | — | 字节码缓存 |
| `KHDownloader.exe`（根目录） | 43.6 MB | 二进制不进 Git；改挂 **Release 附件** |
| `_MANIFEST.json`、`_RELEASE_MANIFEST.json` | 14 KB | 描述的是**上一版发布包**的逐文件哈希，整理后路径已变，留在仓库里只会误导 |

> Git 对单文件硬上限 100 MB、警告线 50 MB；把 43.6 MB 的 EXE 提交进去会让每次 clone 都背上它，且无法瘦身（历史不可删）。所以：**源码进 Git，二进制走 Release**。

## 7. 代码里与"敏感"沾边但属于正常设计的部分

- `source/khdownloader_transport.py` 的 L2/L3 会**自己开无头浏览器**过一次校验，把凭据留在**进程内会话**里 —— 不落盘、不写日志、不导入用户浏览器配置（临时 `user-data-dir`，用完删）。
- 诊断输出有**响应头白名单**，`Cookie`/`Set-Cookie`/`Authorization` 在拒绝清单里，日志不会带出凭据。
- `source/tests/fixtures/*.html` 是站点**公开页面**的快照（测试夹具），不含任何登录态页面。

## 8. 我建议的发布配置

```
进仓库：README.md、LICENSE、.gitignore、docs/（文档+截图+历史）、source/（代码/测试/工具/assets）
不进仓库：.diag/、release/、source/build/、source/dist/、__pycache__/、*.exe、_*MANIFEST.json
Release 附件：KHDownloader-v1.0.0-win64.zip（含 EXE + 文档 + 源码 + 哈希清单）
```

## 9. 复现这次自查

```bat
cd source
python tools\scan_secrets.py --root .. --json ..\.diag\secrets_scan.json
```

退出码 0 = 无"硬命中"（密钥类）；发现疑似凭据时打印文件:行号并以退出码 1 结束，可直接接进 CI。

## 10. 发布结果（已执行）

| 项 | 值 |
| --- | --- |
| 仓库 | <https://github.com/DeepWaterrrrr/KHDownloader>（**公开**） |
| 首次提交 | `cc9640e` —— 59 个文件（源码 + 文档 + 夹具），无二进制、无 `.diag/` |
| 版本 | `v1.0.0`（原内部阶段号 v009 全面弃用） |
| Release | <https://github.com/DeepWaterrrrr/KHDownloader/releases/tag/v1.0.0> |
| 附件 | `KHDownloader-v1.0.0-win64.zip`（44,674,684 字节，SHA-256 `bbbcd426…c5bc2`）、`KHDownloader.exe`（43,618,401 字节） |
| 提交身份 | 仓库内单独设为 `DeepWaterrrrr <DeepWaterrrrr@users.noreply.github.com>`，未使用全局真实邮箱 |

已处理：绝对路径全部换成 `C:\Users\<用户名>`；`verify_user_original_v008.py` 里硬编码的本机目录改为环境变量 `KHD_ORIG_DIR`。

保留并知情的两处：

1. `docs/screenshots/v008_gui_frozen_launch.png` 里能看到 `C:\Users\<用户名>\Downloads\KHInsider` —— 只露用户名，无其他信息；
2. `docs/EXECUTION_LOG.md` 里两个回环端口（`127.0.0.1:5389` / `127.0.0.1:7890`），历史调试记录。

> 想改成私有：`gh repo edit DeepWaterrrrr/KHDownloader --visibility private --accept-visibility-change-consequences`

