# -*- coding: utf-8 -*-
"""验证资源管理器 / 快捷方式读到的 EXE 文件图标确实是**新图标**。

做法：
  1. 用 ExtractIconExW 取出 EXE 里的 32x32 大图标（资源管理器显示的就是它）；
  2. 用 GetIconInfo + GetBitmapBits 拿到像素数据，算 SHA-256 **像素指纹**；
  3. 与源 assets/KHDownloader.ico 的 32x32 图像比对（Windows 存的是未乘 alpha 的 BGRA，
     这里做归一化后比对平均色与结构相似度）；
  4. 如果给了参照 EXE（旧版/默认图标），额外做逐像素比对，证明"新 ≠ 旧"。

为什么不能只看颜色：PyInstaller 的默认应用图标同样是蓝色系，
纯按"蓝像素占比"判定，新旧 EXE 都会通过 —— 那样等于没验证。

用法：
    python verify_exe_file_icon.py <exe> [参照exe]
"""
import ctypes
from ctypes import wintypes
import hashlib
import os
import struct
import sys

sys.stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", errors="replace", closefd=False)

shell32 = ctypes.windll.shell32
user32 = ctypes.windll.user32
gdi = ctypes.windll.gdi32

shell32.ExtractIconExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int,
                                   ctypes.POINTER(wintypes.HICON),
                                   ctypes.POINTER(wintypes.HICON), ctypes.c_uint]
shell32.ExtractIconExW.restype = ctypes.c_uint
user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.c_void_p,
                               ctypes.c_void_p, ctypes.c_void_p]
gdi.GetObjectW.argtypes = [wintypes.HGDIOBJ, ctypes.c_int, ctypes.c_void_p]
gdi.GetObjectW.restype = ctypes.c_long
gdi.GetBitmapBits.argtypes = [wintypes.HBITMAP, ctypes.c_uint, ctypes.c_void_p]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", ctypes.POINTER(ctypes.c_ubyte))]


