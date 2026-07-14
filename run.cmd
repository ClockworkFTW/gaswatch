@echo off
REM Frequent pull (no browser, no heavy rate PDFs) - used by the 3x daily tasks.
REM cd to this script's own folder so it works regardless of install path.
cd /d "%~dp0"
REM gaswatch writes its own log via GASWATCH_LOG, so progress also shows in this
REM window instead of being redirected away into a file.
set "GASWATCH_LOG=data\pull.log"
echo === gaswatch pull  %date% %time% ===
.venv\Scripts\gaswatch.exe pull-all
.venv\Scripts\gaswatch.exe export-powerbi
.venv\Scripts\gaswatch.exe alerts >> data\alerts.log 2>&1
.venv\Scripts\gaswatch.exe dashboard
echo === finished  %date% %time% ===
