"""build_frozen.py —— 可复现的打包驱动（build_exe.bat 调用它）

为什么不让 build_exe.bat 直接调 `python -m PyInstaller`？因为打包这件事有两个
"看起来成功了，其实交出去的是坏程序"的坑，必须由程序来把关：

  坑 1：环境里躺着标准库的**过时 backport 包**（enum34 / typing / pathlib）。
        PyInstaller 6.x 在 compat.py 里硬性拒绝构建，`python -m PyInstaller`
        会直接报错退出，一个字节都不编译。本机就有一个 pathlib-1.0.1。
        这里用"构建期间临时改名挪走、构建结束立刻改回"的办法解决：
        **不改动用户的 Python 环境**（不 pip uninstall），而且全程可逆；
        挪走还顺带避免了那个包被误打进包里的风险。
        搬迁范围**严格限定在 site-packages**：第一版曾按 dist-info 里的
        相对路径反推，算错一级把标准库 Lib\\pathlib.py 挪走了，构建当场
        ImportError（from collections import Sequence）。现在越界路径一律跳过。

  坑 2：curl_cffi 没被打进包。它是"能不能过站点程序化访问校验"的关键依赖，
       少了它程序会**静默退回普通请求**，用户那边看到的还是被拦住，
        但没人能看出是打包漏了。所以构建完必须开包验收（见 --selftest）。

用法（在 source 目录下）：

    python tools\\build_frozen.py              # 构建 + 开包验收
    python tools\\build_frozen.py --selftest   # 构建 + 开包验收 + 冻包真连一次站点

退出码 0 才算成功。
"""

from __future__ import annotations

import argparse
import shutil
import site
import subprocess
import sys
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent.parent
SPEC = SOURCE_DIR / "KHDownloader.spec"
EXE = SOURCE_DIR / "dist" / "KHDownloader.exe"

# 中文控制台（cmd.exe 默认 936 代码页）能正常打印中文；这里只是上一道保险：
# 万一某个字符在当前代码页里编不出来，宁可显示成 ? 也不要让打包脚本崩在打印上。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 老解释器 / 被重定向的流
        pass

#: PyInstaller 6.x 拒绝与之共存的标准库过时 backport（compat.py 里的硬检查）。
OBSOLETE_BACKPORTS = ("enum34", "typing", "pathlib")
#: 临时挪走目录的名字（构建结束后会删掉这个空壳）。
DISABLED_DIR_NAME = "__khd_build_disabled__"


def _log(message: str = "") -> None:
    print(message, flush=True)


def _site_roots() -> list[Path]:
    """本机所有 site-packages 目录（第三方包只该待在这些地方）。"""
    roots: list[Path] = []
    for raw in list(site.getsitepackages()) + [site.getusersitepackages()]:
        path = Path(raw)
        if path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def _is_inside(child: Path, parent: Path) -> bool:
    return parent in child.parents


def _find_obsolete_backports() -> list[tuple[str, Path]]:
    """把环境里存在的过时 backport 定位到 **site-packages 内** 的具体文件/目录。

    这里刻意**不**用 importlib.metadata 的 locate_file()/files 去推算路径：
    实测 pathlib-1.0.1 的 dist-info 会让 locate_file("") 给出的基准目录错一级，
    结果把标准库的 Lib\\pathlib.py 当成过时包挪走 —— 挪走之后 `import pathlib`
    就落到那个坏 backport 上，PyInstaller 立刻 ImportError（from collections
    import Sequence）。所以改成只在 site-packages 里按名字找，标准库目录碰都不碰。
    """
    from importlib.metadata import PackageNotFoundError, distribution

    roots = _site_roots()
    found: list[tuple[str, Path]] = []
    for name in OBSOLETE_BACKPORTS:
        try:
            distribution(name)  # 与 PyInstaller compat.py 同款的“已安装”判据
        except PackageNotFoundError:
            continue
        hits: list[Path] = []
        for root in roots:
            module_file = root / f"{name}.py"
            if module_file.is_file():
                hits.append(module_file)
            package_dir = root / name
            if package_dir.is_dir():
                hits.append(package_dir)
            hits.extend(sorted(p for p in root.glob(f"{name}-*.dist-info") if p.is_dir()))
        found.extend((name, path) for path in hits)
        shown = "、".join(path.name for path in hits) or "无"
        _log(f"  [检出] {name}：{len(hits)} 项（{shown}）")
    return found


