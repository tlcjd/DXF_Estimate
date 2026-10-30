@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set "PY_EXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PY_EXE=%~dp0.venv\Scripts\python.exe"
if defined PY_EXE goto run_python
for /D %%D in ("%~dp0..\DXF_*") do (
  if not defined PY_EXE if exist "%%~fD\.venv\Scripts\python.exe" set "PY_EXE=%%~fD\.venv\Scripts\python.exe"
)
if defined PY_EXE goto run_python
for /D %%D in ("D:\P\Estimate\DXF_*") do (
  if not defined PY_EXE if exist "%%~fD\.venv\Scripts\python.exe" set "PY_EXE=%%~fD\.venv\Scripts\python.exe"
)
if defined PY_EXE goto run_python
where py >nul 2>nul
if not errorlevel 1 goto run_py_launcher
where python >nul 2>nul
if not errorlevel 1 goto run_system_python
echo ERROR: Python not found. Copy this PoC beside the existing DXF program folder.
pause
exit /b 1
:run_py_launcher
py -3 -u "%~dp0web_trial\server.py"
goto check_result
:run_system_python
python -u "%~dp0web_trial\server.py"
goto check_result
:run_python
"%PY_EXE%" -u "%~dp0web_trial\server.py"
:check_result
if errorlevel 1 pause
