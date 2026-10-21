# Register (or unregister) the AgnesCodexProxy scheduled tasks.
# Idempotent and path-independent: re-run from any checkout location.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1             # install / refresh
#   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Unregister # remove the tasks
param([switch]$Unregister)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$tasks = @("AgnesCodexProxy", "AgnesCodexProxyWatchdog")

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdmin)) {
    Write-Host "Registering scheduled tasks needs elevation - relaunching with UAC..."
    $args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$($MyInvocation.MyCommand.Path)`"")
    if ($Unregister) { $args += "-Unregister" }
    Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -ArgumentList $args -Verb RunAs
    exit 0
}

if ($Unregister) {
    foreach ($t in $tasks) { Unregister-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue | Out-Null }
    Write-Host "Unregistered: $($tasks -join ', ')"
    exit 0
}

$pwsh = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$user = $env:USERNAME

# Non-fatal python check (launch.ps1 resolves python the same way at runtime).
$havePy = $env:AGNES_PROXY_PYTHON -or (Get-Command python -ErrorAction SilentlyContinue) -or (Get-ChildItem "C:\Python3*\python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1)
if (-not $havePy) { Write-Warning "No python found on PATH or under C:\Python3*. Set AGNES_PROXY_PYTHON before the tasks are used." }

# --- AgnesCodexProxy: start the proxy at logon ---
$proxyAction = New-ScheduledTaskAction -Execute $pwsh -Argument ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$here\launch.ps1`"") -WorkingDirectory $here
$proxyTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$proxySettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Unregister-ScheduledTask -TaskName "AgnesCodexProxy" -ErrorAction SilentlyContinue | Out-Null
Register-ScheduledTask -TaskName "AgnesCodexProxy" -Action $proxyAction -Trigger $proxyTrigger -Settings $proxySettings -User $user -Description "Agnes codex namespace-flattening proxy on 127.0.0.1:8787 (hidden, auto-start at logon)" | Out-Null

# --- AgnesCodexProxyWatchdog: check + restart every 5 minutes ---
$wdAction = New-ScheduledTaskAction -Execute $pwsh -Argument ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$here\watchdog.ps1`"") -WorkingDirectory $here
$wdTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$wdSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Unregister-ScheduledTask -TaskName "AgnesCodexProxyWatchdog" -ErrorAction SilentlyContinue | Out-Null
Register-ScheduledTask -TaskName "AgnesCodexProxyWatchdog" -Action $wdAction -Trigger $wdTrigger -Settings $wdSettings -User $user -Description "Watchdog: restarts the Agnes codex proxy within 5 minutes when port 8787 is down" | Out-Null

Write-Host "Registered tasks:"
Get-ScheduledTask $tasks | Select-Object TaskName, State | Format-Table
Write-Host ("Watchdog next run: " + (Get-ScheduledTaskInfo -TaskName "AgnesCodexProxyWatchdog").NextRunTime)
