param(
    [switch]$NoAutoStart,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'dist\OpenCodeTokenMonitor.exe'
if (-not (Test-Path -LiteralPath $source)) {
    throw "EXE not found: $source. Run .\build.ps1 first."
}

$installDir = Join-Path $env:LOCALAPPDATA 'Programs\OpenCodeTokenMonitor'
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
$target = Join-Path $installDir 'OpenCodeTokenMonitor.exe'

# Stop both the tray and any open dashboard before replacing the executable.
$running = Get-Process -Name 'OpenCodeTokenMonitor' -ErrorAction SilentlyContinue
if ($running) {
    $running | Stop-Process -Force
    Start-Sleep -Milliseconds 500
}
Copy-Item -LiteralPath $source -Destination $target -Force

$configDir = Join-Path $env:LOCALAPPDATA 'OpenCodeTokenMonitor'
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
