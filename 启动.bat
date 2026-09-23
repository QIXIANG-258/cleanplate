@echo off
chcp 936 >nul 2>&1
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto not_installed

echo.
echo   CleanPlate 正在启动，浏览器会自动打开。
echo   这个窗口就是服务本体，关掉它即停止。
echo.

".venv\Scripts\python.exe" start.py serve --open
echo.
echo   服务已停止。
pause
exit /b 0

:not_installed
echo.
echo   还没装依赖，先跑 scripts\setup.bat
echo.
pause
exit /b 1
