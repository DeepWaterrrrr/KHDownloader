# -*- coding: utf-8 -*-
"""verify_exe_icon.py —— 验证图标**真的编进了 EXE 的 PE 资源表**。

为什么需要这个：把旧 EXE 改名成 KHDownloader.exe 也能让文件名对上，
但资源管理器、快捷方式、任务栏读的仍然是 EXE 内部资源表里那份图标。
所以必须解析 PE 目录，把图标组（RT_GROUP_ICON）里每个尺寸的图像数据
逐个读出来，和 assets/KHDownloader.ico 对比 —— 字节一致才算嵌入成功。

同时校验 EXE 里确实打包了运行资源 assets/KHDownloader.ico 和 .png
（onefile 会用 PyInstaller 的 CArchive 存放，这里用 PKG magic + 内容匹配定位）。

用法：
    python tools/verify_exe_icon.py dist/KHDownloader.exe [assets/KHDownloader.ico]
"""

from __future__ import annotations

import hashlib
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---- PE 结构常量 -----------------------------------------------------------
DOS_MAGIC = b"MZ"
PE_SIG = b"PE\x00\x00"
RT_ICON = 3          # 图标图像
RT_GROUP_ICON = 14   # 图标组（.ico 文件的头）
RESOURCE_TYPE = 16   # 资源目录
#: PE data directory 的**索引**（不是资源类型 ID）。IMAGE_DIRECTORY_ENTRY_RESOURCE = 2，
#: 索引 0 是导出表、1 是导入表、2 才是资源表 —— 写错会把版本信息当资源表读。
RESOURCE_DIR_INDEX = 2


