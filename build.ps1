$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

python -m pip install --disable-pip-version-check -r requirements.txt
python -m PyInstaller --noconfirm --clean OpenCodeTokenMonitor.spec

Write-Host "Built: $PSScriptRoot\dist\OpenCodeTokenMonitor.exe" -ForegroundColor Green
