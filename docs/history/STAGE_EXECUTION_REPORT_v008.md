# KHDownloader 阶段执行报告 v008（第二次修订：最终命名与应用图标）

- **阶段类型**：Corrective / GUI Usability —— 专辑导入识别 / 地址核对 / 曲目预览 / 网页保存指引
- **起始版本**：v007
- **对应 Brief**：`KHDownloader_STAGE_EXECUTION_BRIEF_v008.md`
  （2026-10-11 第一次修订：集中交付 + 审查信息写入 Report；
  2026-10-11 第二次修订：**新增 §10.1 最终程序无尾号命名与图标**）
- **本轮裁决**：`Implemented — Awaiting User Verification`
- **编制日期**：2026-10-12（GMT+8）
- **本 Report 位置**：`stage-handoff/reports/STAGE_EXECUTION_REPORT_v008.md`
  与集中交付目录内副本 `release/KHDownloader/STAGE_EXECUTION_REPORT_v008.md`（两份内容一致）

> **本次修订做了什么**
>
> **第一次修订**（§11 / §11.1）：Brief §2–10 的功能与验证要求已实现；新建集中交付文件夹、
> 按 §11 重写面向用户的 README、按 §11.1 把全部审查信息写入本 Report，并补齐交付副本验证。
> 期间**首次拿到用户原样保存的 2003 HTML**（上一轮它在执行环境中不存在），
> 把 Brief §9 第一行的"用户原样文件"场景从"合成样本代替"升级为**原样文件实跑验证**。
>
> **第二次修订**（本次，§10.1）：Strategy AI 随 Brief 提供了两个真实图标文件
> （`KHDownloader.ico` / `KHDownloader.png`），要求
> ① 最终程序名固定为**无尾号**的 `KHDownloader.exe`；
> ② 交付根目录固定为 `release/KHDownloader/`；
> ③ **图标必须真正嵌入 EXE**（改文件名不算）；
> ④ 设置 Tk 窗口图标并验证标题栏/任务栏；
> ⑤ 已有 `release/KHDownloader/` 先备份再整理。
> 本次为此**修改了 `khdownloader_gui.py`（新增资源定位与窗口图标）**，
> 因此 **EXE 已重新构建**（新名 + 新图标），旧哈希不再适用。
> `khdownloader_core.py`（下载与解析逻辑）本轮**未做任何改动**。

---

## 1. 确认的根因

用户报告：导入 2003 专辑 HTML（CROSS†CHANNEL Soundtrack Arrange Version CRISS†CROSS）时，
程序提示"这个页面里没有解析到任何曲目链接"，但网页里明明有 19 首歌。

**根因链**（本轮用源码复核 + 原样文件实跑复现）：

1. `parse_album_page()` 用 `album_path(用户地址) + "/"` 去**过滤**页面里的曲目链接
   —— 地址填错 → 全部曲目被滤掉；
2. `inspect_import_html()` 把"过滤后没有曲目"一律归类为 `no_tracks`；
3. GUI `import_album_page()` **先强制要求地址框非空**，再拿这个地址去解析文件；
   识别成功后**直接开始下载**，既没有来源核对，也没有预览。

即：**文件检查与用户输入地址耦合，顺序做反了** —— 先按地址过滤、再下"没有曲目"的结论。
用户随后确认：失败原因就是地址框与导入的网页不对应。

**修复方向（Brief §4.1）**：把顺序倒过来 —— **先识别这份文件属于哪张专辑，再与地址核对**。

---

## 2. 工作 A：专辑自动识别与地址核对

### 2.1 识别规则（`identify_album_source`）

在**任何地址过滤之前**运行，只看页面自身：

| 依据 | 处理方式 |
|---|---|
| `table#songlist` 内曲目详情页链接（`/game-soundtracks/album/<slug>/NN….mp3`） | **主证据**。取 slug 众数；出现多个不同 slug → 记冲突、判定为不可识别 |
| `<link rel="canonical">` / `<meta property="og:url">` | **辅助**。与曲目表冲突时以曲目表为准并记录冲突；指向外站则记冲突 |
| 页面 `<title>` | **仅用于展示**，绝不从中推算 slug |
| 推荐专辑 / 导航 / 评论区链接 | **不采集**（表外的链接一律不看） |
| 外站 mp3 链接（CDN 等） | 不参与来源识别，记冲突 |
| `<base>` / 多专辑候选 | 不盲信；无法可靠识别 → `unresolved`，请用户核对 |

其他约束：不执行导入 HTML 的脚本、不读取其本地资源引用、公开 URL 编码原样保留（不解码 `%2520`）。

关键实现（`khdownloader_core.py`）：

```python
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
```

返回字段：`identified / album_slug / album_url / canonical_url / title / track_count /
track_hrefs / evidence / conflicts / has_songlist`。

### 2.2 地址核对（五种关系）

```python
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
```

`album_identity()` 的比较口径 =（主机小写、去 `www.`）+（专辑路径去尾斜杠）；
查询参数与片段不计入；**不反复解码** `%2520`。

| relation | 含义 | 行为 |
|---|---|---|
| `match` | 地址与网页同一张专辑 | 直接解析，预览显示"已核对" |
| `empty` | 地址框为空 | 允许导入；按**识别出的地址**解析，界面回填并请用户确认 |
| `different_album` | 地址是**另一张**专辑 | 同时展示"你填的地址"与"网页真正的地址"，由用户决定；**不静默替换、不报"没有曲目"** |
| `song_page` | 填的是曲目详情页 | 说明填的是歌曲页；能识别专辑时提供一键切回 |
| `unresolved` | 来源无法确定 | 保留输入、说明原因；不猜、不下载 |

**关键不变式**：解析一律使用**识别出的地址**（`_resolved_address()`），而不是用户输入地址
—— 地址填错永远不会再把曲目滤光。

**主机适配**（支持本地镜像 / mock 站 / `www` 前缀）：

```python
    if detected_url and user_host:
        # 用户主机 ≠ 官方主机且 slug 与用户地址路径一致 → 用用户主机
        if src["album_slug"] and f"/{src['album_slug']}" in urlsplit(address_try).path.rstrip("/"):
            detected_url = f"{user_host}{urlsplit(detected_url).path}"
        elif src["album_slug"] and not same_album(address_try, detected_url):
            # 不同专辑：检测地址也按用户主机回填（slug 用页面识别出的）
            detected_url = f"{user_host}{urlsplit(src['album_url']).path}"
```

> 为什么需要它：页面里的曲目链接是 **root-relative**（`/game-soundtracks/album/...`，不带主机），
> 无法自证主机。若强制按官方域名构造，本地镜像 / 测试用 mock 站会被误判成"不同专辑"。

### 2.3 冲突确认后**重新解析**

用户在冲突对话框点"是"之后，GUI 把识别出的地址**显式写回地址框**，并用**新地址重新跑一次检查**拿曲目：

```python
        if relation == "different_album":
            # 地址框是另一张专辑：明确展示双方，让用户选择，不静默替换。
            use_detected = messagebox.askyesno(
                "地址与网页不是同一张专辑",
                f"地址框里填的是：\n  {raw_url}\n\n"
                f"但导入的网页属于：\n  {detected}\n\n"
                f"（{inspection.evidence}）\n\n"
                "要使用**网页中的专辑地址**继续吗？\n"
                "选「否」则取消导入，地址框保持不变。",
            )
            if not use_detected:
                self.log("—— 导入已取消：地址与网页不属于同一张专辑 ——")
                return
            # 用户确认：把网页识别出的地址填入地址框（显式、可见）
            self.url_var.set(detected)
            raw_url = detected
            relation = "match"
            self.log(f"已按你的确认改用网页识别的专辑地址：{detected}")
```

```python
        # ---- 解析曲目（match 之后才解析；用确认过的地址）----
        if not inspection.tracks:
            # song_page/different_album 分支确认后，core 只给了核对信息，
            # 这里用确认后的地址重新检查一次拿到曲目。
            inspection = inspect_import_html(html, raw_url)
            if inspection.kind != IMPORT_KIND_ALBUM or not inspection.tracks:
                messagebox.showwarning("没有解析到曲目", inspection.message)
                self.log(f"—— 导入失败：{inspection.message.splitlines()[0]} ——")
                return
```

