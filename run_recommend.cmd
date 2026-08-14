@echo off
setlocal
rem ==========================================================================
rem  Portable launcher - regenerates recommendations and opens recommend.html
rem  Lives in the (OneDrive-synced) project folder, so it works on ANY PC that
rem  has this folder synced and Python installed. Just double-click it.
rem ==========================================================================

rem Work from this script's own folder, wherever OneDrive synced it to.
cd /d "%~dp0"

rem Prefer the Windows "py" launcher; fall back to "python".
set "PYEXE=python"
where py >nul 2>&1 && set "PYEXE=py"

echo Generating recommendations with %PYEXE% ...
echo (this can take a little while - it queries TMDB)
echo.

%PYEXE% recommend.py --open

if errorlevel 1 (
  echo.
  echo *** recommend.py exited with an error - see the messages above. ***
  echo     Common causes: Python not installed, or state\secrets.yml missing.
  pause
)
endlocal
