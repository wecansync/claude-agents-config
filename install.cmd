@echo off
setlocal EnableExtensions DisableDelayedExpansion
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 "%~dp0bin\install.py" %*
exit /b %errorlevel%
:use_python
python "%~dp0bin\install.py" %*
exit /b %errorlevel%
