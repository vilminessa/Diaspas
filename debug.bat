@echo off
rem ------------------------------------------------------------------------
rem  Diaspas - project control console (menu + single-action mode).
rem
rem  ASCII only on purpose: cmd re-reads a batch by byte offset after
rem  "chcp 65001", multibyte Cyrillic shifts that offset and fragments of
rem  lines get executed. All Russian text lives in tools/debug_ctl.py.
rem
rem      debug.bat              - menu
rem      debug.bat 5            - run item 5 and exit (scripts, CI)
rem      debug.bat B "general (ALT7).bat"  - item with an argument
rem      debug.bat I --yes      - accept a dangerous item without asking
rem ------------------------------------------------------------------------
setlocal EnableExtensions
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

if not "%~1"=="" goto dispatch

:menu
cls
python tools\debug_ctl.py menu
echo.
set "sel="
set /p "sel=choice: "
if not defined sel goto fin
if "%sel%"=="0" goto fin
python tools\debug_ctl.py %sel%
echo.
echo rc=%errorlevel%
pause
goto menu

:dispatch
rem Scripted mode: no prompts are possible, dangerous items need --yes.
rem The mode is passed as an argument, not an env var: env would be inherited
rem by child processes (pytest under the CI item would see it as its own mode).
python tools\debug_ctl.py %* --scripted
exit /b %errorlevel%

:fin
exit /b 0
