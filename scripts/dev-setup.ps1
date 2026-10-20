# WastedDailyVI - lokale Entwicklungsumgebung unter Windows OHNE Docker (fuer Claude Code / Studio-Modus).
#   powershell -ExecutionPolicy Bypass -File scripts\dev-setup.ps1
# Nur ASCII in dieser Datei (Windows PowerShell 5.1 liest Skripte ohne BOM als ANSI).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

function Need($cmd, $wingetId) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Host "Installiere $wingetId ..."
        winget install --id $wingetId -e --accept-source-agreements --accept-package-agreements
    }
}
Need "uv" "astral-sh.uv"
Need "ffmpeg" "Gyan.FFmpeg"
Need "deno" "DenoLand.Deno"
Need "git" "Git.Git"
# frisch installierte Programme sind erst nach neuem PATH sichtbar
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host ".env angelegt - Schluessel bei Bedarf eintragen." }
if (-not (Test-Path "config.yaml")) { Copy-Item "config.example.yaml" "config.yaml"; Write-Host "config.yaml angelegt." }
.venv\Scripts\wdvi.exe init
.venv\Scripts\wdvi.exe doctor
Write-Host ""
Write-Host "Fertig. Optional fuer GPU-Whisper: NVIDIA-Treiber + CUDA 12/cuDNN 9 (sonst laeuft Whisper auf der CPU)."
Write-Host "Optional Musik-Erkennung: uv pip install --python .venv\Scripts\python.exe -e "".[music]"""
Write-Host "Test: .venv\Scripts\wdvi.exe render-demo"
