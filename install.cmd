@echo off
setlocal EnableExtensions DisableDelayedExpansion
set ARGS=%*
if "%~1"=="" set ARGS=--apply
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 "%~dp0bin\install.py" %ARGS%
exit /b %errorlevel%
:use_python
python "%~dp0bin\install.py" %ARGS%
exit /b %errorlevel%
