$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

python -m pip install --disable-pip-version-check -r requirements.txt
python -m PyInstaller --noconfirm --clean AgentTokenMonitor.spec

Write-Host "Built: $PSScriptRoot\dist\AgentTokenMonitor.exe" -ForegroundColor Green