---

## 3. 工作 B：导入预览与用户主动开始

导入识别成功后**不立刻请求详情页、不开始下载**，先出预览：

```python
    def _preview_and_confirm(self, inspection, album_url: str, imported_file: str,
                             folder: Path):
        """显示曲目预览对话框。返回确认 dict；取消返回 None。"""
        tracks = inspection.tracks
        title = inspection.page_title or album_url
        lines = [
            f"专辑：{title}",
            f"专辑地址：{album_url}",
            f"曲目数：{len(tracks)}",
            f"保存到：{folder}",
            "",
            "曲目顺序：",
        ]
        for i, t in enumerate(tracks, start=1):
            lines.append(f"  {i:>2}. {t.name}")
        lines += [
            "",
            f"来源文件：{Path(imported_file).name}",
            "（识别依据：" + (inspection.evidence or "地址相符") + "）",
        ]
        if inspection.conflicts:
            lines.append("注意：" + "；".join(inspection.conflicts))

        ok = messagebox.askyesno(
            "导入预览 —— 确认后开始下载",
            "\n".join(lines) + "\n\n确认无误就点「是」开始下载；点「否」返回不下载。",
        )
        if not ok:
            return None
        return {
            "album_url": album_url,
            "tracks": list(tracks),
            "title": title,
            "source_file": imported_file,
        }
```

用户点「是」后进入下载，**用预览确认过的同一份对象**，不回头读地址框：

```python
    def _start_confirmed_import(self, confirmed: dict):
        """用**预览确认过的**导入对象启动下载线程（不再请求专辑页）。"""
        tracks = confirmed["tracks"]
        album_url = confirmed["album_url"]
        folder = self._current_folder()
        if folder is None:
            return

        # 记录本次已确认的任务（改地址/切模式时会显式作废）
        self.confirmed_import = confirmed
        ...
        self.source_var.set(f"来源：已导入网页（{album_url}）")
        self.set_running(True)
        self.log(f"来源：已导入网页 —— {confirmed.get('source_file', '')}")
        self.log(f"专辑地址：{album_url}")
        self.log(f"保存到：{folder}")

        worker = threading.Thread(
            target=self._import_worker,
            args=(tracks, album_url, folder),
            daemon=True,
        )
        worker.start()
        self.root.after(POLL_INTERVAL_MS, self._pump_events)
```

其他要求落实：

- 主窗口常驻来源标记：`来源：直接请求（…）` / `来源：已导入网页（<地址>）`；
- 点「开始下载」（直接请求）或「下载单个链接」会**显式作废**导入任务并复位标记
  —— 不会出现"显示新地址、下载旧专辑"；
- `_finish` / `_fatal`（任务结束 / 中止）同样复位导入状态；
- 运行中「导入专辑页面…」「下载单个链接…」「保存网页指引」等入口统一禁用（`set_running`）；
- 保存目录在启动时确定并校验（`_current_folder()`），界面显示的就是实际使用的目录。

---

## 4. 工作 C：获取网页 / 保存指引

按钮 **「保存网页指引」**，与「用浏览器打开」并列（不堆叠重复入口）。三种分支：

```python
    def show_save_guide(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showinfo("保存网页指引", "1. 先在地址框粘贴专辑地址（…）...\n"
                                                "5. 回到本程序，点「导入专辑页面…」选择刚保存的文件。")
            return
        if is_song_page_url(url):
            messagebox.showinfo("保存网页指引", "地址框里填的是一首歌的详情页。...")
            return
        messagebox.showinfo(
            "保存网页指引",
            "按这个顺序做：\n\n"
            "1. 点下方「用浏览器打开」，在正常浏览器里打开这个专辑页。\n"
            "2. 等页面完全加载、**曲目列表真正出现**（能看到每首歌的名字）。\n"
            "3. 按键盘 Ctrl+S 保存网页：\n"
            "     · 保存类型务必选「网页，仅 HTML」（.html）——\n"
            "       本版本支持 .html/.htm；\n"
            "     · 「网页，单个文件」（.mht/.mhtml）是归档格式，本版本**不支持**，\n"
            "       改扩展名也没有用，请重新另存。\n"
            "4. 回到本程序，点「导入专辑页面…」选择刚保存的 .html 文件。",
        )
        self.log("已显示保存网页指引（等待曲目列表出现 → Ctrl+S 另存为「仅 HTML」→ 导入）。")
```

**能力与限制（如实）**：

- 本工具**只做指引**：它在正常浏览器里打开的是**你**的浏览器，保存动作由你按 `Ctrl+S` 完成；
- **不**把程序自己的 requests 请求包装成"保存了你的浏览器网页"；
- **不**声称能读取外部浏览器当前标签页（没有这种能力，也没有引入浏览器自动化依赖）；
- **不支持 MHTML**，且会识别并按内容判定（与扩展名无关）：

