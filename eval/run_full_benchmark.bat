@echo off
REM Double-click: full benchmark with the AI reader (rules first, AI when needed). Takes 5-10 minutes.
REM Uses your .env keys. AI answers are cached, so running it again is almost free.
cd /d "%~dp0.."
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PY=python"
if exist "..\.venv\Scripts\python.exe" set "PY=..\.venv\Scripts\python.exe"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
echo Running the full benchmark. Please wait...
"%PY%" eval\run_benchmark.py --mode hybrid > eval\benchmark_full_log.txt 2>&1
type eval\benchmark_full_log.txt
echo.
echo Finished. This window closes in 60 seconds.
timeout /t 60 > nul
