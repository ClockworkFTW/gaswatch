@echo off
REM Weekly heavy pull - re-fetches multi-MB tariff PDFs and parses rate values.
cd /d "%~dp0"
.venv\Scripts\gaswatch.exe pull-all --include-heavy >> data\pull.log 2>&1
.venv\Scripts\gaswatch.exe export-powerbi           >> data\pull.log 2>&1
