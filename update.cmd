@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "ARGS=%*"
set "ACTION=--apply"
:scan_args
if "%~1"=="" goto run
if /I "%~1"=="--dry-run" set "ACTION="
shift
goto scan_args
:run
if defined ACTION (call "%~dp0install.cmd" %ACTION% %ARGS%) else (call "%~dp0install.cmd" %ARGS%)
exit /b %errorlevel%