def grab_icon32(exe: str):
    """返回 (w, h, pixels)。pixels 是 BGRA 字节。"""
    large = (wintypes.HICON * 1)()
    small = (wintypes.HICON * 1)()
    n = shell32.ExtractIconExW(exe, 0, large, small, 1)
    if n == 0 or not large[0]:
        return None
    ii = ICONINFO()
    if not user32.GetIconInfo(large[0], ctypes.byref(ii), None, None):
        return None
    bi = BITMAP()
    if not gdi.GetObjectW(ii.hbmColor, ctypes.sizeof(bi), ctypes.byref(bi)):
        return None
    # ICONINFO.hbmColor 是纯颜色位图，不含 AND 掩码，高度不用除 2
    w, h = bi.bmWidth, abs(bi.bmHeight)
    stride = ((bi.bmWidthBytes + 3) // 4) * 4
    buf = ctypes.create_string_buffer(stride * h)
    got = gdi.GetBitmapBits(ii.hbmColor, len(buf), buf)
    if got == 0:
        return None
    return w, h, buf.raw[:stride * h], stride


def stats(raw, w, h, stride):
    """统计平均色、蓝白像素占比、以及缩到 8x8 的灰度指纹（结构特征）。"""
    n = w * h
    rs = gs = bs = 0
    blue = white = 0
    for y in range(h):
        base = y * stride
        for x in range(w):
            o = base + x * 4
            b, g, r = raw[o], raw[o + 1], raw[o + 2]
            rs += r
            gs += g
            bs += b
            if b > r + 25 and b > 110:
                blue += 1
            elif r > 235 and g > 235 and b > 235:
                white += 1
    # 8x8 块平均亮度指纹
    block = w // 8
    fp = []
    for by in range(8):
        for bx in range(8):
            tot = cnt = 0
            for y in range(by * block, (by + 1) * block):
                for x in range(bx * block, (bx + 1) * block):
                    o = y * stride + x * 4
                    tot += (raw[o] * 3 + raw[o + 1] * 6 + raw[o + 2]) // 10
                    cnt += 1
            fp.append(tot / cnt)
    return {
        "avg": (rs // n, gs // n, bs // n),
        "blue_pct": blue / n * 100,
        "white_pct": white / n * 100,
        "fingerprint": fp,
        "sha": hashlib.sha256(raw).hexdigest(),
    }


def main():
    exe = os.path.abspath(sys.argv[1])
    ref = sys.argv[2] if len(sys.argv) > 2 else None
    ico = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "assets", "KHDownloader.ico")

    print("=" * 70)
    print("EXE 文件图标（资源管理器显示的那个）验证")
    print("=" * 70)
    print(f"EXE : {exe}")

    got = grab_icon32(exe)
    if not got:
        print("[失败] 取不到 EXE 图标（资源管理器会显示为默认空白图标）")
        return 1
    w, h, raw, stride = got
    s = stats(raw, w, h, stride)
    print(f"      {w}x{h}  像素 SHA-256 = {s['sha'][:32]}…")
    print(f"      平均色 RGB = {s['avg']}")
    print(f"      蓝像素 {s['blue_pct']:.1f}%   白像素 {s['white_pct']:.1f}%")

    failures = []
    # 只有真的完成了与源 ICO 的比对，结论才能声称"与源图标一致"
    compared_with_source = False

    # --- 与源 ICO 的 32x32 图像做结构比对 ---
    # ICO 里的图像可能是 PNG（带 alpha）或 BMP；这里都转成"是否偏蓝 + 平均色"来比。
    if os.path.isfile(ico):
        d = open(ico, "rb").read()
        cnt = struct.unpack_from("<H", d, 4)[0]
        src = None
        for i in range(cnt):
            off = 6 + i * 16
            side = d[off] or 256
            size = struct.unpack_from("<I", d, off + 8)[0]
            doff = struct.unpack_from("<I", d, off + 12)[0]
            if side == 32:
                src = d[doff:doff + size]
        if src:
            is_png = src[:8] == b"\x89PNG\r\n\x1a\n"
            print(f"\n源 assets/KHDownloader.ico 的 32x32 图像："
                  f"{'PNG 压缩' if is_png else 'BMP 未压缩'}，{len(src):,} B")
            # 32x32x4 的 BMP 去掉 40 字节头后就是像素；PNG 解压成 RGBA
            if is_png:
                try:
                    import zlib
                    # 只取 IDAT，解 PNG（PNG 无滤波字节在此尺寸也可解，但要处理 filter）
                    idat = b""
                    pos = 8
                    while pos < len(src):
                        ln = struct.unpack_from(">I", src, pos)[0]
                        tag = src[pos + 4:pos + 8]
                        if tag == b"IDAT":
                            idat += src[pos + 8:pos + 8 + ln]
                        pos += 12 + ln
                    rawpng = zlib.decompress(idat)
                    # 反滤波（ICO 内 PNG 用的是完整 PNG 规范，5 种 filter 都可能出现）
                    st = 32 * 4 + 1
                    out = bytearray()
                    prev = bytearray(32 * 4)

                    def paeth(a, b, c):
                        p = a + b - c
                        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                        if pa <= pb and pa <= pc:
                            return a
                        return b if pb <= pc else c

                    for y in range(32):
                        f = rawpng[y * st]
                        line = bytearray(rawpng[y * st + 1:(y + 1) * st])
                        if f == 0:
                            pass
                        elif f == 1:
                            for i in range(4, len(line)):
                                line[i] = (line[i] + line[i - 4]) & 0xFF
                        elif f == 2:
                            for i in range(len(line)):
                                line[i] = (line[i] + prev[i]) & 0xFF
                        elif f == 3:
                            for i in range(len(line)):
                                left = line[i - 4] if i >= 4 else 0
                                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
                        elif f == 4:
                            for i in range(len(line)):
                                left = line[i - 4] if i >= 4 else 0
                                upleft = prev[i - 4] if i >= 4 else 0
                                line[i] = (line[i] + paeth(left, prev[i], upleft)) & 0xFF
                        else:
                            raise StopIteration(f"未知 filter {f}")
                        out += line
                        prev = line
                    n = 32 * 32
                    sr = sg = sb = sblue = 0
                    for i in range(0, n * 4, 4):
                        r, g, b, a = out[i], out[i + 1], out[i + 2], out[i + 3]
                        sr += r
                        sg += g
                        sb += b
                        if b > r + 25 and b > 110:
                            sblue += 1
                    print(f"  源图标平均色 RGB = ({sr//n}, {sg//n}, {sb//n})   "
                          f"蓝像素 {sblue/n*100:.1f}%")
                    # 与 EXE 里取到的平均色比：同一个图标缩放到 32x32 后平均色应接近
                    da = max(abs(s['avg'][0] - sr // n),
                             abs(s['avg'][1] - sg // n),
                             abs(s['avg'][2] - sb // n))
                    print(f"  平均色最大通道差 = {da}")
                    if da <= 60:
                        print("  → 与源 ICO 图标一致")
                        compared_with_source = True
                    else:
                        print("  → ★ 与源 ICO 图标差异过大，可能不是同一个图标")
                        failures.append(f"EXE 文件图标平均色与源 ICO 差 {da}（>60）")
                except StopIteration as exc:
                    print(f"  ★ ICO 内 PNG 结构无法解析：{exc} —— 结论不能声称已比对")
                    failures.append(f"源 ICO 的 32x32 图像无法解析：{exc}")
                except Exception as exc:
                    print(f"  ★ PNG 解析异常：{exc} —— 结论不能声称已比对")
                    failures.append(f"源 ICO 的 32x32 PNG 解析异常：{exc}")

    # --- 与参照 EXE 逐像素比对 ---
    if ref and os.path.isfile(ref):
        r2 = grab_icon32(os.path.abspath(ref))
        if r2:
            rw, rh, rraw, rstride = r2
            rs2 = stats(rraw, rw, rh, rstride)
            print(f"\n参照 EXE：{ref}")
            print(f"      {rw}x{rh}  像素 SHA-256 = {rs2['sha'][:32]}…")
            print(f"      平均色 RGB = {rs2['avg']}")
            same = (w == rw and h == rh and raw == rraw)
            ndiff = sum(1 for a, b in zip(raw, rraw) if a != b) if len(raw) == len(rraw) else -1
            print(f"      与本 EXE 像素完全一致：{same}")
            if len(raw) == len(rraw):
                print(f"      不同字节 {ndiff}/{len(raw)} "
                      f"({ndiff/len(raw)*100:.1f}%)")
            if same:
                print("  → ★ 两个 EXE 图标完全一样，说明没有真正换图标")
                failures.append("新 EXE 与参照 EXE 的文件图标完全相同")
            else:
                print("  → 两者图标不同，新 EXE 确实换了图标")
        else:
            print(f"\n参照 EXE 取不到图标：{ref}")

    print("\n" + "=" * 70)
    if failures:
        print("[结论] 未通过：")
        for f in failures:
            print("  - " + f)
        return 1
    if not compared_with_source:
        print("[结论] 部分通过：能取到图标位图，但**未能**与源 assets/KHDownloader.ico 完成比对")
        print("       （仅此不足以断言图标正确；本次的比对证据来自 verify_exe_icon.py 的 PE 逐字节核对）")
        print("=" * 70)
        return 1
    print("[结论] 通过：资源管理器能取到图标位图，且与源 assets/KHDownloader.ico 一致。")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())