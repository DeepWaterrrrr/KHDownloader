# -*- coding: utf-8 -*-
"""验证 KHDownloader.exe 窗口图标**真的显示出来了**（不是只看 ico 文件存在）。

三个独立证据：
  1. WM_GETICON（窗口自身图标句柄）
  2. GetClassLongPtrW GCLP_HICON / GCLP_HICONSM（类图标句柄）
     —— Tk 的 wm iconbitmap 走的是**类图标**这条路，只查 WM_GETICON 会误报"没有图标"
  3. PrintWindow(PW_RENDERFULLCONTENT) 抓取非客户区（标题栏）像素，
     从像素里判定标题栏左上角是否真有非背景色块

用法：python verify_window_icon.py <exe路径> [输出标题栏png]
"""
import ctypes
from ctypes import wintypes
import io
import os
import struct
import subprocess
import sys
import time
import zlib

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

EXE = os.path.abspath(sys.argv[1])
OUT = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else None
IMAGE = os.path.basename(EXE)

WM_GETICON = 0x007F
ICON_BIG, ICON_SMALL, ICON_SMALL2 = 1, 0, 2
GCLP_HICONSM, GCLP_HICON = -13, -14
WM_GETMINMAXINFO = 0x0024
PW_RENDERFULLCONTENT = 0x00000002

user32 = ctypes.windll.user32
gdi = ctypes.windll.gdi32

# 64 位下句柄必须声明成 WPARAM/LPARAM 的正确宽度，否则高位被截成 0
user32.SendMessageW.restype = ctypes.c_longlong
user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.GetClassLongPtrW.restype = ctypes.c_longlong
user32.GetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

env = dict(os.environ)
env["PATH"] = r"C:\Windows\System32;C:\Windows"
p = subprocess.Popen([EXE], cwd=os.path.dirname(EXE), env=env,
                     creationflags=0x00000008 | 0x00000200, close_fds=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL)
print("launched:", p.pid)
time.sleep(9)

# onefile EXE 会派生子进程：引导进程解包后另起真正的 GUI 进程。
# 只按 p.pid 过滤会找不到窗口，所以按**进程名**收全部 PID。
_csv = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {IMAGE}", "/FO", "CSV", "/NH"],
                      capture_output=True).stdout.decode("gbk", errors="replace")
PIDS = set()
for line in _csv.splitlines():
    parts = [x.strip('"') for x in line.split('","')]
    if len(parts) >= 2 and parts[1].isdigit():
        PIDS.add(int(parts[1]))
print("pids:", sorted(PIDS))

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


targets = []


def cb(hwnd, lparam):
    if not user32.IsWindowVisible(hwnd):
        return True
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value in PIDS:
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        cbuf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cbuf, 256)
        r = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        targets.append((hwnd, buf.value, cbuf.value, r))
    return True


user32.EnumWindows(EnumWindowsProc(cb), 0)
if not targets:
    print("NO_WINDOW")
    subprocess.run(["taskkill", "/F", "/IM", IMAGE], capture_output=True)
    sys.exit(1)

hwnd, title, cls, r = targets[0]
print(f"hwnd={hwnd}  title={title!r}  class={cls!r}")
print(f"rect=({r.left},{r.top})-({r.right},{r.bottom})")

print("\n--- 图标句柄 ---")
got = {
    "WM_GETICON ICON_BIG": user32.SendMessageW(hwnd, WM_GETICON, ICON_BIG, 0),
    "WM_GETICON ICON_SMALL": user32.SendMessageW(hwnd, WM_GETICON, ICON_SMALL, 0),
    "WM_GETICON ICON_SMALL2": user32.SendMessageW(hwnd, WM_GETICON, ICON_SMALL2, 0),
    "GetClassLongPtr GCLP_HICON": user32.GetClassLongPtrW(hwnd, GCLP_HICON),
    "GetClassLongPtr GCLP_HICONSM": user32.GetClassLongPtrW(hwnd, GCLP_HICONSM),
}
for k, v in got.items():
    print(f"  {k:32} = {v}")
hwnds = [v for v in got.values() if v]

# --- 用 PrintWindow 抓非客户区 ---
w, h = r.right - r.left, r.bottom - r.top
hdc = user32.GetWindowDC(hwnd)
memdc = gdi.CreateCompatibleDC(hdc)
bmp = gdi.CreateCompatibleBitmap(hdc, w, h)
gdi.SelectObject(memdc, bmp)
ok = user32.PrintWindow(hwnd, memdc, PW_RENDERFULLCONTENT)
print(f"\nPrintWindow(PW_RENDERFULLCONTENT) = {ok}")


class BIH(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


bih = BIH()
bih.biSize = 40
bih.biWidth = w
bih.biHeight = -h
bih.biPlanes = 1
bih.biBitCount = 32
bih.biCompression = 0
buf = ctypes.create_string_buffer(w * h * 4)
lines = gdi.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bih), 0)
print(f"GetDIBits lines={lines}")
raw = buf.raw


def px(x, y):
    o = (y * w + x) * 4
    return raw[o + 2], raw[o + 1], raw[o]     # (R,G,B)


# 标题栏高度：窗口上边缘往下取 31px（Win10 默认标题栏）
TB = 31
# 背景色 = 标题栏最右侧一点（没有控件的地方）
bg = px(w - 6, 6)
print(f"\n标题栏背景色（取右上角）= {bg}")
# 统计标题栏前 40 列里非背景色像素
diff = [(x, y) for y in range(2, TB) for x in range(2, 40) if px(x, y) != bg]
print(f"标题栏左侧 2..40 列中非背景像素数 = {len(diff)}")
if diff:
    xs = [d[0] for d in diff]
    ys = [d[1] for d in diff]
    print(f"  x 范围 {min(xs)}..{max(xs)}   y 范围 {min(ys)}..{max(ys)}")
    # 颜色直方图 top5
    from collections import Counter
    cnt = Counter(px(x, y) for x, y in diff)
    for c, n in cnt.most_common(5):
        print(f"  {c}  {n} px")

if OUT:
    # 存标题栏裁剪：x 0..200, y 0..TB
    cw, ch = 200, TB
    rows = bytearray()
    for y in range(ch):
        rows.append(0)
        for x in range(cw):
            o = (y * w + x) * 4
            rows += bytes((raw[o + 2], raw[o + 1], raw[o], 255))

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xffffffff))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", cw, ch, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(rows), 9))
    png += chunk(b"IEND", b"")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, "wb").write(png)
    print(f"\n标题栏裁剪已存：{OUT}  {len(png)} B  {cw}x{ch}")

gdi.DeleteObject(bmp)
gdi.DeleteDC(memdc)
user32.ReleaseDC(hwnd, hdc)
subprocess.run(["taskkill", "/F", "/IM", IMAGE], capture_output=True)

print("\n--- 结论 ---")
print(f"图标句柄非零数：{len(hwnds)} / {len(got)}")
print(f"标题栏非背景像素：{len(diff)}")
print("判定：", "窗口图标已生效" if hwnds and len(diff) > 50 else "★ 未取到图标证据")