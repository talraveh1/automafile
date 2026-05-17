@echo off
REM Start the Drag'n'Doc Windows toaster via the repo venv's dnd.exe.
REM Double-clickable from Explorer. Forwards extra args to `dnd toaster start`,
REM e.g. `toaster.cmd --no-tray` or `toaster.cmd --fg`.
setlocal

set "DND=%~dp0..\.venv\Scripts\dnd.exe"
if not exist "%DND%" (
    echo dnd.exe not found at "%DND%" -- run: python scripts\install.py 1>&2
    exit /b 1
)

"%DND%" toaster start %*
endlocal
