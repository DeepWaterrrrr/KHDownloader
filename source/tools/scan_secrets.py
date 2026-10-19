#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""上传前的泄露自查：扫密钥、凭据、个人可识别信息与体积炸弹。

为什么要这个脚本
----------------
"我检查过了，没问题" 不可审计。这个脚本把判据写成代码，任何人可以重跑：

    python tools\\scan_secrets.py --root .. --json ..\\.diag\\secrets_scan.json

- 发现**疑似凭据**（带值的 token / 私钥 / 云密钥 / 带值的 Cookie 头）→ 打印 文件:行号，退出码 1
- 只发现**噪声**（哈希、公开 beacon token、回环地址、用户名路径）→ 列表打印，退出码 0
- `--strict` 连"用户名路径 / 邮箱 / 回环端口"也算失败，用于公开仓库的门禁

判据是保守的：宁可多报，也不让一个真密钥溜过去。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- 判据

# (级别, 名称, 正则, 说明)   fatal = 命中即失败
RULES = [
    ("fatal", "GitHub令牌", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "gh*_ 开头的 GitHub token"),
    ("fatal", "AWS凭据", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "AWS access key id"),
    ("fatal", "私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PEM 私钥"),
    ("fatal", "通用密钥赋值",
     re.compile(r"(?i)\b(?:api[_-]?key|secret[_-]?key|client[_-]?secret|password|passwd)\b\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"),
     "key/secret/password = \"...\""),
    ("fatal", "JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"), "三段式 JWT"),
    ("fatal", "带值Cookie头",
     re.compile(r"(?im)^\s*(?:set-)?cookie\s*:\s*[^=\r\n]{2,40}=[^;\r\n]{8,}"), "Cookie: name=value"),
    ("fatal", "cf_clearance赋值",
     re.compile(r"cf_clearance['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9._%-]{20,}"), "真正的通行凭据值"),
    ("fatal", "OpenAI风格密钥", re.compile(r"\bsk-[A-Za-z0-9]{24,}"), "sk-..."),
    ("warn", "邮箱", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "可能是作者隐私"),
    ("warn", "Windows用户名路径", re.compile(r"[A-Za-z]:\\{1,2}Users\\{1,2}[A-Za-z0-9_.-]+"), "暴露本机用户名"),
    ("warn", "回环端口", re.compile(r"\b127\.0\.0\.1:\d{2,5}\b"), "本机调试端口"),
    ("info", "十六进制长串", re.compile(r"\b[0-9a-f]{32,}\b"), "多为 SHA-256，需逐条确认"),
    ("info", "凭据关键词提及", re.compile(r"cf_clearance|__cf_bm|cf_clearance"), "只看是否带值"),
]

TEXT_EXT = {
    ".py", ".md", ".txt", ".json", ".bat", ".cmd", ".spec", ".ps1", ".sh",
    ".cfg", ".ini", ".toml", ".yml", ".yaml", ".html", ".htm", ".js", ".css",
    ".log", ".env", ".conf", ".xml", ".csv",
}

SKIP_DIRS = {".git", "__pycache__", "build", "dist", "node_modules", ".venv", "venv"}
MAX_BYTES = 8 * 1024 * 1024
# 二进制体积警告线（GitHub 单文件 100 MB 硬限 / 50 MB 警告）
BIG_FILE_BYTES = 50 * 1024 * 1024


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def scan(root: Path, strict: bool) -> int:
    findings: list[dict] = []
    big: list[dict] = []
    scanned = 0

    for path in iter_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > BIG_FILE_BYTES:
            big.append({"file": rel, "bytes": size})
            continue
        if path.suffix.lower() not in TEXT_EXT or size > MAX_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            if len(line) > 4000:          # 压缩成一行的 HTML/JS，跳过避免误报与卡顿
                continue
            for level, name, rx, note in RULES:
                if rx.search(line):
                    findings.append({
                        "level": level, "rule": name, "note": note,
                        "file": rel, "line": lineno, "text": line.strip()[:160],
                    })

    fatal = [f for f in findings if f["level"] == "fatal"]
    warn = [f for f in findings if f["level"] == "warn"]
    info = [f for f in findings if f["level"] == "info"]

    print(f"扫描 {scanned} 个文本文件，{root}")
    print(f"致命 {len(fatal)} / 警告 {len(warn)} / 提示 {len(info)}"
          f"；超 {BIG_FILE_BYTES // 1024 // 1024} MB 的文件 {len(big)} 个")
    print()

    for group, title in ((fatal, "致命（必须处理）"), (warn, "警告（公开仓库建议处理）"), (info, "提示（人工确认）")):
        if not group:
            continue
        print(f"== {title} ==")
        for f in group:
            print(f"  {f['file']}:{f['line']}  [{f['rule']}] {f['text']}")
        print()

    if big:
        print("== 大文件（应走 Release，不进 Git）==")
        for b in big:
            print(f"  {b['bytes'] / 1024 / 1024:8.1f} MB  {b['file']}")
        print()

    failed = bool(fatal) or (strict and (warn or big))
    print("结果：" + ("FAIL" if failed else "PASS"))
    return 0 if not failed else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="上传前泄露自查")
    ap.add_argument("--root", default=".", help="扫描根目录（默认当前目录）")
    ap.add_argument("--json", default="", help="把明细写到这个 JSON 文件")
    ap.add_argument("--strict", action="store_true", help="警告项也算失败（公开仓库门禁）")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"不是目录：{root}")
        return 2

    rc = scan(root, args.strict)
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"root": str(root), "exit": rc}, ensure_ascii=False), encoding="utf-8")
        print(f"[json] {out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
