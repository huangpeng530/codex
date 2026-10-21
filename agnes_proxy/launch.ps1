# Agnes codex proxy launcher (port 8787).
# Path-independent: resolves proxy.py and python relative to this script,
# so the whole folder can live anywhere (reinstall-friendly).
$ErrorActionPreference = "SilentlyContinue"
$here = $PSScriptRoot

function Find-Python {
    if ($env:AGNES_PROXY_PYTHON -and (Test-Path $env:AGNES_PROXY_PYTHON)) { return $env:AGNES_PROXY_PYTHON }
    $onPath = (Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    if ($onPath) { return $onPath }
    $machine = Get-ChildItem "C:\Python3*\python.exe" -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
    if ($machine) { return $machine.FullName }
    $local = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
    if ($local) { return $local.FullName }
    return $null
}

# Double-start guard: proxy already up -> nothing to do.
if (Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue) { exit 0 }

$python = Find-Python
if (-not $python) { exit 1 }

$proxy = Join-Path $here "proxy.py"
Start-Process -FilePath $python -ArgumentList "`"$proxy`"" -WorkingDirectory $here -WindowStyle Hidden
exit 0
