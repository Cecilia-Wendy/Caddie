$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDir = Split-Path -Parent $ScriptDir
$Python = Join-Path $AppDir ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "Missing .venv. Run: py -3.12 -m venv .venv"
}

Set-Location $AppDir
$env:PYINSTALLER_CONFIG_DIR = Join-Path $AppDir "build\pyinstaller-config"
$Version = (& $Python -c "from app_version import APP_VERSION; print(APP_VERSION)").Trim()

& $Python "scripts\create_app_icon.py"
& $Python -m PyInstaller --noconfirm --clean "caddie-windows.spec"

$ReleaseDir = Join-Path $AppDir "dist\Caddie-Windows"
$ZipPath = Join-Path $AppDir "dist\Caddie-$Version-windows-x64.zip"

if (Test-Path $ReleaseDir) { Remove-Item $ReleaseDir -Recurse -Force }
New-Item -ItemType Directory -Path $ReleaseDir | Out-Null
Copy-Item (Join-Path $AppDir "dist\Caddie\*") $ReleaseDir -Recurse
Copy-Item (Join-Path $AppDir "docs\WINDOWS-测试版使用说明.txt") $ReleaseDir

if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
Compress-Archive -Path $ReleaseDir -DestinationPath $ZipPath -CompressionLevel Optimal

Write-Host ""
Write-Host "Built: $ReleaseDir\Caddie.exe"
Write-Host "Share: $ZipPath"
Write-Host "Data will remain in: $env:USERPROFILE\.caddie"
