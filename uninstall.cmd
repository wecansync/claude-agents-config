@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "ARGS=%*"
set "ACTION=--apply"
:scan_args
if "%~1"=="" goto run
if /I "%~1"=="--dry-run" set "ACTION="
if /I "%~1"=="--check" set "ACTION="
shift
goto scan_args
:run
if defined ACTION (call "%~dp0install.cmd" --uninstall --apply %ARGS%) else (call "%~dp0install.cmd" --uninstall %ARGS%)
exit /b %errorlevel%
