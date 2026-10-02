@echo off
REM Starts InPro Copilot at http://localhost:8000 (restarts itself when the code changes)
cd /d "%~dp0"
set PYTHONPATH=src
set PYTHONUTF8=1
set "PY=python"
if exist "..\.venv\Scripts\python.exe" set "PY=..\.venv\Scripts\python.exe"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
"%PY%" -m uvicorn inpro_copilot.api:app --port 8000 --reload --reload-dir src
