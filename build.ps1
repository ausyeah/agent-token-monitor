$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

python -m pip install --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed with exit code $LASTEXITCODE" }

# PyInstaller and pip both log progress to stderr. Under ErrorActionPreference
# 'Stop', PowerShell turns native stderr output into a terminating error before
# $LASTEXITCODE is ever consulted, so stderr is redirected to stdout for the
# build and only the real exit code decides success.
$ErrorActionPreference = 'Continue'
python -m PyInstaller --noconfirm --clean AgentTokenMonitor.spec 2>&1 |
    Where-Object { $_ -is [System.Management.Automation.ErrorRecord] -or $_ -is [string] } |
    ForEach-Object { "$_" } | Select-Object -Last 20
$pyInstallerExit = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($pyInstallerExit -ne 0) { throw "PyInstaller failed with exit code $pyInstallerExit" }

$exe = Join-Path $PSScriptRoot 'dist\AgentTokenMonitor.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw "Expected build output not found: $exe" }

Write-Host "Built: $exe" -ForegroundColor Green
