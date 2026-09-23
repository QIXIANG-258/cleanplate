@echo off
chcp 936 >nul 2>&1
setlocal
cd /d "%~dp0.."
title CleanPlate - Setup

set "TORCH_KIND=CUDA"
if /i "%~1"=="cpu" set "TORCH_KIND=CPU"

echo.
echo ============================================================
echo   CleanPlate  环境安装
echo   PyTorch 版本: %TORCH_KIND%
echo ============================================================
echo.

if exist ".venv\Scripts\python.exe" goto have_venv

echo [1/3] 查找 Python 3.10 或更高版本 ...
set "PYCMD="
call :try_py py -3.13
call :try_py py -3.12
call :try_py py -3.11
call :try_py py -3.10
call :try_py python

if not defined PYCMD goto no_python

echo.
echo [2/3] 创建虚拟环境 .venv ...
%PYCMD% -m venv .venv
if not exist ".venv\Scripts\python.exe" goto venv_failed
goto install

:no_python
echo.
echo   [错误] 没找到 Python 3.10 或更高版本。
echo   请先安装: https://www.python.org/downloads/
echo   安装时记得勾选 "Add python.exe to PATH"。
echo.
pause
exit /b 1

:venv_failed
echo   [错误] 虚拟环境创建失败。
pause
exit /b 1

:have_venv
echo [1/3] 已存在虚拟环境 .venv，跳过创建

:install
echo.
echo [3/3] 安装依赖 ...
echo.

set "PY=.venv\Scripts\python.exe"
set "MIRROR=https://mirrors.aliyun.com/pypi/simple/"

echo   -- PyTorch ^(%TORCH_KIND%^) --
echo   包比较大，耐心等，别关窗口。
if /i "%TORCH_KIND%"=="CPU" goto torch_cpu
"%PY%" -m pip install torch==2.6.0+cu124 --index-url https://download.pytorch.org/whl/cu124
goto torch_done

:torch_cpu
"%PY%" -m pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu

:torch_done
if errorlevel 1 goto torch_warn
goto other_deps

:torch_warn
echo.
echo   [警告] PyTorch 安装失败。若是网络问题，可改用 CPU 版：
echo          scripts\setup.bat cpu
echo.

:other_deps
echo.
echo   -- 其余依赖 --
"%PY%" -m pip install -r requirements.txt -i %MIRROR%
if errorlevel 1 goto deps_failed

echo.
echo ============================================================
echo   安装完成。接下来：
echo     1) 双击 scripts\run.bat 启动界面
echo     2) 或在 VSCode 里按 F5
echo   首次启动会自动下载 LaMa 模型，约 200 MB。
echo ============================================================
echo.
pause
exit /b 0

:deps_failed
echo.
echo   [错误] 依赖安装失败。换个镜像再试，比如：
echo          ".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple
echo.
pause
exit /b 1

REM ------------------------------------------------------------
REM  Subroutine: try candidate interpreters, remember the first
REM  one that is >= 3.10.
REM  Version test is written as 3.10 > major + minor/100 to avoid
REM  parentheses -- parens are block syntax in batch and mix badly
REM  with non-ASCII text.
REM ------------------------------------------------------------
:try_py
if defined PYCMD exit /b 0
%* -c "import sys;sys.exit(3.10>sys.version_info[0]+sys.version_info[1]/100)" >nul 2>&1
if not errorlevel 1 set "PYCMD=%*"
if not defined PYCMD exit /b 0
echo        找到: %*
exit /b 0
