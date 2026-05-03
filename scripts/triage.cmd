@echo off
REM Launch an interactive Claude Code session inside the dragndoc container
REM and immediately run /triage. You stay attached and see every action.
REM
REM Permissions are pre-skipped (--dangerously-skip-permissions). Safe here
REM because Claude only sees what is bind-mounted into the container:
REM /workspace (this repo) and /docs (the documents tree).
setlocal

cd /d "%~dp0\.."

set "SERVICE=dragndoc"
set "PROMPT=%~1"
if "%PROMPT%"=="" set "PROMPT=/triage"

docker compose up -d %SERVICE% >nul
if errorlevel 1 exit /b %errorlevel%

docker compose exec %SERVICE% claude --dangerously-skip-permissions "%PROMPT%"
endlocal
