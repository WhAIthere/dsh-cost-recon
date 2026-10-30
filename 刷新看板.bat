@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
echo ==========================================
echo   DSH usage dashboard - refresh data
echo ==========================================
echo.
echo NOTE: close DSH first, or the last few minutes
echo       of session logs will be missing.
echo.
echo Scanning and rebuilding the dashboard...
echo.
python src\refresh.py
if errorlevel 1 goto failed
echo.
echo Done. Opening the dashboard...
start "" "%~dp0dashboard.html"
echo.
echo You can close this window now.
pause
exit /b 0
:failed
echo.
echo [x] refresh failed - please send me the error above.
pause
exit /b 1
