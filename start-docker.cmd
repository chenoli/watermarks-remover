@echo off
rem One-command Docker start for the watermarks-remover service: starts Docker
rem Desktop if needed, runs the provider setup on first use, builds and starts
rem the wr-core container, waits for /health. Needs only Docker Desktop.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0service\scripts\start_docker.ps1" %*
set "rc=%ERRORLEVEL%"
rem Keep the window open when double-clicked so the result stays visible.
if not "%WR_NO_PAUSE%"=="1" pause
exit /b %rc%
