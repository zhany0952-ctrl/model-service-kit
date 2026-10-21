@echo off
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8
if defined MODEL_SERVICE_PYTHON goto configured
if exist "%~dp0.venv\Scripts\python.exe" goto adjacent
python -m model_service.gui %*
goto finished
:configured
"%MODEL_SERVICE_PYTHON%" -m model_service.gui %*
goto finished
:adjacent
"%~dp0.venv\Scripts\python.exe" -m model_service.gui %*
:finished
if errorlevel 1 pause
endlocal