def _u16(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def parse_pe(path: Path) -> dict:
    """解析 PE，返回 {资源类型: {资源名: 字节数据}}。"""
    data = path.read_bytes()
    if data[:2] != DOS_MAGIC:
        raise SystemExit(f"不是 PE 文件（MZ 头缺失）：{path}")

    pe_off = _u32(data, 0x3C)
    if data[pe_off:pe_off + 4] != PE_SIG:
        raise SystemExit(f"PE 签名缺失：{path}")

    coff = pe_off + 4
    nsections = _u16(data, coff + 2)
    opt_size = _u16(data, coff + 16)
    opt = coff + 20
    magic = _u16(data, opt)
    # PE32 = 0x10B，PE32+（64 位，PyInstaller 默认）= 0x20B。
    # 两者的 data directory 起始偏移不同，必须分开算，否则会把版本信息当资源表读。
    if magic == 0x10B:
        dd = opt + 96
    elif magic == 0x20B:
        dd = opt + 112
    else:
        raise SystemExit(f"不是 PE32/PE32+（optional header magic=0x{magic:X}）：{path}")

    # data directory 从 optional header 起算，资源表是第 3 项（索引 2）
    rsrc_rva = _u32(data, dd + RESOURCE_DIR_INDEX * 8)
    rsrc_size = _u32(data, dd + RESOURCE_DIR_INDEX * 8 + 4)
    if not rsrc_rva:
        return {"sections": [], "resources": {}, "rsrc_rva": 0, "rsrc_size": 0}

    sections = []
    sec = opt + opt_size
    for i in range(nsections):
        base = sec + i * 40
        vsize = _u32(data, base + 8)
        vaddr = _u32(data, base + 12)
        rawsize = _u32(data, base + 16)
        rawptr = _u32(data, base + 20)
        sections.append((vaddr, vsize, rawptr, rawsize))

    def rva_to_off(rva: int) -> int:
        for vaddr, vsize, rawptr, rawsize in sections:
            if vaddr <= rva < vaddr + max(vsize, rawsize):
                return rawptr + (rva - vaddr)
        raise SystemExit(f"RVA 0x{rva:X} 不在任何节内")

    res_base = rva_to_off(rsrc_rva)

    def walk(off: int, depth: int, path_names: list[int]):
        n_named = _u16(data, off + 12)
        n_id = _u16(data, off + 14)
        total = n_named + n_id
        out = []
        for i in range(total):
            entry = off + 16 + i * 8
            name = _u32(data, entry)
            child = _u32(data, entry + 4)
            # 高位为 0 = 整数 ID；否则是名称偏移（本 EXE 用不到）
            key = name & 0x7FFFFFFF
            if child & 0x80000000:
                out.extend(walk(res_base + (child & 0x7FFFFFFF), depth + 1, path_names + [key]))
            else:
                leaf = res_base + child
                data_rva = _u32(data, leaf)
                size = _u32(data, leaf + 4)
                out.append((path_names + [key], rva_to_off(data_rva), size))
        return out

    leaves = walk(res_base, 0, [])

    resources: dict = {}
    for names, off, size in leaves:
        # 资源树是三层：TYPE → NAME → LANGUAGE。
        # 键必须取 **NAME**（中间层，names[1]）；取 names[-1] 会拿到 LANGUAGE id，
        # 于是按图标组里记录的 resource_id 查不到东西。
        rtype = names[0] if names else 0
        rname = names[1] if len(names) > 2 else (names[-1] if names else 0)
        resources.setdefault(rtype, {})[rname] = data[off:off + size]

    return {"sections": sections, "resources": resources, "rsrc_rva": rsrc_rva, "rsrc_size": rsrc_size}


def ico_images(ico: bytes) -> list[tuple[int, bytes]]:
    """从 .ico 解析出 [(像素边长, 该尺寸的图像数据)]。"""
    assert ico[:4] == b"\x00\x00\x01\x00", "不是合法 ICO"
    count = _u16(ico, 4)
    out = []
    for i in range(count):
        off = 6 + i * 16
        w = ico[off] or 256
        size = _u32(ico, off + 8)
        doff = _u32(ico, off + 12)
        out.append((w, ico[doff:doff + size]))
    return out


# PyInstaller CArchive 结尾标记：'MEI\014\013\012\013\016'
PKG_MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
#: 条目结构：entrylen(4) offset(4) datalen(4) ulen(4) flag(1) spare(5) name...
TOC_ENTRY = struct.Struct("!IIIIII")


def _carchive_entries(exe_bytes: bytes) -> list[str] | None:
    """列出 onefile 包（PyInstaller CArchive）里的所有条目名。

    布局：文件末尾是 ``PKG_MAGIC`` + 包大小 + TOC 偏移 + TOC 长度，
    紧接其后是 TOC 条目串，每条前 4 字节是自身长度（含这 4 字节）。

    返回 None 表示没找到这个结构（不是 PyInstaller onefile 产物）。
    """
    magic_pos = exe_bytes.rfind(PKG_MAGIC)
    if magic_pos < 0 or magic_pos + 4 + 16 > len(exe_bytes):
        return None
    # PKG_MAGIC(8) + pkg_len(4) + toc_offset(4) + toc_len(4)
    pkg_len = struct.unpack_from("!I", exe_bytes, magic_pos + 8)[0]
    toc_off = struct.unpack_from("!I", exe_bytes, magic_pos + 12)[0]
    toc_len = struct.unpack_from("!I", exe_bytes, magic_pos + 16)[0]
    if toc_len == 0 or toc_len > len(exe_bytes):
        return None

    pkg_start = magic_pos + 8 + pkg_len - toc_len
    if pkg_start < 0 or pkg_start + toc_len > len(exe_bytes):
        return None

    out: list[str] = []
    p = pkg_start
    end = pkg_start + toc_len
    while p < end:
        if p + 4 > end:
            break
        entry_len = struct.unpack_from("!I", exe_bytes, p)[0]
        if entry_len == 0 or entry_len < TOC_ENTRY.size + 1:
            break
        # entry_len 的最高位在旧格式里代表"已压缩"，掩掉低位长度
        length = entry_len & 0x7FFFFFFF
        _, _, _, _, flag, _spare = TOC_ENTRY.unpack_from(exe_bytes, p)
        name_bytes = exe_bytes[p + TOC_ENTRY.size: p + length]
        name = name_bytes.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        if name:
            out.append(name)
        p += length
    return out or None


def main() -> int:
    exe_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "KHDownloader.exe"
    ico_path = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "assets" / "KHDownloader.ico"
    png_path = ROOT / "assets" / "KHDownloader.png"

    if not exe_path.is_file():
        print(f"[失败] 找不到 EXE：{exe_path}")
        return 1
    if not ico_path.is_file():
        print(f"[失败] 找不到 ICO：{ico_path}")
        return 1

    exe_bytes = exe_path.read_bytes()
    ico_bytes = ico_path.read_file() if hasattr(ico_path, "read_file") else ico_path.read_bytes()
    png_bytes = png_path.read_bytes()

    print("=" * 68)
    print("EXE 图标嵌入验证（解析 PE 资源表，不靠文件名判断）")
    print("=" * 68)
    print(f"EXE : {exe_path}")
    print(f"      {len(exe_bytes):,} B   sha256 {hashlib.sha256(exe_bytes).hexdigest()}")
    print(f"ICO : {ico_path}  {len(ico_bytes):,} B")
    print(f"PNG : {png_path}  {len(png_bytes):,} B")

    pe = parse_pe(exe_path)
    res = pe["resources"]
    print(f"\n资源表 RVA=0x{pe['rsrc_rva']:X}  大小={pe['rsrc_size']:,} B")
    print(f"资源类型：{sorted(res)}   （3=RT_ICON, 14=RT_GROUP_ICON）")

    failures = []

    # ---- 1. 图标组 ---------------------------------------------------------
    if RT_GROUP_ICON not in res:
        failures.append("EXE 里没有 RT_GROUP_ICON（图标组），说明图标没编进去")
        print("\n[失败] EXE 里没有图标组（RT_GROUP_ICON）")
        return 1

    group_raw = next(iter(res[RT_GROUP_ICON].values()))
    # GROUP_ICON 结构：reserved(2) type(2) count(2) 然后 count 个 GRPICONDIRENTRY(14)
    assert group_raw[:4] == b"\x00\x00\x01\x00", "图标组头异常"
    gcount = _u16(group_raw, 4)
    pe_sizes = []
    for i in range(gcount):
        e = 6 + i * 14
        w = group_raw[e] or 256
        h = group_raw[e + 1] or 256
        ncolors = group_raw[e + 2]
        planes = _u16(group_raw, e + 4)
        bpp = _u16(group_raw, e + 6)
        nbytes = _u32(group_raw, e + 8)
        rid = _u16(group_raw, e + 12)
        pe_sizes.append((w, h, bpp, nbytes, rid))
    print(f"\n[1] 图标组内含 {gcount} 个规格：")
    for w, h, bpp, nbytes, rid in pe_sizes:
        print(f"      {w}x{h}  {bpp}bpp  {nbytes:,} B  resource_id={rid}")

    # ---- 2. 每个规格的图像数据是否与源 ICO 逐字节一致 ----------------------
    icon_res = res.get(RT_ICON, {})
    src_images = dict(ico_images(ico_bytes))
    print(f"\n[2] 逐规格比对（EXE RT_ICON 数据 vs 源 assets/KHDownloader.ico）：")
    matched = 0
    for w, h, bpp, nbytes, rid in pe_sizes:
        blob = icon_res.get(rid)
        src = src_images.get(w)
        if blob is None:
            print(f"      {w}x{h}: ★ EXE 里找不到 resource_id={rid}")
            failures.append(f"{w}x{h} 图像数据缺失")
            continue
        if src is None:
            print(f"      {w}x{h}: ★ 源 ICO 里没有这个尺寸")
            failures.append(f"源 ICO 缺 {w}x{h}")
            continue
        if blob == src:
            print(f"      {w}x{h}: 一致  {len(blob):,} B  sha256 {hashlib.sha256(blob).hexdigest()[:16]}")
            matched += 1
        else:
            print(f"      {w}x{w}: ★ 不一致  EXE {len(blob):,} B vs ICO {len(src):,} B")
            failures.append(f"{w}x{h} 数据不一致")

    need = {16, 24, 32, 48, 64, 128, 256}
    have = {w for w, _, _, _, _ in pe_sizes}
    if need - have:
        failures.append(f"EXE 图标缺规格 {sorted(need - have)}")
    print(f"\n      规格齐全：{'是' if not (need - have) else '否，缺 ' + str(sorted(need - have))}")
    print(f"      逐字节匹配：{matched}/{len(pe_sizes)}")

    # ---- 3. 运行资源（datas）是否真打进包 ---------------------------------
    # 不能靠"在 EXE 原始字节里搜 ICO"来判断：onefile 的 CArchive 里条目可能是
    # 压缩存储的，原始字节根本不在文件里。这里解析 PyInstaller 的 CArchive 目录，
    # 按条目名确认 assets/ 两份资源确实在包里。
    print("\n[3] 运行资源（窗口图标在运行时读它）：")
    names_in_pkg: list[str] | None = None
    try:
        from PyInstaller.archive.readers import CArchiveReader

        names_in_pkg = list(CArchiveReader(str(exe_path)).toc)
        source = "PyInstaller CArchiveReader"
    except Exception as exc:
        print(f"      （PyInstaller reader 不可用：{exc}；改用内置解析器）")
        names_in_pkg = _carchive_entries(exe_bytes)
        source = "内置解析器"

    if names_in_pkg is None:
        print("      ★ 无法读取 onefile 包目录，datas 是否打包无法确认")
        failures.append("读不出 onefile 包目录（datas 是否打包无法确认）")
    else:
        for want in ("assets/KHDownloader.ico", "assets/KHDownloader.png"):
            hit = [n for n in names_in_pkg if n.replace("\\", "/").endswith(want)]
            if hit:
                print(f"      {want:24} 在包内（{source}）：{hit[0]}")
            else:
                print(f"      {want:24} ★ 不在包内（{source}）")
                failures.append(f"{want} 不在 onefile 包内（spec datas 漏了？）")
        print(f"      包内条目总数：{len(names_in_pkg)}（读取方式：{source}）")

    # ---- 4. 结论 -----------------------------------------------------------
    print("\n" + "=" * 68)
    if failures:
        print("[结论] 未通过：")
        for f in failures:
            print("  - " + f)
        return 1
    print(f"[结论] 通过：图标组 {gcount} 个规格全部逐字节匹配 assets/KHDownloader.ico，")
    print("       且 ico/png 两份运行资源都已打进 onefile 包。")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())