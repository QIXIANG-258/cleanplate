@echo off
chcp 936 >nul 2>&1
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" goto not_installed

echo.
echo   CleanPlate 正在启动，浏览器会自动打开。
echo   关闭这个窗口即停止服务。
echo.

".venv\Scripts\python.exe" start.py serve --open
pause
exit /b 0

:not_installed
echo.
echo   [错误] 还没有安装依赖，请先运行 scripts\setup.bat
echo.
pause
exit /b 1