class _MoveAside:
    """把指定的文件/目录临时挪到影子目录，退出时（无论成败）原样搬回。

    用 with 而不是 try/finally 散在代码里，是为了保证 **Ctrl+C / 构建中途失败**
    也会还原 —— 用户的环境不该因为一次打包而变形。

    安全阀：只搬 site-packages 里面的东西。任何越界的路径（例如标准库的
    Lib\\pathlib.py）一律跳过并留痕，宁可打包失败也不去动标准库。
    """

    def __init__(self, files: list[tuple[str, Path]]) -> None:
        self._pairs: list[tuple[Path, Path]] = []
        self._roots: set[Path] = set()
        self.skipped: list[Path] = []
        roots = _site_roots()
        # 取**最具体**的那个根：site.getsitepackages() 在 Windows 上会同时给出
        # 前缀目录（...\Python311）和真正的 site-packages，先命中的若是前缀目录，
        # 影子目录就会被建到 Python311 底下 —— 不致命但很脏。
        roots.sort(key=lambda p: len(p.parts), reverse=True)
        for _, path in files:
            root = next((r for r in roots if path != r and _is_inside(path, r)), None)
            if root is None:
                self.skipped.append(path)
                continue
            self._roots.add(root)
            self._pairs.append((path, root / DISABLED_DIR_NAME / path.relative_to(root)))

    def __enter__(self) -> "_MoveAside":
        for src, dst in self._pairs:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        for src, dst in self._pairs:
            if dst.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
        for root in self._roots:
            shell = root / DISABLED_DIR_NAME
            # 只删自己建的空壳；万一里面有东西，留着让人看得见。
            try:
                if shell.is_dir() and not any(shell.rglob("*")):
                    shutil.rmtree(shell, ignore_errors=True)
            except OSError:
                pass
        return False


def _run(cmd: list[str], cwd: Path | None = None) -> int:
    _log("  $ " + " ".join(cmd))
    return subprocess.call(cmd, cwd=str(cwd) if cwd else None)


