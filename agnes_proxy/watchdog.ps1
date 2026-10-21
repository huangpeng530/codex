# AgnesCodexProxyWatchdog: restart the proxy (port 8787) if it is down.
# Path-independent (uses $PSScriptRoot); safe to run from any checkout location.
$ErrorActionPreference = "SilentlyContinue"
$here = $PSScriptRoot

$log = Join-Path $here "watchdog.log"
if (Test-Path $log) {
    $lines = Get-Content $log
    if ($lines.Count -gt 200) { $lines | Select-Object -Last 200 | Set-Content $log }
}

$listener = Get-NetTCPConnection -LocalPort 8787 -State Listen
if (-not $listener) {
    & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File (Join-Path $here "launch.ps1")
    Start-Sleep -Seconds 3
    $after = Get-NetTCPConnection -LocalPort 8787 -State Listen
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    if ($after) {
        $newpid = ($after | Select-Object -First 1).OwningProcess
        Add-Content -Path $log -Value ("{0} proxy down, restarted ok (pid {1})" -f $stamp, $newpid)
    } else {
        Add-Content -Path $log -Value ("{0} proxy down, restart FAILED (python or proxy.py missing?)" -f $stamp)
    }
}
