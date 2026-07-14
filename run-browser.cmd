@echo off
REM Nightly pull including Ruby (Playwright/Chromium).
cd /d "%~dp0"
set "GASWATCH_LOG=data\pull.log"
echo === gaswatch browser pull  %date% %time% ===
.venv\Scripts\gaswatch.exe pull-all --include-browser
.venv\Scripts\gaswatch.exe dashboard
echo === finished  %date% %time% ===
