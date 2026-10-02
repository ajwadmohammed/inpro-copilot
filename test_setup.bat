@echo off
REM Double-click to test your setup: packages, Tesseract, every AI key, then a quick accuracy benchmark.
REM Results are saved in the eval folder (setup_log.txt and benchmark_log.txt).
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PY=python"
if exist "..\.venv\Scripts\python.exe" set "PY=..\.venv\Scripts\python.exe"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
echo Checking setup...
"%PY%" check_setup.py > eval\setup_log.txt 2>&1
type eval\setup_log.txt
if "%1"=="--no-benchmark" goto end
echo.
echo Running the quick benchmark (rules + AI when needed). This takes a few minutes...
"%PY%" eval\run_benchmark.py --mode hybrid --quick > eval\benchmark_log.txt 2>&1
type eval\benchmark_log.txt
:end
echo.
echo Finished. Results are in the eval folder. This window closes in 60 seconds.
timeout /t 60 > nul
