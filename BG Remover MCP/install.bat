@echo off
rem ASCII ONLY - non-ASCII text breaks the cmd parser.
rem Installs: python deps, MCP server registration, skill copy.
cd /d "%~dp0"
title Install bg-remover MCP

echo ============================================================
echo   Installing bg-remover MCP server + skill
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

echo [1/4] Python packages (pillow, numpy, scipy, mcp)...
%PY% -m pip install --quiet pillow numpy scipy mcp
if errorlevel 1 (
    echo [X] pip install failed.
    pause
    exit /b 1
)
echo       OK

echo [2/4] Optional AI engine (rembg + onnxruntime, ~100 MB)
echo       Needed only for non-white backgrounds. Skip = white-key only.
set /p AI="      Install now? [y/N]: "
if /i "%AI%"=="y" (
    %PY% -m pip install rembg onnxruntime
    if errorlevel 1 (
        echo       [!] AI engine install failed - white engine still works.
    ) else (
        echo       OK
    )
) else (
    echo       Skipped. Install later: %PY% bg_remove.py --install-ai
)

echo [3/4] Copying skill to %USERPROFILE%\.claude\skills\bg-remove ...
if not exist "%USERPROFILE%\.claude\skills" mkdir "%USERPROFILE%\.claude\skills"
robocopy "%~dp0skills\bg-remove" "%USERPROFILE%\.claude\skills\bg-remove" /E /NFL /NDL /NJH /NJS /nc /ns /np >nul
if errorlevel 8 (
    echo [X] Skill copy failed.
    pause
    exit /b 1
)
echo       OK

echo [4/4] Registering MCP server in Claude Code (user scope)...
where claude >nul 2>&1
if errorlevel 1 (
    echo       [!] claude CLI not found - register manually:
    echo           claude mcp add bg-remover --scope user -- python "%~dp0mcp_server.py"
) else (
    call claude mcp remove --scope user bg-remover >nul 2>&1
    call claude mcp add --scope user bg-remover -- %PY% "%~dp0mcp_server.py"
    if errorlevel 1 (
        echo       [!] Registration failed. Run manually:
        echo           claude mcp add bg-remover --scope user -- python "%~dp0mcp_server.py"
    ) else (
        echo       OK
    )
)

echo.
echo ============================================================
echo   Done. Next:
echo     1. Restart Claude Code
echo     2. Check with: /mcp   (server bg-remover)
echo     3. Try:  %PY% bg_remove.py "some.jpeg" --trim
echo ============================================================
pause
