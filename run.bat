@echo off
cd /d "%~dp0"
where py >nul 2>&1 && (
    py -3 sysinfo.pyw %*
    exit /b %ERRORLEVEL%
)
python sysinfo.pyw %*
