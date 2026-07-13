@echo off
REM Frequent pull (no browser, no heavy rate PDFs) - used by the 3x daily tasks.
REM cd to this script's own folder so it works regardless of install path.
cd /d "%~dp0"
.venv\Scripts\gaswatch.exe pull-all       >> data\pull.log 2>&1
.venv\Scripts\gaswatch.exe export-powerbi >> data\pull.log 2>&1
.venv\Scripts\gaswatch.exe alerts         >> data\alerts.log 2>&1
.venv\Scripts\gaswatch.exe dashboard      >> data\pull.log 2>&1
