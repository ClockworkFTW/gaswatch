@echo off
REM Weekly heavy pull - re-fetches multi-MB tariff PDFs and parses rate values.
cd /d "%~dp0"
set "GASWATCH_LOG=data\pull.log"
echo === gaswatch rates pull  %date% %time% ===
.venv\Scripts\gaswatch.exe pull-all --include-heavy
.venv\Scripts\gaswatch.exe export-powerbi
echo === finished  %date% %time% ===
