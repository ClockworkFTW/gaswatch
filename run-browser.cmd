@echo off
REM Nightly pull including Ruby (Playwright/Chromium).
cd /d "%~dp0"
.venv\Scripts\gaswatch.exe pull-all --include-browser >> data\pull.log 2>&1
.venv\Scripts\gaswatch.exe dashboard                  >> data\pull.log 2>&1
