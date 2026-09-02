@echo off
setlocal
cd /d "%~dp0"

rem ============================================================
rem  Publish inventory + site to GitHub Pages
rem  One-time setup (see README "GitHub Pages" section), then
rem  just double-click (or schedule) this after each snapshot.
rem ============================================================

where git >nul 2>nul
if errorlevel 1 (
  echo [!] git not found. Install Git for Windows and re-open the terminal.
  call :maybe_pause
  exit /b 1
)

if not exist .git (
  echo [!] This folder is not a git repository yet. One-time setup:
  echo.
  echo     1. Create an EMPTY repo on GitHub named   yourname.github.io
  echo     2. Run these commands here:
  echo          git init
  echo          git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
  echo          git branch -M main
  echo     3. Run this script again.
  call :maybe_pause
  exit /b 1
)

echo === Exporting latest snapshot to docs\data.json ===
python export_site_data.py
if errorlevel 1 (
  echo [!] export_site_data.py failed - run the snapshot first:
  echo     run_tracker.bat
  call :maybe_pause
  exit /b 1
)

echo === Copying tool files into docs\ ===
copy /y polestar_tracker.py docs\ >nul
copy /y run_tracker.bat docs\ >nul
copy /y README_polestar_tracker.md docs\ >nul

git add -A
git diff --cached --quiet
if errorlevel 1 (
  git commit -m "Update Polestar 4 inventory site (%DATE% %TIME%)"
) else (
  echo Nothing new to commit.
)

echo === Pushing to GitHub ===
git push -u origin HEAD
if errorlevel 1 (
  echo [!] Push failed. Check your remote: git remote -v
  call :maybe_pause
  exit /b 1
)

echo.
echo Done. If this is the first push, enable Pages once:
echo   GitHub repo - Settings - Pages - Source "Deploy from a branch"
echo   - Branch: main - Folder: /docs - Save
echo Site will appear at https://YOUR_USER.github.io/
call :maybe_pause
exit /b 0

:maybe_pause
rem pause only when launched by double-click
echo %cmdcmdline% | find /i "%~f0" >nul && pause
goto :eof
