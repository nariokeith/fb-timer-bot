@echo off
REM Double-click this to install the Lordnine bots on this PC.
REM
REM A .bat and not the .ps1 directly: double-clicking a PowerShell script
REM opens it in Notepad, and running one unsigned trips the default
REM execution policy with a message that reads like a virus warning to
REM anyone who has not seen it before.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows.ps1"
echo.
echo ============================================================
echo  Finished. Press any key to close this window.
echo  If something went wrong, send install-log.txt back.
echo ============================================================
pause >nul
