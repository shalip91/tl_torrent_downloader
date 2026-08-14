@echo off
setlocal
rem ==========================================================================
rem  ONE-TIME per-PC setup: registers a "runrec:" browser link that runs
rem  run_recommend.cmd. Do this once on each PC. No admin needed (HKCU).
rem ==========================================================================
set "LAUNCHER=%~dp0run_recommend.cmd"

reg add "HKCU\Software\Classes\runrec" /ve /d "URL:Run Recommend Protocol" /f >nul
reg add "HKCU\Software\Classes\runrec" /v "URL Protocol" /d "" /f >nul
reg add "HKCU\Software\Classes\runrec\shell\open\command" /ve /d "\"%LAUNCHER%\" \"%%1\"" /f >nul

echo.
echo Done. Registered the "runrec:" link on this PC, pointing at:
echo     %LAUNCHER%
echo.
echo Now, in Chrome, create a bookmark with this exact URL:
echo     runrec:go
echo.
echo Clicking that bookmark will regenerate recommendations and open the page.
echo (Chrome will ask permission the first time - tick "Always allow".)
pause
endlocal