def _archive_entries(exe: Path) -> list[str]:
    """列出 EXE 包内的条目（用 PyInstaller 自带的 archive_viewer）。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "PyInstaller.utils.cliutils.archive_viewer", "-l", "-b", str(exe)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        _log(f"  [警告] 无法列出包内容：{type(exc).__name__}: {exc}")
        return []
    if proc.returncode != 0:
        _log(f"  [警告] 包内容列表读取失败（退出码 {proc.returncode}）")
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _verify_bundle(exe: Path) -> bool:
    """开包验收：curl_cffi 的代码 + libcurl 动态库必须在包里。

    这一步是**必须**的：没有它，"打包成功"和"程序真的能过校验"是两件事。
    """
    _log(f"[验收] 检查 {exe.name} 里有没有带上换指纹那一层 ...")
    entries = _archive_entries(exe)
    if not entries:
        _log("  [失败] 读不到包内容，无法确认 curl_cffi 是否打进去了。")
        return False

    module_hits = [e for e in entries if e.startswith("curl_cffi\\") or e.startswith("curl_cffi/")]
    dll_hits = [e for e in entries if "libcurl" in e.lower() and e.lower().endswith(".dll")]
    wrapper_hits = [e for e in entries if "_wrapper.pyd" in e]

    _log(f"  curl_cffi 模块条目：{len(module_hits)}")
    _log(f"  libcurl 动态库    ：{len(dll_hits)}" + (f" -> {dll_hits[0]}" if dll_hits else ""))
    _log(f"  加速扩展 _wrapper ：{len(wrapper_hits)}")

    if not module_hits or not wrapper_hits:
        _log("  [失败] 包里没有 curl_cffi —— 这个 EXE 会静默退回普通请求，还是会被站点拦住。")
        _log("         请先 `python -m pip install curl_cffi` 再重新打包。")
        return False
    if not dll_hits:
        _log("  [失败] 包里没有 libcurl 动态库 —— 换指纹这一层在别人机器上会加载失败。")
        return False
    _log("  [通过] curl_cffi + libcurl 都在包里。")
    return True


def _run_frozen_selftest(exe: Path) -> bool:
    """让冻包自己去连一次站点，用退出码说话（EXE 无控制台，只能看退出码 + 报告）。"""
    report = exe.parent / "selftest_transport.txt"
    _log(f"[自检] 运行 {exe.name} --selftest-transport（会真的访问一次站点）...")
    code = _run([str(exe), "--selftest-transport", "--selftest-out", str(report)])
    if report.is_file():
        _log("  --- 自检报告 ---")
        for line in report.read_text(encoding="utf-8").splitlines():
            _log("  " + line)
        _log("  --- 报告结束 ---")
    if code == 0:
        _log(f"  [通过] 冻包换指纹成功（退出码 0），报告：{report}")
        return True
    _log(f"  [失败] 冻包自检退出码 {code}，详见报告：{report}")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KHDownloader 打包驱动")
    parser.add_argument("--selftest", action="store_true", help="构建后让冻包真连一次站点并检查报告")
    parser.add_argument("--skip-bundle-check", action="store_true", help="跳过开包验收（仅调试用）")
    parser.add_argument("--check-only", action="store_true", help="只做环境体检并打印搬迁计划，不打包")
    args = parser.parse_args(argv)

    if not SPEC.is_file():
        _log(f"[失败] 找不到 spec：{SPEC}")
        return 1
    for icon in ("KHDownloader.ico", "KHDownloader.png"):
        if not (SOURCE_DIR / "assets" / icon).is_file():
            _log(f"[失败] 缺少 assets\\{icon}，没有真实图标就不打包。")
            return 1

    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        _log("[失败] 本机没有 curl_cffi。它是「过校验」那一层的基础：")
        _log("       python -m pip install curl_cffi")
        return 1

    _log("[1/4] 检查环境里有没有标准库的过时 backport 包 ...")
    stale = _find_obsolete_backports()
    plan = _MoveAside(stale)
    for path in plan.skipped:
        _log(f"  [跳过] {path} —— 不在 site-packages 里，本驱动不碰。")
    if plan._pairs:  # noqa: SLF001 - 同一个脚本内部的计划预览
        _log("      这些包会让 PyInstaller 直接拒绝构建；本次构建期间临时挪走，结束后还原：")
        for src, dst in plan._pairs:  # noqa: SLF001
            _log(f"        {src}  ->  {dst}")
    if args.check_only:
        _log("  [--check-only] 环境体检结束，没有打包。")
        return 0

    _log("[2/4] 开始打包 ...")
    rc = 1
    with plan:
        rc = _run(
            [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", SPEC.name],
            cwd=SOURCE_DIR,
        )
    if plan._pairs:  # noqa: SLF001
        _log("      已还原临时挪走的包（用户环境保持不变）。")
    if rc != 0:
        _log(f"[失败] PyInstaller 退出码 {rc}。")
        return 1
    if not EXE.is_file():
        _log(f"[失败] 打包命令返回成功，但没有产出 {EXE}。")
        return 1

    _log("[3/4] 验收产物 ...")
    size_mb = EXE.stat().st_size / 1024 / 1024
    _log(f"  {EXE}  （{EXE.stat().st_size:,} 字节，约 {size_mb:.1f} MB）")
    if not args.skip_bundle_check and not _verify_bundle(EXE):
        return 1

    if args.selftest:
        _log("[4/4] 冻包真实自检 ...")
        if not _run_frozen_selftest(EXE):
            return 1
    else:
        _log("[4/4] 跳过冻包自检（加 --selftest 打开：会让冻包真连一次站点）")

    _log("")
    _log("打包完成：" + str(EXE))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
