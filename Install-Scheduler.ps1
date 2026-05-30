# Install-Scheduler.ps1
# Registers a Windows Scheduled Task that runs crawler.py silently at logon.
# crawler.py manages its own 1-2 hour sleep loop -no repetition interval needed.
# Uses pythonw.exe so no console window appears at login.
#
# Run once in an elevated PowerShell window (Run as administrator).

# ---------------- CONFIG ----------------
$TaskName   = 'TL-WatchlistDownloader'
$WorkingDir = $PSScriptRoot
if (-not $WorkingDir) { $WorkingDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$ScriptPath = Join-Path $WorkingDir 'crawler.py'
# ----------------------------------------

# Find pythonw.exe (runs Python without a console window)
$PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) {
    throw "Python not found in PATH. Make sure Python is installed and on your PATH."
}
$PythonwExe = Join-Path (Split-Path $PythonExe) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $PythonwExe)) {
    Write-Warning "pythonw.exe not found - falling back to python.exe (a console window will appear at login)."
    $PythonwExe = $PythonExe
}

if (-not (Test-Path -LiteralPath $ScriptPath)) {
    throw "Cannot find $ScriptPath. Make sure crawler.py exists next to this installer."
}

# Remove any previous version of the task
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# Action: run pythonw.exe crawler.py silently
$action = New-ScheduledTaskAction `
    -Execute $PythonwExe `
    -Argument "`"$ScriptPath`"" `
    -WorkingDirectory $WorkingDir

# Trigger: fire once at logon -no repetition, crawler loops internally
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Polls TorrentLeech for watchlist matches. Runs hidden at logon; crawler manages its own check interval.' | Out-Null

Write-Host ""
Write-Host "Scheduled task '$TaskName' installed successfully."
Write-Host "  Trigger : At logon ($env:USERNAME)"
Write-Host "  Action  : $PythonwExe"
Write-Host "  Script  : $ScriptPath"
Write-Host "  WorkDir : $WorkingDir"
Write-Host ""
Write-Host "Start it now:   schtasks /run /tn $TaskName"
Write-Host "Stop it:        schtasks /end /tn $TaskName"
Write-Host "Remove it:      Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
