@echo off
rem Interactive setup: choose the Layer B rewrite provider, model, API key and
rem profile. Writes .env, then optionally starts the service.
set "WIZARD=%~dp0service\scripts\setup_wizard.py"
set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE="
set "PYLAUNCH=python"
where py >nul 2>nul && set "PYLAUNCH=py -3"
if defined PYEXE (
  "%PYEXE%" "%WIZARD%" %*
) else (
  %PYLAUNCH% "%WIZARD%" %*
)
set "rc=%ERRORLEVEL%"
rem Exit code 10: saved and the user asked to start the service now.
if "%rc%"=="10" (
  call "%~dp0start-service.cmd"
  exit /b %ERRORLEVEL%
)
if not "%WR_NO_PAUSE%"=="1" pause
exit /b %rc%
