@echo off
rem One-command local start for the watermarks-remover service.
rem Installs what is missing, starts Ollama and the server, waits for /health.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0service\scripts\start_service.ps1" %*
set "rc=%ERRORLEVEL%"
rem Keep the window open when double-clicked so the result stays visible.
if not "%WR_NO_PAUSE%"=="1" pause
exit /b %rc%
