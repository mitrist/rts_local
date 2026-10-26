# Локальный старт консоли: venv, пустые SQLite в data/local, python -m web_ui.
# Существующие env.txt и sqlite не перезаписываются. Старые базы прода не подключаются.
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

function Resolve-PythonLauncher {
    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -and ($cmd.Source -notmatch "WindowsApps")) {
        return $cmd.Source
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -and ($cmd.Source -notmatch "WindowsApps")) {
        return $cmd.Source
    }
    $roots = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python"),
        "C:\Program Files\Python312",
        "C:\Program Files\Python311"
    )
    foreach ($dir in $roots) {
        if (-not (Test-Path $dir)) { continue }
        $found = Get-ChildItem -Path $dir -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notmatch "\\Lib\\venv\\" } |
            Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    throw "Python не найден. Установите Python 3.12 или добавьте python.exe в PATH."
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    $launcher = Resolve-PythonLauncher
    & $launcher -m venv (Join-Path $Root ".venv")
}
& $VenvPython -m pip install -r (Join-Path $Root "requirements.txt")

$usersFile = Join-Path $Root "web_users.txt"
if (-not (Test-Path -LiteralPath $usersFile)) {
    Copy-Item -LiteralPath (Join-Path $Root "web_users.example.txt") -Destination $usersFile
}

New-Item -ItemType Directory -Force -Path (Join-Path $Root "data\local\meta") | Out-Null

$example = Join-Path $Root "deploy\local.env.example"
Get-Content -LiteralPath $example | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }
    $idx = $line.IndexOf("=")
    if ($idx -lt 1) { return }
    $key = $line.Substring(0, $idx).Trim()
    $val = $line.Substring($idx + 1).Trim()
    Set-Item -Path "Env:$key" -Value $val
}

function Resolve-RepoPath([string]$rel) {
    if ([System.IO.Path]::IsPathRooted($rel)) { return $rel }
    return (Join-Path $Root ($rel -replace "/", "\"))
}

$env:RTS_PROJECT_ROOT = $Root
$env:RTS_META_DIR = Resolve-RepoPath $env:RTS_META_DIR
$env:EIS_ANALYTICS_DB = Resolve-RepoPath $env:EIS_ANALYTICS_DB
$env:EIS_HISTORY_DB = Resolve-RepoPath $env:EIS_HISTORY_DB
if ($env:EIS_ENV_FILE) {
    $env:EIS_ENV_FILE = Resolve-RepoPath $env:EIS_ENV_FILE
}

$init = @'
from pathlib import Path
import os
from eis_analytics_db import connect
from eis_history_db import history_connect

analytics = Path(os.environ["EIS_ANALYTICS_DB"])
history = Path(os.environ["EIS_HISTORY_DB"])
if analytics.is_file():
    print("keep", analytics)
else:
    conn = connect(analytics)
    conn.close()
    print("created", analytics)
if history.is_file():
    print("keep", history)
else:
    conn = history_connect(history)
    conn.close()
    print("created", history)
'@
$init | & $VenvPython -
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Консоль: http://127.0.0.1:$($env:RTS_WEB_PORT)"
Write-Host "Analytics: $($env:EIS_ANALYTICS_DB)"
Write-Host "History: $($env:EIS_HISTORY_DB)"
& $VenvPython -m web_ui
