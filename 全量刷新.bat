@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
echo ==========================================
echo   DSH usage dashboard - FULL rebuild
echo ==========================================
echo.
echo FULL mode: rescan every session log and
echo re-fetch the official hourly data for the
echo last 14 days. It takes much longer than a
echo normal refresh (about 1-2 minutes).
echo.
echo Use this when the numbers look wrong or you
echo changed the dedup / attribution logic.
echo.
echo Starting...
echo.
python src\refresh.py --full
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
echo [x] full refresh failed - please send me the error above.
pause
exit /b 1
