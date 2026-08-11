@echo off
rem ASCII ONLY - non-ASCII text here breaks the cmd parser. All Russian text lives in run.py
cd /d "%~dp0"
title Google Flow - Image Generator

echo ============================================================
echo   Google Flow - Image Generator
echo   This window stays open. Errors are shown below.
echo ============================================================
echo.

set PY=
where python >nul 2>&1 && set PY=python
if not defined PY where py >nul 2>&1 && set PY=py
if not defined PY (
    echo [X] Python not found in PATH.
    echo     Install Python 3.10+ from python.org with "Add to PATH" checked.
    echo.
    pause
    exit /b 1
)

%PY% "%~dp0run.py" %*
set RC=%errorlevel%

echo.
echo ============================================================
echo   Finished. Exit code: %RC%
echo ============================================================
pause
