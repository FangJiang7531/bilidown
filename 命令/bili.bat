@echo off
REM ============================================================
REM  BiliDown CLI launcher
REM  NOTE: keep this file ASCII-only. cmd.exe parses .bat files with
REM  the OEM codepage, so Chinese text here turns into mojibake.
REM  All Chinese help output comes from bili_cli.py (UTF-8 aware).
REM ============================================================
chcp 65001 >nul 2>nul
setlocal

set "PYCMD="
where py >nul 2>nul && set "PYCMD=py"
if not defined PYCMD (
    where python >nul 2>nul && set "PYCMD=python"
)
if not defined PYCMD (
    echo [ERROR] Python 3 not found in PATH.
    echo         Install Python 3, or use the packaged BiliDown.exe instead.
    pause
    exit /b 1
)

%PYCMD% "%~dp0bili_cli.py" %*
exit /b %ERRORLEVEL%
