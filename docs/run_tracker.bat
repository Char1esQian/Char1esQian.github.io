@echo off
setlocal
cd /d "%~dp0"

rem ============================================================
rem  Polestar 4 inventory tracker - one-click runner
rem  Double-click: snapshot + report, then waits for a keypress.
rem  From a terminal: pass extra args through, e.g.
rem     run_tracker.bat --states NJ,NY,PA,CT
rem ============================================================

where python >nul 2>nul
if errorlevel 1 (
  echo [!] Python not found in PATH.
  echo     Install Python 3.10+ from https://www.python.org/downloads/
  echo     and tick "Add python.exe to PATH" during setup.
  call :maybe_pause
  exit /b 1
)

echo === Polestar 4 inventory snapshot ===
python polestar_tracker.py snapshot --log-file tracker.log %*
if errorlevel 1 goto fail

echo.
python polestar_tracker.py report
if errorlevel 1 goto fail

echo.
echo OK. Database: polestar4_inventory.sqlite3   Log: tracker.log
call :maybe_pause
exit /b 0

:fail
echo.
echo [!] Tracker failed - check tracker.log, or run:
echo     python polestar_tracker.py snapshot -v
call :maybe_pause
exit /b 1

:maybe_pause
rem pause only when launched by double-click, not from a terminal/cron
echo %cmdcmdline% | find /i "%~f0" >nul && pause
goto :eof
