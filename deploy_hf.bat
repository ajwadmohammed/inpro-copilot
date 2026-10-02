@echo off
rem Puts InPro Copilot online (Hugging Face Space). Safe to run again after changes: it updates the same Space.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe") else (set "PY=python")
echo Installing the Hugging Face uploader (one time)...
"%PY%" -m pip install -q "huggingface_hub>=0.30"
"%PY%" deploy\hf_deploy.py --wait
echo.
pause
