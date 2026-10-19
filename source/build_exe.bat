@echo off
REM ============================================================
REM  KHDownloader —— 一键打包脚本（Windows）
REM ------------------------------------------------------------
REM  作用：把 khdownloader_gui.py 打成单文件、无控制台的
REM        图形界面 EXE，输出到 dist\KHDownloader.exe
REM        （无阶段尾号；阶段编号只保留在 Brief/Report/内部工具里）
REM
REM  图标：assets\KHDownloader.ico
REM        - 编进 EXE 的 PE 资源表 → 资源管理器 / 快捷方式 / 任务栏
REM        - 同时作为运行数据打进包 → 窗口标题栏图标运行时从
REM          sys._MEIPASS\assets\ 读
REM        assets\KHDownloader.png 一并打包，作为 Tk 兜底与后续维护用
REM
REM  构建方式：走 KHDownloader.spec。
REM        命令行上的 --name / --icon 不会覆盖 spec 里的字段，
REM        所以脚本和 spec 必须一致 —— 否则会出现两个最终程序名。
REM
REM  前提：
REM    1) 已安装 Python 3.11+（64 位）
REM    2) 已安装运行依赖：python -m pip install -r requirements.txt
REM    3) 已安装打包工具：python -m pip install pyinstaller
REM       （PyInstaller 不进 requirements.txt，它是"打包用的开发工具"，
REM         不是程序运行时依赖）
REM
REM  用法：双击本文件，或在项目根目录运行  build_exe.bat
REM ============================================================

setlocal

REM 切到本脚本所在目录（项目根目录）
cd /d "%~dp0"

echo.
echo [1/4] 检查 Python ...
python --version
if errorlevel 1 (
    echo.
    echo [错误] 没找到 python 命令。请先安装 Python 3.11+ 并加入 PATH。
    pause
    exit /b 1
)

echo.
echo [2/4] 检查图标资源 ...
if not exist "assets\KHDownloader.ico" (
    echo.
    echo [错误] 缺少 assets\KHDownloader.ico
    echo        没有真实图标文件时不打包，避免产出"看着像有图标、
    echo        实际还是旧默认图标"的程序。
    pause
    exit /b 1
)
if not exist "assets\KHDownloader.png" (
    echo.
    echo [错误] 缺少 assets\KHDownloader.png
    pause
    exit /b 1
)
echo     assets\KHDownloader.ico  OK
echo     assets\KHDownloader.png  OK

echo.
echo [3/4] 检查 PyInstaller ...
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [提示] 未安装 PyInstaller，正在安装 ...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo.
        echo [错误] PyInstaller 安装失败，请手动执行：python -m pip install pyinstaller
        pause
        exit /b 1
    )
)

echo.
echo [4/4] 开始打包（走 KHDownloader.spec）...
REM 走 tools\build_frozen.py，不直接调 PyInstaller：它替我们处理两件事 ——
REM   1) 环境里若躺着 pathlib / typing / enum34 这类"标准库的过时 backport"包，
REM      PyInstaller 会直接拒绝构建（报 obsolete backport）。驱动会在构建期间
REM      临时把这类包挪开、构建完再放回去，用户的 Python 环境一个字节都不动；
REM   2) 打包完**开包验收** curl_cffi 与 libcurl 是否真的在包里 ——
REM      少了它，EXE 会静默退回普通请求，用户看到的还是"被人机校验拦住"。
REM 想额外让产出的 EXE 真连一次站点自检（会联网，约十几秒）：build_exe.bat --selftest
python "%~dp0tools\build_frozen.py" %*
if errorlevel 1 (
    echo.
    echo [错误] 打包失败，请看上面的输出。
    pause
    exit /b 1
)

if not exist "dist\KHDownloader.exe" (
    echo.
    echo [错误] 打包命令返回成功但没有产出 dist\KHDownloader.exe，请检查 spec。
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  打包完成！
echo  产物： %cd%\dist\KHDownloader.exe
echo  它是一个单文件、无控制台的图形界面程序，可在没有装
echo  Python 的 64 位 Windows 上直接双击运行。
echo.
echo  改名旧的 EXE 不等于完成要求 —— 改动必须重新构建才会进包。
echo  包内已验收：curl_cffi + libcurl（换浏览器指纹那一层）。
echo  想让它真连一次站点自检：  build_exe.bat --selftest
echo  （自检报告默认写到 dist\selftest_transport.txt）
echo ============================================================
echo.
pause
endlocal