```python
def detect_mhtml(content: str) -> bool:
    """MHTML 是 MIME 多部分容器（RFC 2557），不是 HTML：纯文本解析器看不到
    `table#songlist`，所以必须提前识别并明确告诉用户"这个格式不支持"，
    而不是报"没有曲目"。"""
    head = (content or "").lstrip()[:400].lower()
    if head.startswith("from:") or head.startswith("mime-version:"):
        return True
    if "content-type: multipart/related" in head:
        return True
    return False
```

---

## 5. 工作 D：有依据的失败提示

`inspect_import_html` 现在按 **8 类**给出可执行的下一步，覆盖 Brief §7 列表：

| 类别 | 提示要点 |
|---|---|
| 地址与文件属于不同专辑 | 同时列出两个地址；明确"这不是「页面没有曲目」" |
| 输入是曲目详情页 | 说明填的是歌曲页；能识别则给出一键切换 |
| 保存的是人机验证页 | 说明这是校验页，请等曲目列表出现后再另存 |
| 文件格式不支持（MHTML） | 明确不支持，改扩展名无用，请重新另存为「网页，仅 HTML」 |
| 有曲目表但链接未解析出 | 指向具体原因（外站链接 / 多专辑冲突等） |
| 页面没有曲目表 / 无可识别内容 | 说明文件不像专辑页 |
| 无法确定原始来源地址 | 保留输入，请用户补充或核对 |
| 文件读取失败 | GUI 层捕获 `OSError`，单独报"读不了这个文件" |

提示里**不再**让普通用户去"运行 tools/inspect_site.py"；失败后地址框、保存目录、导入信息都保留。

---

## 6. 文件与函数变化

| 文件 | 变化 |
|---|---|
| `khdownloader_core.py` | 新增 `identify_album_source` / `album_identity` / `same_album` / `is_song_page_url` / `detect_mhtml` / `_page_title`；新增常量 `KHINSIDER_ALBUM_ROOT`、`IMPORT_KIND_UNRESOLVED`、`IMPORT_KIND_UNSUPPORTED_FORMAT`；`ImportInspection` 扩展 6 个字段（都有默认值，旧调用兼容）；`inspect_import_html` 重写为"先识别、再核对" |
| `khdownloader_gui.py` | 新增 `confirmed_import` 状态、`source_var` 来源标记、`save_guide_button`；重写 `import_album_page`（五关系处理 + 预览）；新增 `_preview_and_confirm` / `_start_confirmed_import` / `_import_worker` / `show_save_guide`；`open_in_browser` 识别歌曲页地址；`start_download` / `download_single_link` 作废导入；`_finish` / `_fatal` 复位；`set_running` 管控新按钮 |
| `tests/test_v008_import_workflow.py` | **新增（20 用例）**：识别规则、五种关系、地址规范化、干扰链接、MHTML、mock 站全链路 |
| `tests/test_v008_user_original.py` | **本轮新增（14 用例）**：用**用户原样** 2003 / 2011 页面做回归（详见 §7） |
| `tests/test_gui_smoke.py` | `_FakeMessagebox` 增加 `askyesno`（并支持连续回答序列）；新增 `TestGuiV008`（6 用例）；本轮再加 **2 个用原样 2003 文件的 GUI 用例** |
| `tests/test_v007_recovery.py` | 1 处语义更新：无 songlist、无来源的页面 → `unresolved`（v008 正确行为） |
| `tests/fixtures/user_original_2003_album_page.html` | **本轮新增**：用户原样 2003 页面（64,309 B） |
| `tests/fixtures/user_original_2011_album_page.html` | **本轮新增**：用户原样 2011 页面（93,766 B） |
| `tests/fixtures/album_page_2003_synthetic.html` | v008 上轮新增的**合成**夹具（明确标注非用户原样文件） |
| `tests/fixtures/SOURCES.md` | **本轮新增**：逐样本来源与哈希说明 |
| `tools/verify_user_original_v008.py` | **本轮新增**：对原样页面实跑场景矩阵并落盘证据 |
| `tools/verify_e2e_2003_real.py` | **本轮新增**：用户原样 2003 导入 → 首曲真实下载 + 请求轨迹 |
| `tools/verify_frozen_selftest.py` | 新增 v008 导入识别检查 + 保存指引按钮检查（12 项）；**第二次修订再加 2 项图标检查 → 14 项**（详见 §9.3） |
| **`assets/KHDownloader.ico`** | **第二次修订新增**：Strategy AI 提供的 Windows 图标（91,809 B，7 种规格） |
| **`assets/KHDownloader.png`** | **第二次修订新增**：Strategy AI 提供的原始透明 PNG（1,308,564 B，1254×1254 RGBA） |
| **`KHDownloader.spec`** | **第二次修订重写**：`name="KHDownloader"`（无尾号）+ `icon=` + `datas` 含 ico/png；缺图标时直接 `SystemExit` 拒绝构建；项目根改为从 `SPECPATH` 向上查找 |
| **`build_exe.bat`** | **第二次修订更新**：改为走 spec；打包前检查图标存在；打包后校验产物存在 |
| **`khdownloader_gui.py`（第二次修订）** | **新增 `ICON_ICO_NAME` / `ICON_PNG_NAME` / `ASSET_SUBDIR` 常量与 `resource_roots()` / `resource_path()` / `apply_window_icon()` 三个函数**，`App.__init__` 中 `title()` 之后调用（详见 §11.1）。**下载/导入逻辑零改动** |
| **`tools/verify_exe_icon.py`** | **第二次修订新增**：解析 PE 资源表，把图标组每个规格与源 ICO **逐字节**比对（详见 §9.5） |
| **`tools/check_icon_source_mode.py`** | **第二次修订新增**：源码态资源定位与窗口图标检查，含"换 cwd 后仍能找到"断言 |
| **`tools/verify_window_icon.py`** | **第二次修订新增**：`PrintWindow` 抓标题栏 + 读图标句柄 + 标题栏像素统计（详见 §11.3） |
| **`tools/verify_exe_file_icon.py`** | **第二次修订新增**：`ExtractIconExW` 取资源管理器显示的文件图标，与源 ICO 及参照旧 EXE 比对 |
| `README.md` / `TECHNICAL_DOCUMENTATION.md` / `EXECUTION_LOG.md` | 更新 v008 语义；第一次修订追加"交付目录 + 原样文件验证"记录；**第二次修订追加命名与图标记录** |
| `v008_delivery/USER_RETEST_v008.md` | 更新为无尾号 EXE；**新增场景 E（图标复测）**；删去"原样文件未验证"的旧限制（已补上） |
| `release/KHDownloader/`（第二次修订整理） | 无尾号最终交付目录：EXE + README + 完整源码（含 `assets/`）+ 文档 + 截图 + manifest |
| `release/history/KHDownloader_20261012_103739/` | **第二次修订新增**：原 `release/KHDownloader/`（v001 期）的完整备份，2/2 文件逐字节一致 |
| `stage-handoff/briefs/` | 存放本次 Brief 原文（`KHDownloader_STAGE_EXECUTION_BRIEF_v008.md`，含 §10.1） |

---

## 7. 逐场景证据（本轮亲验 vs 历史结果分列）

### 7.1 样本来源与哈希

| 样本 | 来源 | 大小 | SHA-256 |
|---|---|---|---|
| `user_original_2003_album_page.html` | **用户原样**：`D:\<用户名>\Desktop\temp\temp\3\…(2003)….html` | 64,309 B | `7e1911885c318cb71b00ba84adc70dd571b1390ab5a1d9513b05f3ca862d8e16` |
| `user_original_2011_album_page.html` | **用户原样**：`D:\<用户名>\Desktop\temp\temp\…(2011)….html` | 93,766 B | `89fb458779a71b7a1cb92fc1a64842ebf11e2cb4de9e599266ef15c7e2558dd5` |
| `album_page_2003_synthetic.html` | **合成**（按 Strategy AI 审查数据结构对齐） | 15,155 B | 见 manifest |
| `real_album_page.html` | v003 期采集的 2011 真实页（历史样本，非本次用户原样文件） | 61,654 B | `1e460a83…`（前 8 字节） |

**原样文件定位过程**：上一轮全盘检索未找到；本轮从 Windows「最近使用」快捷方式的原始字节中解析出目标路径，
在 `D:\<用户名>\Desktop\temp\temp\3\` 找到该文件（64,309 B），已按字节原样复制为回归夹具（未裁剪、未脱敏、未改写）。

**脱敏扫描结果（原样 2003 页）**：`cf_clearance` / `__cf_bm` / `set-cookie` / `Authorization` /
`Bearer` / `password` / 常见邮箱域名 —— **零命中**；不含本地绝对路径；无 Cookie、无凭据。
（该页 `head` 内出现的 `jquery.cookie` 是站点自带的库文件名，不含任何 Cookie 值。）

### 7.2 用户原样 2003 HTML —— Brief §9 第 1–4 行（本轮亲验，`tools/verify_user_original_v008.py`）

| # | 输入 | 输出 | 识别曲目数 | 首曲 / 末曲 | 结论 |
|---|---|---|---|---|---|
| 1 | 原样 2003 + **正确地址** | `kind=album`，`relation=match` | **19** | `Airwaves Arr.ver.` / `_and then_` | ✅ 与 Brief §2 完全一致 |
| 2 | 原样 2003 + **2011 地址** | `kind=no_tracks`，`relation=different_album`，提示同时列出两个地址 | 0（按错地址不解析） | — | ✅ **不再报"没有解析到任何曲目链接"**；取消则不下载 |
| 3 | 原样 2003 + **空地址** | `kind=album`，`relation=empty`，`detected_url` = 2003 专辑地址 | **19** | 同上 | ✅ 地址可空、自动识别 |
| 4 | 原样 2003 + **首曲详情页地址** | `kind=no_tracks`，`relation=song_page`，提示"这是曲目详情页"+ 给出专辑地址 | 0 | — | ✅ 提示准确、可一键恢复 |
| 5 | 原样 2003 + 正确地址 **+ 查询参数** | `relation=match` | 19 | 同上 | ✅ 规范化正确 |
| 6 | 原样 2003 + 正确地址 **+ 末尾斜杠** | `relation=match` | 19 | 同上 | ✅ |
| 7 | 原样 2003 + 正确地址 **+ 片段 `#songlist`** | `relation=match` | 19 | 同上 | ✅ |
| 8 | 原样 2003 + `HTTPS://www.Downloads.…`（大小写 / www） | `relation=match` | 19 | 同上 | ✅ |
| 9 | 原样 2003 页内**推荐 / 相关专辑链接** | 识别结果不受影响 | 19 | 同上 | ✅ 不采集无关曲目 |

页面结构实测：`table#songlist` 存在；页面内 `/album/...mp3` 形式的链接共 **95 个**，
去重后唯一曲目详情页 **19 个**，且 **95 个全部属于同一个 slug**（`cross-channel-soundtrack-arrange-version-criss-cross-2003`）
—— 这正是"众数即唯一来源"的实证。

### 7.3 用户原样 2011 HTML（本轮亲验）

| 输入 | 输出 | 识别曲目数 | 首曲 / 末曲 |
|---|---|---|---|
| 原样 2011 + 正确地址 | `album` / `match` | **30** | `群青のティファレス(GAME Size)` / `時間の雲(FULL Size)` |
| 原样 2011 + 2003 地址 | `no_tracks` / `different_album` | 0 | — |
| 原样 2011 + 空地址 | `album` / `empty` | 30 | 同上 |
| 两份原样文件互配错地址 | 双向都 `different_album`，互不混淆 | — | — |

页面内链接 **150 个** → 去重 **30** 个唯一曲目，全部同一 slug。

### 7.4 GUI 层：冲突确认与取消（本轮亲验，`TestGuiV008`）

| 场景 | 断言 | 结果 |
|---|---|---|
| 原样 2003 + 地址框留 2011 + 用户选「**否**」 | 弹框文案含"不是同一张专辑"且**同时含两个地址**、不含"没有解析到任何曲目链接"；地址框**保持 2011 不变**；`confirmed_import` 为 None；`session` 为 None（**没建网络会话**）；目录里除导入的 HTML 外**没有新文件** | ✅ |
| 原样 2003 + 地址框留 2011 + 用户选「是」→ 预览选「否」 | 出现**两次**询问（冲突确认 + 曲目预览）；预览文案含 **19**、`Airwaves Arr.ver.`、`_and then_`、2003 专辑地址；地址框被**显式回填**为 2003 地址；随后**不下载**、不建会话、无新文件 | ✅ |
| 预览后取消（合成样本，mock 站） | 不创建会话、不访问详情页、目录里只剩导入的 HTML | ✅ |
| 开始已确认任务 | worker 只吃 `confirmed["tracks"]`；来源标记 = `来源：已导入网页（…）` | ✅ |
| 确认导入后切"开始下载" | 导入任务作废、标记复位为 `直接请求` | ✅ |

### 7.5 真实网络：原样 2003 导入 → 首曲落盘（本轮亲验，`tools/verify_e2e_2003_real.py`）

```
样本： user_original_2003_album_page.html 64309 bytes（用户原样保存页面）
识别： kind=album  relation=match  曲目数=19
首曲： Airwaves Arr.ver. | 末曲： _and then_
== 第 1 首：Airwaves Arr.ver. ==
  详情页 200，直链主机： nu.vgmtreasurechest.com
  结果：status=completed  写入=4173657 bytes  跳过=False
  文件：Airwaves Arr.ver.mp3  大小=4173657  头=b'ID3'
  SHA-256： e36d8d99831723f86815d51fdd7bc55ff3b8af62d515719812a86a5b25328a42
== 请求轨迹（证明没有重复请求专辑页）==
  - https://downloads.khinsider.com/game-soundtracks/album/cross-channel-...-2003/01.%2520Airwaves%2520Arr.ver..mp3
  - https://nu.vgmtreasurechest.com/soundtracks/cross-channel-...-2003/blbxkijv/01.%20Airwaves%20Arr.ver..mp3
  专辑页请求次数： 0 （预期 0：下载只用预览确认的曲目）
```

- 首曲真实下载成功：**4,173,657 字节**，文件头 `ID3`；
- **下载过程中对专辑页的请求次数 = 0** —— 直接证实 Brief §9 最后一行"开始已确认的导入任务：使用预览曲目，不再请求专辑页"；
- **交叉印证**：用户自己此前用浏览器/工具保存的 `C:\Users\<用户名>\Downloads\KHInsider\Airwaves Arr.ver.mp3`
  大小与本轮管线下载结果**完全相同**（4,173,657 字节），SHA-256 也**完全相同**
  （`e36d8d99831723f86815d51fdd7bc55ff3b8af62d515719812a86a5b25328a42`）；
- 临时目录中的产物在验证完成后**已删除**，未触碰用户任何目录。

### 7.6 真实网络：2011 导入任务 → 首曲落盘（**历史结果**，v008 上一轮，标注为历史证据）

```
导入识别: 30 首；首曲: 群青のティファレス(GAME Size)
首曲直链主机: lambda.vgmtreasurechest.com
status: completed | 完成（4.70 MB）-> 01 first track.mp3
大小: 4930262 | 头: b'ID3' | SHA-256: 3d1bc76d14ee2d62f438161267424238308b7c0e8a3abd216eaeaa9f70ed2c58
```

### 7.7 Brief §9 场景表逐行覆盖

| 输入 / 操作 | 覆盖测试 | 结果 |
|---|---|---|
| 用户原样 2003 + 正确地址：19 首、首/末曲、顺序 | `test_v008_user_original.TestUserOriginal2003.test_correct_address_all_19_tracks_in_order` | ✅ |
| 同 HTML + 2011 地址：提示不匹配，确认切换后 19 首；取消不下载 | core `test_wrong_2011_address_reports_conflict_not_no_tracks` + GUI 两个原样文件用例 | ✅ |
| 同 HTML + 空地址：识别来源，确认后预览 19 首 | `test_empty_address_identifies_source` + GUI `empty` 分支 | ✅ |
| 同 HTML + 首曲详情页：提示并可恢复 | `test_first_song_detail_page_address` | ✅ |
| 2011 样本：30 首、名称/编码正确 | `TestUserOriginal2011.test_30_tracks_names_and_encoding`（原样） + `test_2011_sample_still_30_tracks`（历史样本） | ✅ |
| 查询参数 / 片段 / 末尾斜杠 | `test_address_normalization_variants_still_match`、`test_www_and_scheme_case_normalization`、`TestAlbumIdentity`（5 例） | ✅ |
| 推荐专辑 / 混合来源 / 外站链接不误识别 | `test_related_links_do_not_change_identity`、`test_related_albums_not_collected`、`test_external_mp3_links_not_used_for_identity` | ✅ |
| 验证页 / MHTML / 缺曲目结构分类正确 | `test_mhtml_detected_and_rejected`、challenge 用例、`test_songlist_but_unresolvable_source` | ✅ |
| 预览后取消：不建会话、不访问详情页、不写音频 | GUI `test_preview_declined_does_not_download_or_touch_network` | ✅ |
| 预览后改地址 / 导入另一文件 / 切模式 | GUI `test_switch_to_direct_mode_invalidates_import` | ✅ |
| 开始已确认任务：用预览曲目、不再请求专辑页 | GUI `test_preview_confirmed_uses_preview_tracks` + 真实网络请求轨迹（专辑页 0 次） | ✅ |
| 后续取消 / 失败 / 已有文件保持原有保护 | v007 全部既有用例回归通过 | ✅ |

### 7.8 未完成项（如实列出）

- **用户本人尚未在 GUI 里手动点过一遍**（异常对话框、预览窗口、保存指引弹窗都是自动化替身断言 +
  原样文件实跑，不是人手点击）。这也是最终裁决没有升到
  `Import Workflow Fix Verified` 的唯一原因。
- 2003 专辑**整张 19 首**未整批下载（Brief §9 明确不要求反复整张下载），只验证了首曲；
  其余 18 首的详情页/音频主机可访问性未逐首确认。
- 历史样本 `real_album_page.html` 与用户本轮原样 2011 文件是**两次不同的抓取**（61,654 B vs 93,766 B），
  两者都解析出 30 首，但页面结构可能略有差异。
- **图标检查未覆盖用户机器上的 Windows 图标缓存**（§11.3 末）。三层验证都是执行环境内
  用 Win32 API 取到的客观数据；用户若在原目录看到旧图标，需复制到别处再看，
  或重启资源管理器。本轮未删除任何系统缓存、未改注册表。
- **未验证任务栏上悬停/分组显示时的图标外观**。已验证的是窗口图标句柄
  （`GCLP_HICON` / `GCLP_HICONSM` 非零）与标题栏实际像素；任务栏与标题栏用的是同一份
  句柄，但没有单独截任务栏区域的像素。

---

## 8. 测试：命令、环境、结果

| 项 | 值 |
|---|---|
| 命令 | `python -m unittest discover -s tests -p "test_*.py"` |
| 运行目录 | 项目根 `D:\WorkSpace\agent\khdownloader`（另在交付副本 `release/KHDownloader/source` 各跑一遍） |
| 解释器 | 隔离 venv `.v007_work/buildenv`（Python 3.11.9）——不含无关工具依赖 |
| 依赖版本 | requests 2.34.2 / beautifulsoup4 4.15.0 / Tk 8.6 |
| 平台 | Windows 10（10.0.19045），64-bit AMD64 |
| **结果** | **167 / 167 通过，0 失败，0 跳过** |
| 退出码 | **0** |
| 用时 | 55.43 s（项目根，第二次修订后）/ 53.84 s（交付副本 `release/KHDownloader/source`） |
| 基线（改动前） | 125 / 125（52.690 s） |
| 图标改动后的定向复跑 | `python -m unittest tests.test_v008_import_workflow tests.test_gui_smoke` → **52 / 52 通过** |

用例增长：125（v007 基线）→ 151（v008 上轮：+20 core +6 GUI）→ **167**（第一次修订：+14 原样文件 core +2 原样文件 GUI）。

**第二次修订没有新增用例，但改了 `khdownloader_gui.py`**，因此重新跑了全量：
167 仍全部通过；图标本身的验证放在 `tools/` 下的四个专项脚本里
（源码态、PE 资源表、窗口标题栏、资源管理器取图），
以及 `tools/verify_frozen_selftest.py` 新增的 2 项冻结态检查 → **14 / 14**。

```
Ran 167 tests in 55.432s

OK
EXIT=0
```

**关键失败信息**：无失败。仅有两处**非致命**噪声（历史遗留，不影响结果）：
Tk 在解释器退出时打印 `RuntimeError: main thread is not in main loop`
（`Variable.__del__` 触发，是共用 Tk 根窗口的测试收尾噪声，退出码仍为 0）。

**本轮修过的测试问题**（代码侧无改动，均为测试自身写错）：

- `insp.matched_address` 是布尔字段（"地址是否相符"），我最初按"URL 字符串"断言 → 改为断言布尔 +
  用 `insp.detected_url` 校验识别地址。

---

## 9. EXE 构建、冻结与干净目录检查（第二次修订后已重建）

### 9.1 程序与源码属于同一版本（Brief §11 要求 1）

| 文件 | 修改时间 | 大小 | SHA-256 |
|---|---|---|---|
| `khdownloader_core.py`（本轮**未改**） | 2026-10-12 00:12 | 66,223 B | `bd1fb04b4ff71a93408d253fb999a759fb76ba388cbf3126477ebb35b25df530` |
| `khdownloader_gui.py`（本轮新增资源定位 + 窗口图标） | 2026-10-12 10:35 | 49,055 B | `9f8b617056687fafcb402bfd9a8770cefad08cb9591af8721883dc12ab5664c5` |
| `dist/KHDownloader.exe`（**重建于 2026-10-12 10:38**） | 2026-10-12 10:38 | **14,928,399 B** | `a93cbe07f5ef2e36eabba1ea94ab5a61e3b524e0c3735c9d145b2416e24a8304` |

`khdownloader_gui.py` 的修改时间**早于** EXE 构建时间，且构建走的就是当前工作区源码
（spec 里 `pathex` 指向项目根），因此**交付程序与源码同属一个版本**。

> 旧产物 `dist/KHDownloader_v008.exe`（13,754,884 B，
> `933b093c24d122ad53ffc3d8e90ee586591cf15536791b0dbddd3676dd161cc5`）与
> `dist/KHDownloader_v007.exe` **都保留原样、未改名、未删除**。
> 本轮**没有**把其中任何一个改名充当新程序：新 EXE 是重新构建的，
> 大小与哈希都和两者不同（§9.5 有 PE 层面的图标证据）。

### 9.2 构建（第二次修订：走 spec）

```
:: 主程序 —— 走 KHDownloader.spec，程序名与图标都由 spec 决定
.v007_work\buildenv\Scripts\python.exe -X utf8 -m PyInstaller ^
    --noconfirm --clean KHDownloader.spec

:: 冻结自检 —— 走 .v008_work\pyi_selftest_spec\_frozen_selftest_v008.spec
.v007_work\buildenv\Scripts\python.exe -X utf8 -m PyInstaller ^
    --noconfirm --clean .v008_work\pyi_selftest_spec\_frozen_selftest_v008.spec
```

两次构建均 `Build complete!`（退出码 0）。构建日志含 `Copying icon to EXE`（证明图标确实进了 EXE）。

**依赖版本**：Python 3.11.9 (64bit)、PyInstaller 6.22.3、pyinstaller-hooks-contrib 2026.8、
requests 2.34.2、beautifulsoup4 4.15.0、soupsieve 3.0.1。
构建环境是隔离 venv `.v007_work\buildenv`（不污染系统 Python）。

| 产物 | 大小（bytes） | SHA-256 |
|---|---|---|
| `dist/KHDownloader.exe` | **14,928,399** | `a93cbe07f5ef2e36eabba1ea94ab5a61e3b524e0c3735c9d145b2416e24a8304` |
| `dist/_frozen_selftest_v008.exe` | 14,940,811 | `2607e7760342926e460eed753ac4b3395856163ab17013bc1cd266951f143451` |

**为什么改走 spec（Brief §10.1 第 5 点）**：命令行上的 `--name` / `--icon` **不会**覆盖
spec 里的字段。如果脚本用命令行参数、spec 用另一套值，就会出现两个不同的最终程序名。
所以 `build_exe.bat` 现在也调用 `pyinstaller KHDownloader.spec`，
程序名、图标、datas 三者只有一个来源。

### 9.3 冻结自检（退出码 0，**14 / 14 通过**）

```
[OK]   requests: 2.34.2
[OK]   bs4: 4.15.0
[OK]   bs4 BeautifulSoup: True
[OK]   import khdownloader_core: khdownloader_core
[OK]   core.parse_album_page: 解析出 1 首：A
[OK]   core v007 恢复能力: 错误分类 / 导入分类 / 单曲入口 / 脱敏 均可用
[OK]   core v008 导入识别: 来源识别 / 地址核对 / MHTML / 歌曲页判定 均可用
[OK]   import khdownloader_gui: khdownloader_gui
[OK]   GUI 控件实例化: 按钮 = ['开始下载', '取消', '选择文件夹…', '导入专辑页面…',
                                '下载单个链接…', '保存网页指引']
[OK]   图标运行资源可定位: roots=['…\\AppData\\Local\\Temp\\_MEI0000a0802',
                                  '…\\AppData\\Local\\Temp\\_MEI0000a0802']
                             | ico=91809B | png=1308564B
[OK]   窗口图标已设置: 生效方式 = ico:…\Temp\\_MEI0000a0802\assets\KHDownloader.ico
通过 14 / 14
全部通过。
```

自检脚本本次新增两项（`tools/verify_frozen_selftest.py` 第 5 节），
**这两项在冻结态才有意义**：源码运行一定能找到 `assets/`，冻结态必须靠 `sys._MEIPASS` 才能找到。
其中 roots 打印出的 `…\_MEI0000a0802` 就是 onefile 解包目录 —— 说明
资源定位走的是**冻结后的模块资源位置**，不是 cwd、不是原项目目录。

自检 EXE 的 spec **也带了同一份 icon 与 datas**：如果自检包里没有 assets，
"冻结态资源定位"这项检查就永远失败或永远假通过，两种都没意义。

### 9.4 从交付目录与干净目录启动 EXE（本轮亲验）

| 检查 | 方法 | 结果 |
|---|---|---|
| 从**交付目录**启动 | 运行 `release/KHDownloader/KHDownloader.exe`，`cwd` = 交付目录 | ✅ 窗口标题 `KHDownloader —— KHInsider MP3 下载器` 正常，进程存活 |
| 复制到**干净临时目录**启动 | 只把 EXE 放进 `.v008_work\final_clean_launch\`（目录里**只有这一个文件**，无 `source/`、无 `assets/`），`PATH` 仅 `C:\Windows\System32;C:\Windows` | ✅ 正常启动；窗口图标**同样生效**（见 §11.3） |
| 导入 / 预览 / 取消未回归 | 图标改动只碰 `apply_window_icon()` 与资源定位，未触及导入与下载逻辑；`tests.test_v008_import_workflow` + `tests.test_gui_smoke` | ✅ **52 / 52 通过** |

**本项目不做的事**：本轮不声明 Cloudflare 直连问题已消失，也不声明所有专辑均可下载或整个项目已发布就绪。

### 9.5 图标真的嵌进 EXE 了吗 —— 解析 PE 资源表逐字节核对

`tools/verify_exe_icon.py`（本次新增）**直接解析 EXE 的 PE 资源目录**，
把图标组（`RT_GROUP_ICON`）里每个尺寸的图像数据取出来，与
`assets/KHDownloader.ico` **逐字节比对**。这样"改了文件名"这种做法就不可能冒充完成：

```
资源表 RVA=0x51000  大小=93,620 B
资源类型：[3, 14, 24]   （3=RT_ICON, 14=RT_GROUP_ICON）

[1] 图标组内含 7 个规格：
      16x16  32bpp    855 B  resource_id=1
      24x24  32bpp  1,454 B  resource_id=2
      32x32  32bpp  2,258 B  resource_id=3
      48x48  32bpp  4,030 B  resource_id=4
      64x64  32bpp  6,200 B  resource_id=5
      128x128 32bpp 18,338 B  resource_id=6
      256x256 32bpp 58,556 B  resource_id=7

[2] 逐规格比对（EXE RT_ICON 数据 vs 源 assets/KHDownloader.ico）：
      16x16:   一致    855 B  sha256 bba769bf20238064…
      24x24:   一致  1,454 B  sha256 a680abe1c96a0ac3…
      32x32:   一致  2,258 B  sha256 584ee66d2ec8ce24…
      48x48:   一致  4,030 B  sha256 a49e6b25a9ed9e66…
      64x64:   一致  6,200 B  sha256 68fdd0560b7025e9…
      128x128: 一致 18,338 B  sha256 12e5bf005c75603d…
      256x256: 一致 58,556 B  sha256 fffa00961f662914…
      规格齐全：是     逐字节匹配：7/7

[3] 运行资源（窗口图标在运行时读它）：
      assets/KHDownloader.ico  在包内（PyInstaller CArchiveReader）
      assets/KHDownloader.png  在包内（PyInstaller CArchiveReader）
      包内条目总数：1006

[结论] 通过：图标组 7 个规格全部逐字节匹配 assets/KHDownloader.ico，
       且 ico/png 两份运行资源都已打进 onefile 包。
```

> 这段脚本本身也踩过两个坑，都已在代码注释里写明原因：
> ① PyInstaller 默认产出 **PE32+**，data directory 起点是 optional header + 112 而不是 + 96；
> ② 资源树是 `TYPE → NAME → LANGUAGE` 三层，键必须取**中间层 NAME**，
> 取 `LANGUAGE` 会导致"图像数据缺失"的假阴性。

### 9.6 图标规格与哈希（Brief §10.1 第 10 点要求）

| 文件 | 大小（bytes） | SHA-256 | 说明 |
|---|---|---|---|
| `assets/KHDownloader.ico` | 91,809 | `e86970e036a4c626d6a8a7ed60866d4047b5dfd05819138389e2234504ae3e7d` | 由 Strategy AI 随 Brief 提供，未修改 |
| `assets/KHDownloader.png` | 1,308,564 | `fd5d4c42a32ff16e29f78b80e5aedc9651cfe2d42d5d2e4592ffbf16a5304ddc` | 同上 |

ICO 头解析（`count = 7`）：

| # | 尺寸 | bpp | 字节 |
|---|---|---|---|
| 0 | 16×16 | 32 | 855 |
| 1 | 24×24 | 32 | 1,454 |
| 2 | 32×32 | 32 | 2,258 |
| 3 | 48×48 | 32 | 4,030 |
| 4 | 64×64 | 32 | 6,200 |
| 5 | 128×128 | 32 | 18,338 |
| 6 | 256×256 | 32 | 58,556 |

Brief 要求的 16 / 24 / 32 / 48 / 64 / 128 / 256 **七种规格齐全**，全部 32bpp，数据块无越界。
PNG 为 **1254×1254、8bit RGBA（colortype 6，带透明通道）**。

---

## 10. 集中交付文件夹（Brief §11 + §10.1）

### 10.1 真实完整路径

```
D:\WorkSpace\agent\khdownloader\release\KHDownloader\
```

**这是 Brief §10.1 指定的最终交付根目录，无阶段尾号。**
第一次修订时的 `release/KHDownloader_v008/` 目录**原样保留**（含当时交付的
`KHDownloader_v008.exe` 与 `README.md`），作为历史归档，**不在当前入口列表中**。

### 10.2 原有 `release/KHDownloader/` 的备份（Brief §10.1 要求 7）

该目录在本轮之前**已存在**，里面是更早期（v001 期）的两个文件。整理新交付前先完整备份：

```
D:\WorkSpace\agent\khdownloader\release\history\KHDownloader_20261012_103739\
```

逐字节核对结果：

| 文件 | 大小（bytes） | SHA-256 | 与备份是否一致 |
|---|---|---|---|
| `KHDownloader.exe` | 21,809,102 | `dbfa426febbf6f0a1ee13b12cfb3e826316db601c413765e58b8443716c2db16` | ✅ 一致 |
| `README.md` | 5,813 | `0193bdffdbacb71a2c38cb93102602a2ada8a10723898a8a8b507532847cd0e2` | ✅ 一致 |

**文件数 2 / 2，全部一致，无遗漏。** 确认备份完整后才移除原位置同名文件，
再写入本轮新交付内容 —— 没有任何内容被静默丢弃。

### 10.3 目录清单

```
release/KHDownloader/
  KHDownloader.exe                          ← 双击运行这个（14,928,399 B，无尾号）
  README.md                                  ← 面向用户的使用说明（以本目录为基准）
  STAGE_EXECUTION_REPORT_v008.md ← 本报告副本
  _MANIFEST.json                             ← 逐文件大小 + SHA-256
  source/                                    ← 完整可复现源码
    khdownloader_core.py
    khdownloader_gui.py
    requirements.txt
    KHDownloader.spec                        ← 程序名 / 图标 / datas 都在这里
    build_exe.bat                            ← 一键打包（走 spec，产物进 source/dist/）
    assets/
      KHDownloader.ico                       ← Windows 图标（7 种规格）
      KHDownloader.png                       ← 原始透明 PNG（Tk 兜底 / 后续维护）
      README.md                              ← 这两个文件谁读它们
    tests/  (6 个 .py + fixtures/ 12 个样本 + SOURCES.md)
    tools/  (9 个诊断/验证脚本，本轮新增图标相关工具)
  docs/
    USER_RETEST_v008.md                      ← 含新增的场景 E（图标复测）
    REQUIREMENTS.md
    TECHNICAL_DOCUMENTATION.md
    EXECUTION_LOG.md
    screenshots/
```

### 10.4 交付的 EXE 与构建产物哈希一致（Brief §11 要求 1）

逐字节比对：`dist/KHDownloader.exe` 与 `release/KHDownloader/KHDownloader.exe`
大小均为 **14,928,399 字节**，SHA-256 均为
`a93cbe07f5ef2e36eabba1ea94ab5a61e3b524e0c3735c9d145b2416e24a8304`。**完全一致。**

这个 EXE 是**本轮重新构建**的产物，不是任何旧 EXE 的改名（§9.1 / §9.5 有证据）。

### 10.5 从交付副本做的完整性检查（Brief §11 要求 7）

在 `release/KHDownloader/source/` 下运行：

```
python -m unittest discover -s tests -p "test_*.py"
→ Ran 167 tests in 53.841s
→ OK
→ EXIT=0
```

**167 / 167 通过、0 失败、0 跳过、退出码 0。**
说明 `source/` 副本**没有遗漏模块、样本或路径**（缺任何夹具都会直接报错，不会静默通过），
新增的 `assets/` 两份资源也在其中。交付副本自带 `tools/verify_user_original_v008.py`
与 `tools/verify_e2e_2003_real.py`，两者已改为**优先从 `tests/fixtures/` 读原样样本**，
在交付目录内可直接复跑，不依赖本机桌面绝对路径。
跑测试产生的 `__pycache__` 已在生成 manifest 前清理，交付目录内**不含**缓存文件。

### 10.6 README 要点（Brief §11 要求 4 逐条对照）

| 要求 | README 中的位置 |
|---|---|
| 双击哪个 EXE | §2 —— 明确写"就这一个，文件名不带阶段编号" |
| EXE 运行无需单独安装 Python | §1、§2 |
| 保存目录 | §3（默认 `%USERPROFILE%\Downloads\KHInsider`，可改） |
| 直接输入 URL 与导入 HTML 两条路径 | §3 路线一 / 路线二 |
| 地址冲突确认、曲目预览、保存网页指引 | §3 路线二第 4 步（五种情况逐条列出） |
| MHTML 支持状态 | §3 第 2 步 + §11（明确不支持、改扩展名无用） |
| 取消与文件保护 | §3、§4（`.part` 原子改名、已完成文件不动） |
| Cloudflare 现有边界 | §7（不绕过；改用导入网页路径） |
| 未签名程序说明 | §6（SmartScreen 提示与处理） |
| **应用图标**（§10.1 第 6 点） | §5（文件图标 + 窗口图标，并说明图标缓存不是程序问题） |
| 源码位置及源码运行/测试/构建命令 | §8、§9 |
| 相对路径以交付目录为基准 | 全文用 `source/…`、`docs/…`，未出现本机绝对路径 |

### 10.7 交付目录的排除项检查（Brief §11 要求 6）

已确认交付目录中**不含**：Cookie、浏览器配置、原始 HAR、代理凭据、真实音乐文件、
`.part`、venv、`site-packages`、构建缓存（`__pycache__` / `build/`）、完整 `.git`。
测试样本按来源标注（`source/tests/fixtures/SOURCES.md` 明确区分"用户原样"与"合成"），
**没有**把合成样本说成用户原文件。

目录内大于 3 MB 的文件只有 EXE 本身（约 14.2 MB）与 PNG 图标（约 1.2 MB）；
两者都是程序必需资源，不含任何凭据。

### 10.8 原有产物保留（Brief §11 要求 3、§10.1 要求 7）

集中交付全部采用**复制**方式；原项目源码、`dist/` 内历史产物（v007 / v008 尾号 EXE 均在）、
`stage-handoff/` 历史报告、`release/KHDownloader_v008/`（第一次修订的交付目录）
以及 `release/history/KHDownloader_20261012_103739/`（v001 期内容备份）均**原样保留**。
旧审查 ZIP `v008_delivery/KHDownloader_REVIEW_BUNDLE_v008.zip` 作为历史材料保留，本轮不再重新制作（§11.1）。

---

## 11. 新增：窗口图标与资源路径的实现（Brief §10.1 第 3、4、5 点）

### 11.1 `khdownloader_gui.py` 的实际代码

新增三个常量与三个函数。**资源定位刻意不依赖当前工作目录** ——
双击 EXE 时 cwd 可能是任意目录，用 cwd 找资源是最容易在别人机器上失效的做法：

```python
ICON_ICO_NAME = "KHDownloader.ico"
ICON_PNG_NAME = "KHDownloader.png"
ASSET_SUBDIR = "assets"


def resource_roots() -> list[Path]:
    """列出可能存放资源文件的根目录，按优先级从高到低。

    - 源码运行：资源在 ``<项目目录>/assets/``
    - PyInstaller onefile 冻结：资源被解包到 ``sys._MEIPASS/assets/``
    """
    roots: list[Path] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))

    try:
        roots.append(Path(__file__).resolve().parent)
    except (OSError, NameError):
        pass
    ...


def resource_path(filename: str) -> Path | None:
    """在资源根目录里找 ``filename``，找到返回路径，找不到返回 ``None``。

    不抛异常：图标缺失不该让整个程序起不来。
    """
    for root in resource_roots():
        for candidate in (
            root / ASSET_SUBDIR / filename,   # 源码运行 / 冻结运行的主路径
            root / filename,                  # 兼容直接把图标放在根目录的旧布局
        ):
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
    return None


def apply_window_icon(root: tk.Tk) -> str:
    """给窗口设置图标，并返回实际生效的方式。

    优先用 ICO（Windows 标题栏 + 任务栏 + 资源管理器都用这一份）；
    失败再退到 PNG（仍能设标题栏图标，但任务栏表现可能不如 ICO）；
    都没有则返回 ``"none"``，**不影响程序其余功能**。
    """
    ico = resource_path(ICON_ICO_NAME)
    if ico is not None:
        try:
            root.iconbitmap(default=str(ico))
            return f"ico:{ico}"
        except tk.TclError:
            pass

    png = resource_path(ICON_PNG_NAME)
    if png is not None:
        try:
            img = tk.PhotoImage(file=str(png))
            root.iconphoto(True, img)
            # 必须留住引用，否则 PhotoImage 会被垃圾回收，图标会突然变回默认
            root._khdownloader_icon_img = img
            return f"png:{png}"
        except Exception:
            # PhotoImage 只保证带 Tk 的构建支持 PNG；不支持就当没有图标
            pass

    return "none"
```

在 `App.__init__` 里紧跟 `title()` 之后调用：

```python
def __init__(self, root: tk.Tk):
    self.root = root
    self.root.title("KHDownloader —— KHInsider MP3 下载器")
    self.icon_mode = apply_window_icon(root)
```

**图标缺失不会让程序打不开**：`apply_window_icon` 返回 `"none"` 时程序照常运行，
只是用系统默认图标。这是有意的降级，不是静默失败。

源码态实测（`tools/check_icon_source_mode.py`，切到 `HOME` 后仍能找到资源，证明不依赖 cwd）：

```
cwd            : D:\WorkSpace\agent\khdownloader
resource_roots : ['D:\\WorkSpace\\agent\\khdownloader']
ico  -> D:\WorkSpace\agent\khdownloader\assets\KHDownloader.ico 91809
png  -> D:\WorkSpace\agent\khdownloader\assets\KHDownloader.png 1308564
apply_window_icon -> ico:D:\WorkSpace\agent\khdownloader\assets\KHDownloader.ico
切到 HOME 后 ico 仍可定位 -> D:\WorkSpace\agent\khdownloader\assets\KHDownloader.ico
OK：源码态图标检查通过
```

### 11.2 spec 与构建脚本的变更（Brief §10.1 第 1、2、5 点）

`KHDownloader.spec`（项目根，同时复制进交付 `source/`）三处关键字段：

```python
a = Analysis(
    [str(GUI_ENTRY)],
    pathex=[str(PROJECT_ROOT)],
    datas=[
        # (源文件, 包内目标目录)。目标目录必须和 khdownloader_gui.ASSET_SUBDIR 一致。
        (str(ASSETS / "KHDownloader.ico"), "assets"),
        (str(ASSETS / "KHDownloader.png"), "assets"),
    ],
    ...
)
exe = EXE(
    ...,
    name="KHDownloader",                      # ← 无阶段尾号
    console=False,
    icon=str(ASSETS / "KHDownloader.ico"),    # ← 编入 PE 资源表
)
```

spec 还做了一件事：**缺图标就拒绝构建**。

```python
for _icon_name in ("KHDownloader.ico", "KHDownloader.png"):
    if not (ASSETS / _icon_name).is_file():
        raise SystemExit(
            f"找不到图标资源：{ASSETS / _icon_name}\n"
            "没有真实图标文件时不允许用旧默认图标冒充完成，"
            "请让用户补充 Strategy AI 提供的 assets/KHDownloader.ico 与 .png。"
        )
```

`build_exe.bat` 同步：打包步骤改为 `python -m PyInstaller --noconfirm --clean KHDownloader.spec`，
打包前检查两个图标文件是否存在，打包后校验 `dist\KHDownloader.exe` 真的产出了。
脚本与 spec **不会**产生两个不同的最终程序名。
自检 spec 也带同一份 icon 与 datas —— 否则自检的"冻结态资源定位"检查失去意义。

spec 里的项目根是**从 `SPECPATH` 向上查找 `khdownloader_gui.py` 定位**的，不假设 spec 一定在项目根：
第一次尝试用 `SPECPATH.parent` 直接退到上一级，导致入口脚本找不到、构建退出码 1；
改成向上遍历后才正常。

### 11.3 图标显示的实测证据（Brief §10.1 第 8 点）

Brief 要求"记录截图或明确实际未验证项"，并且**不能用"图标文件存在"代替嵌入成功证据**。
本轮做了三层互相独立的检查：

**第一层 —— EXE 资源表**：见 §9.5，PE 解析，7/7 逐字节匹配。

**第二层 —— 窗口标题栏与任务栏**：从交付目录启动 EXE 后，
用 `PrintWindow(PW_RENDERFULLCONTENT)` 抓取非客户区并读图标句柄（`tools/verify_window_icon.py`）：

```
WM_GETICON ICON_BIG / ICON_SMALL / ICON_SMALL2 = 0 / 0 / 0
GetClassLongPtr GCLP_HICON                       = 168827789
GetClassLongPtr GCLP_HICONSM                     = 50196
PrintWindow(PW_RENDERFULLCONTENT) = 1
```

> 注意 `WM_GETICON` 全是 0，**但这不代表没有图标**：Tk 的 `wm iconbitmap`
> 走的是 `SetClassLongPtr`（**类图标**）这条路，只查 `WM_GETICON` 会误报"没有图标"。
> 正确查法是 `GetClassLongPtrW(hwnd, GCLP_HICON)`，如上，非零。

标题栏左上角 200×31 区域的像素分布（`#`=蓝、`.`=白、`x`=深）：

```
 0 |xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx|
 7 |xxxxxxxx..........,##########,..............|
 8 |xxxxxxxx.........##############.............|
 9 |xxxxxxxx........################............|
10 |xxxxxxxx........################....,#..,,#,|
11 |xxxxxxxx........######,#########....?#..?##,|
12 |xxxxxxxx........################....?#.?x#.,|
13 |xxxxxxxx........################....?#?x#..,|
14 |xxxxxxxx........################....??x#...,|
15 |xxxxxxxx........###########,####....?xx#...,|
16 |xxxxxxxx........##########,.####....?#?##..,|
17 |xxxxxxxx........####,#####,.####....?#,x#,.,|
18 |xxxxxxxx........##,..###########....?#.?x#.,|
19 |xxxxxxxx........###,############....?#..?x#,|
20 |xxxxxxxx........################............|
21 |xxxxxxxx........,##############,............|
22 |xxxxxxxx.........,############,.............|

蓝色像素 251   白色像素 777
蓝色范围 x 16..42   y 7..22（16×16）
蓝色填充率 58.1%   方块内白像素占比约 75.6%
```

清晰可辨：**一个 16×16 的蓝色圆角方块，内部有白色内容**（音符与下载箭头），
右侧 `x≈32..42` 是标题文字开头。这与提供的图标设计一致。

标题栏裁剪图随交付：`release/KHDownloader/docs/screenshots/titlebar_icon_delivery.png`。

**第三层 —— 资源管理器显示的文件图标**：`tools/verify_exe_file_icon.py`
用 `ExtractIconExW` 取 EXE 里资源管理器实际会显示的那张 32×32 位图：

```
EXE : release\KHDownloader\KHDownloader.exe
      32x32  像素 SHA-256 = 091350573d38366327b864d4f1733994…
      平均色 RGB = (53, 108, 196)     蓝像素 70.9%   白像素 14.2%

源 assets/KHDownloader.ico 的 32x32 图像：PNG 压缩，2,258 B
  源图标平均色 RGB = (53, 108, 196)   蓝像素 70.9%
  平均色最大通道差 = 0
  → 与源 ICO 图标一致

参照 EXE：release/KHDownloader_v008/KHDownloader_v008.exe（旧版本）
      32x32  像素 SHA-256 = a769af7d13852ca1349ab01b9d490b3c…
      平均色 RGB = (148, 155, 136)
      与本 EXE 像素完全一致：False
      不同字节 3582/4096 (87.5%)
  → 两者图标不同，新 EXE 确实换了图标
```

**这里有两处值得记录的自我纠正**（都属于"看起来通过了，其实没验证"）：

1. 第一版判定用的是"蓝像素占比 > 阈值"，结果**新旧 EXE 都判通过** ——
   因为 PyInstaller 的默认应用图标同样是蓝色系。那个判定等于没验证。
   改成**逐像素比对 + 与源 ICO 平均色/蓝像素比例精确对照**后，才有区分度。
2. ICO 里那张 32×32 是 PNG 且用了 filter=4（Paeth），最初的反滤波只实现了 filter 0/1/2，
   结构比对被**静默跳过** —— 但结论文字却已经写成"与源 ICO 一致"。
   这属于**结论强于证据**。已补齐 Paeth 实现，并加了 `compared_with_source` 标志：
   比对没真正成功时，结论只敢说"部分通过"。

**未验证项（如实说明）**：以上都是**在执行环境**用 Win32 API 拿到的客观数据，
但**没有**覆盖用户机器上的 **Windows 图标缓存**。若用户在同一目录看到旧图标，
先复制到另一个空文件夹再看；仍显示旧图标再考虑重启资源管理器。
本轮**没有**删除任何系统缓存，**没有**改注册表。

---

## 12. 用户复测步骤

详见交付目录内 `docs/USER_RETEST_v008.md`。核心步骤：

1. **场景 A（v008 主线）**：地址框里**故意留着 2011 的地址**，然后导入 2003 的 HTML。
   预期：弹出"地址与网页不是同一张专辑"，同时列出两个地址。
2. 在弹框里选「**是**」使用网页中的专辑地址 → 应看到**19 首**的曲目预览
   （首曲 `Airwaves Arr.ver.`，末曲 `_and then_`）。
3. 在预览里点「**是**」主动开始 → 观察日志与保存目录（默认 `~\Downloads\KHInsider`）。
4. 想自己保存网页时：点「**保存网页指引**」→ 按提示用浏览器打开 → `Ctrl+S` → 选「网页，仅 HTML」→
   再点「导入专辑页面…」。
5. **场景 E（本次新增）**：双击 `release\KHDownloader\KHDownloader.exe`，
   看**文件管理器里的图标**和**窗口标题栏/任务栏的图标**是否都是蓝底白色音符。

失败时只需要把**日志文本框里的几行文字**发回来即可，不需要 Cookie、不需要截图。

---

## 13. 验收对照（Brief §12）

| 功能目标 | 状态 |
|---|---|
| 1. 原样 2003 HTML 不再因错误地址误报无曲目；能说明冲突并确认使用正确来源 | ✅ 实现 + **原样文件实跑**（§7.2 / §7.4） |
| 2. 地址为空时可导入与来源识别；不可靠识别时要求明确核对 | ✅ 实现 + 原样文件实跑 |
| 3. 导入后出现 19 首预览，由用户主动开始；状态与最终下载来源一致 | ✅ 实现 + GUI 用例 + 原样文件实跑 |
| 4. UI 提供打开浏览器、保存指引、选择已保存文件的连贯流程，支持范围真实明确 | ✅ 实现（MHTML 明确不支持并说明；按钮程序化验证可见可点） |
| 5. 原有路径、线程安全、取消和文件保护回归通过 | ✅ 167 / 167（图标改动后再跑导入+GUI 子集 52 / 52） |
| 6. 新建集中交付文件夹，EXE / README / 完整源码 / 文档齐全，副本验证通过；Report 含完整审查信息，未完成项如实列出 | ✅ §10（含交付副本 167/167、EXE 哈希一致、干净目录启动） |
| **7. 最终入口为无尾号的 `KHDownloader.exe`，嵌入新图标并设置窗口图标；源码资产、spec、脚本、README 和实际交付路径一致，干净目录可独立运行** | ✅ §9.5（PE 逐字节 7/7）+ §11.3（标题栏与资源管理器实测）+ §9.4（干净目录启动且图标生效） |

**最终裁决：`Implemented — Awaiting User Verification`**

理由：本轮全部功能目标（含 §10.1 的命名与图标补充）已实现，
适用的自动化检查、原样文件实跑、真实首曲下载、EXE 冻结自检、图标三层验证与交付副本验证
均已完成并留有证据；唯一未完成的是**用户本人按 `USER_RETEST_v008.md` 亲手点一遍**。
按 Brief §12 的定义，`Import Workflow Fix Verified` 要求"**在用户环境复测确认**"，因此本报告不越过这一步。

本报告同时明确：本轮不据此宣称 Cloudflare 直连问题消失、所有专辑均可下载，或整个项目已完整发布就绪。