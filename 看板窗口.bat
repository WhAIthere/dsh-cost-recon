@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM Open the dashboard in a standalone window (Edge/Chrome app mode).
REM Falls back to the default browser if neither is found.
set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"

if not exist "dashboard.html" (
  echo Dashboard not found. Run  refresh  first.
  pause
  exit /b 1
)

if exist "%EDGE%" (
  start "" "%EDGE%" --app="file:///%~dp0dashboard.html"
  exit /b 0
)
if exist "%CHROME%" (
  start "" "%CHROME%" --app="file:///%~dp0dashboard.html"
  exit /b 0
)
start "" "%~dp0dashboard.html"
exit /b 0
