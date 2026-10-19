# -*- coding: utf-8 -*-
"""verify_frozen_download.py —— 冻结运行时下载 / 取消验证（打包验证工具，不随发布走）

起一个本地假站点（127.0.0.1），在冻结后的运行时里：
  · 真实下载一个文件 → 验证文件落盘、且不留 .part 残留；
  · 再用 should_cancel 触发取消 → 验证抛 CancelledError 且不留半成品。

构建（在项目根目录执行；pathex 指回根目录以便找到应用模块）：

    python -m PyInstaller --noconfirm --clean --onefile --console ^
        --name _frozen_dl_test --paths . tools/verify_frozen_download.py

然后运行 dist\\_frozen_dl_test.exe。
"""
import sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import khdownloader_core as c  # noqa: E402

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    def log_message(self, *a): pass
    def do_GET(self):
        body = b"ID3" + b"\x00" * 4096
        self.send_response(200); self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}/x.mp3"
with tempfile.TemporaryDirectory() as tmp:
    f = Path(tmp)
    s = c.make_session()
    d, w, skip = c.download_file(s, url, f, "a.mp3")
    print("[DL] frozen download OK:", d.exists(), "bytes:", w, "no .part:", not Path(str(d)+".part").exists())
    # 取消
    flag = {"c": True}
    try:
        c.download_file(s, url, f, "b.mp3", should_cancel=lambda: flag["c"])
    except c.CancelledError:
        print("[DL] cancel raised CancelledError OK; no b.mp3:", not (f/"b.mp3").exists())
    s.close()
srv.shutdown()
print("[DL] DONE")
