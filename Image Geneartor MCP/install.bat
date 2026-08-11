@echo off
rem ASCII ONLY - non-ASCII text breaks the cmd parser.
rem Installs: python deps, MCP server registration, skill copy.
cd /d "%~dp0"
title Install flow-images MCP

echo ============================================================
echo   Installing flow-images MCP server + skill
echo ============================================================
echo.

set PY=
where python >nul 2>&1 && set PY=python
if not defined PY where py >nul 2>&1 && set PY=py
if not defined PY (
    echo [X] Python not found in PATH.
    pause
    exit /b 1
)

echo [1/3] Python packages...
%PY% -m pip install --quiet playwright mcp
if errorlevel 1 (
    echo [X] pip install failed.
    pause
    exit /b 1
)
echo       OK

echo [2/3] Copying skill to %USERPROFILE%\.claude\skills\flow-image-gen ...
if not exist "%USERPROFILE%\.claude\skills" mkdir "%USERPROFILE%\.claude\skills"
robocopy "%~dp0skills\flow-image-gen" "%USERPROFILE%\.claude\skills\flow-image-gen" /E /NFL /NDL /NJH /NJS /nc /ns /np >nul
if errorlevel 8 (
    echo [X] Skill copy failed.
    pause
    exit /b 1
)
echo       OK

echo [3/3] Registering MCP server in Claude Code (user scope)...
where claude >nul 2>&1
if errorlevel 1 (
    echo       [!] claude CLI not found - register manually:
    echo           claude mcp add flow-images --scope user -- python "%~dp0mcp_server.py"
) else (
    call claude mcp remove --scope user flow-images >nul 2>&1
    call claude mcp add --scope user flow-images -- %PY% "%~dp0mcp_server.py"
    if errorlevel 1 (
        echo       [!] Registration failed. Run manually:
        echo           claude mcp add flow-images --scope user -- python "%~dp0mcp_server.py"
    ) else (
        echo       OK
    )
)

echo.
echo ============================================================
echo   Done. Next:
echo     1. Run RUN_TEST.bat once, log into Google, open Flow project
echo     2. Restart Claude Code
echo     3. Check with: /mcp
echo ============================================================
pause
