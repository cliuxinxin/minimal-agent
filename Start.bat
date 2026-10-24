@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 launcher.py
goto finished

:use_python
where python >nul 2>nul
if errorlevel 1 goto missing_python
python launcher.py
goto finished

:missing_python
echo Please install Python 3.11 or newer from https://www.python.org/downloads/
echo Enable "Add python.exe to PATH" when installing Python.
pause
exit /b 1

:finished
set launch_result=%errorlevel%
if not "%launch_result%"=="0" pause
exit /b %launch_result%
