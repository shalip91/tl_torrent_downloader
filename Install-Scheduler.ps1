# Install-Scheduler.ps1
# Registers a Windows Scheduled Task that runs crawler.py silently at logon
# AND at system boot, so it survives unattended restarts (Windows Update
# reboots, power loss, etc) without you needing to log in first.
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

# Triggers: fire at logon AND at every system boot -no repetition, crawler loops internally.
# The boot trigger is what lets it come back after an unattended restart
# (Windows Update, power blip, etc) instead of waiting for you to log in.
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$triggerBoot  = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5)

# Ask whether to let the task run even if nobody is logged in yet (needed for
# the boot trigger to actually fire before you sign in -- otherwise Windows
# just queues it until your next interactive logon, same as before).
# This requires your Windows account password; it is only held in memory for
# this Register-ScheduledTask call and is never written to disk.
$runHeadless = Read-Host "Allow this task to run before you log in, e.g. right after an unattended restart? [Y/n]"

if ($runHeadless -notmatch '^[Nn]') {
    $securePwd = Read-Host "Enter your Windows account password (for $env:USERNAME)" -AsSecureString
    $bstr      = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePwd)
    $plainPwd  = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

    $principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Password `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger @($triggerLogon, $triggerBoot) `
        -Settings $settings `
        -Principal $principal `
        -User $env:USERNAME `
        -Password $plainPwd `
        -Description 'Polls TorrentLeech for watchlist matches. Runs hidden at logon and at boot (even if not logged in); crawler manages its own check interval.' | Out-Null

    $plainPwd = $null  # drop the plaintext copy as soon as we're done with it
    $modeDesc = "At logon + at startup (runs even before you log in)"
} else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Interactive `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger @($triggerLogon, $triggerBoot) `
        -Settings $settings `
        -Principal $principal `
        -Description 'Polls TorrentLeech for watchlist matches. Runs hidden at logon; crawler manages its own check interval.' | Out-Null

    $modeDesc = "At logon + at startup (still waits for your next logon after a restart)"
}

Write-Host ""
Write-Host "Scheduled task '$TaskName' installed successfully."
Write-Host "  Trigger : $modeDesc"
Write-Host "  Action  : $PythonwExe"
Write-Host "  Script  : $ScriptPath"
Write-Host "  WorkDir : $WorkingDir"
Write-Host ""
Write-Host "Start it now:   schtasks /run /tn $TaskName"
Write-Host "Stop it:        schtasks /end /tn $TaskName"
Write-Host "Remove it:      Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
