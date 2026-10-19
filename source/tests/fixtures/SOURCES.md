# tests/fixtures 样本来源说明

本目录的 HTML 样本分两类，**来源必须区分清楚**，任何报告/交付物都不得把合成样本说成用户原样文件。

## 一、用户原样保存的页面（本轮新增，v008 修订）

由用户在正常浏览器中保存的 KHInsider 专辑页（"网页，仅 HTML"另存）。

| 文件 | 原始保存路径 | 大小 | SHA-256 |
|---|---|---|---|
| `user_original_2003_album_page.html` | `D:\<用户名>\Desktop\temp\temp\3\CROSS†CHANNEL Soundtrack Arrange Version CRISS†CROSS (2003) MP3 - Download Soundtracks for FREE!.html` | 64,309 B | `7e1911885c318cb71b00ba84adc70dd571b1390ab5a1d9513b05f3ca862d8e16` |
| `user_original_2011_album_page.html` | `D:\<用户名>\Desktop\temp\temp\CROSS†CHANNEL ~In memory of all people~ SPECIAL SOUNDTRACK (2011) MP3 - Download Soundtracks for FREE!.html` | 93,766 B | `89fb458779a71b7a1cb92fc1a64842ebf11e2cb4de9e599266ef15c7e2558dd5` |

- 副本按字节原样复制，未做任何裁剪、脱敏或改写；已扫描确认不含 Cookie、`cf_clearance`、
  凭据、本地绝对路径等个人内容（详见 v008 报告"样本脱敏扫描"）。
- 2003 页面对应专辑：`https://downloads.khinsider.com/game-soundtracks/album/cross-channel-soundtrack-arrange-version-criss-cross-2003`（`table#songlist`，19 首）。
- 2011 页面对应专辑：`https://downloads.khinsider.com/game-soundtracks/album/cross-channel-in-memory-of-all-people-special-soundtrack-2011`（`table#songlist`，30 首）。

## 二、合成样本（**不是**用户原样文件）

| 文件 | 说明 |
|---|---|
| `album_page_2003_synthetic.html` | 按 Strategy AI 审查数据构造的 2003 场景结构对齐样本（19 首、首曲 `Airwaves Arr.ver.`、末曲 `_and then_`、含推荐专辑干扰链接）。**明确标注为合成**，仅用于回归测试可复现，不作为"用户原文件验证"证据。 |

## 三、其他历史样本

`album_page.html`、`real_album_page.html`、`real_song_page.html`、`song_page*.html`、
`album_page_without_songlist.html`、`no_length_album_page.html`、`slow_album_page.html`
为 v001–v003 期间采集/构造的样本，来源见各阶段报告。

## 四、复现命令

```bash
# 以交付目录或项目根为当前目录
python -m unittest discover -s tests -p "test_*.py"
```
