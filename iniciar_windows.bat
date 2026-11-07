@echo off
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 exit /b %errorlevel%
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 exit /b %errorlevel%
.venv\Scripts\python.exe app.py
pause
