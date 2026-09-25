param(
    [switch]$NoAutoStart,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'dist\AgentTokenMonitor.exe'
if (-not (Test-Path -LiteralPath $source)) {
    throw "EXE not found: $source. Run .\build.ps1 first."
}

# Remove the previous product's executable so both versions cannot run at once.
$oldInstallDir = Join-Path $env:LOCALAPPDATA 'Programs\OpenCodeTokenMonitor'
$oldExe = Join-Path $oldInstallDir 'OpenCodeTokenMonitor.exe'
Get-Process -Name 'OpenCodeTokenMonitor', 'AgentTokenMonitor' -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $oldExe) {
    Remove-Item -LiteralPath $oldExe -Force
    if (-not (Get-ChildItem -LiteralPath $oldInstallDir -Force -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $oldInstallDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$installDir = Join-Path $env:LOCALAPPDATA 'Programs\AgentTokenMonitor'
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
$target = Join-Path $installDir 'AgentTokenMonitor.exe'

# Stop both the tray and any open dashboard before replacing the executable.
$running = Get-Process -Name 'AgentTokenMonitor' -ErrorAction SilentlyContinue
if ($running) {
    $running | Stop-Process -Force
    Start-Sleep -Milliseconds 500
}
Copy-Item -LiteralPath $source -Destination $target -Force

# Carry an OpenCode-only data directory over before the app reads it.
$legacyConfigDir = Join-Path $env:LOCALAPPDATA 'OpenCodeTokenMonitor'
$configDir = Join-Path $env:LOCALAPPDATA 'AgentTokenMonitor'
$marker = Join-Path $configDir '.migrated-from-opencodetokenmonitor'
if ((Test-Path -LiteralPath $legacyConfigDir) -and -not (Test-Path -LiteralPath $marker)) {
    New-Item -ItemType Directory -Force -Path $configDir | Out-Null
    Get-ChildItem -LiteralPath $legacyConfigDir -Force | ForEach-Object {
        $destination = Join-Path $configDir $_.Name
        if (-not (Test-Path -LiteralPath $destination)) {
            Copy-Item -LiteralPath $_.FullName -Destination $destination -Recurse -Force
        }
    }
    Set-Content -LiteralPath $marker -Value ("Migrated from $legacyConfigDir on " + (Get-Date -Format s)) -Encoding utf8
    Write-Host "Migrated data from: $legacyConfigDir" -ForegroundColor Yellow
}
New-Item -ItemType Directory -Force -Path $configDir | Out-Null
$config = Join-Path $configDir 'config.json'
if (-not (Test-Path -LiteralPath $config)) {
    $example = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'config.example.json') | ConvertFrom-Json
    try {
        $db = (& opencode debug paths db 2>$null | Select-Object -Last 1).Trim()
        if ($db -and (Test-Path -LiteralPath $db -PathType Leaf)) {
            $example.opencode_db = $db
        }
    } catch {
        Write-Warning 'Could not auto-detect opencode.db; portable defaults will be used.'
    }
    $tempConfig = "$config.tmp"
    $example | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $tempConfig -Encoding utf8
    Move-Item -LiteralPath $tempConfig -Destination $config -Force
} else {
    Write-Host 'Preserved existing config.json' -ForegroundColor DarkGray
}

$webviewRoot = 'C:\Program Files (x86)\Microsoft\EdgeWebView\Application'
if (-not (Test-Path -LiteralPath $webviewRoot)) {
    Write-Warning 'Microsoft Edge WebView2 Runtime was not detected. The dashboard requires WebView2.'
}

if (-not $NoAutoStart) {
    & $target install
}

if (-not $NoStart) {
    Start-Process -FilePath $target -ArgumentList 'tray' -WindowStyle Hidden
}

Write-Host "Installed: $target" -ForegroundColor Green
Write-Host "Config:    $config" -ForegroundColor Green
Write-Host "Data:      $configDir" -ForegroundColor Green
Write-Host "Dashboard: Start-Process -FilePath '$target' -ArgumentList 'dashboard'" -ForegroundColor Cyan
