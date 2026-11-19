# Gera o executável dist\LeitorPDF.exe (arquivo único, sem precisar de Python).
#
# Uso (PowerShell, na pasta do projeto):
#     .\build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) { python -m venv .venv }
$py = ".venv\Scripts\python.exe"
& $py -m pip install -q -r requirements.txt -r requirements-build.txt

& $py -m PyInstaller --noconfirm --clean LeitorPDF.spec
if ($LASTEXITCODE -ne 0) { throw "Falha no build" }

$size = [math]::Round((Get-Item dist\LeitorPDF.exe).Length / 1MB, 1)
Write-Host "`nPronto: dist\LeitorPDF.exe ($size MB)" -ForegroundColor Green
