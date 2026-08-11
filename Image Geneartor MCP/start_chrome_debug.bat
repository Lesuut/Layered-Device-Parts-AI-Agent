@echo off
rem ASCII ONLY - non-ASCII text breaks the cmd parser when chcp is used.
rem Launches a SEPARATE Chrome with CDP debug port. Your normal Chrome can stay open.
cd /d "%~dp0"

set PY=
where python >nul 2>&1 && set PY=python
if not defined PY where py >nul 2>&1 && set PY=py
if not defined PY (
    echo [X] Python not found in PATH.
    pause
    exit /b 1
)

%PY% "%~dp0run.py" --setup-only
pause
