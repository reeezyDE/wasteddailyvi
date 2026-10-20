# WastedDailyVI - Ersteinrichtung unter Windows (PowerShell im Projektordner ausfuehren):
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
# Hinweis: Diese Datei enthaelt absichtlich nur ASCII-Zeichen (keine Umlaute/Gedankenstriche), weil
# Windows PowerShell 5.1 Skripte ohne BOM als ANSI liest und Sonderzeichen sonst das Skript zerlegen.
# Empfohlen ist trotzdem die Einrichtung in Ubuntu (WSL) mit 'sh scripts/setup.sh' (siehe README).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "Docker wurde nicht gefunden. Bitte zuerst Docker Desktop installieren (siehe README)." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ".env angelegt - bitte ausfuellen."
}
if (Test-Path "config.yaml" -PathType Container) {
    Write-Host "config.yaml ist ein Ordner (von Docker angelegt) - wird geloescht." -ForegroundColor Yellow
    Remove-Item "config.yaml" -Recurse -Force
}
if (-not (Test-Path "config.yaml")) {
    Copy-Item "config.example.yaml" "config.yaml"
    Write-Host "config.yaml angelegt."
}
New-Item -ItemType Directory -Force -Path "data", "claude-home" | Out-Null

Write-Host "Baue das Docker-Image (beim ersten Mal 10-20 Minuten) ..."
docker compose build
if ($LASTEXITCODE -ne 0) { Write-Host "Bauen fehlgeschlagen - Ausgabe oben pruefen." -ForegroundColor Red; exit 1 }
docker compose run --rm wdvi wdvi init
docker compose run --rm wdvi wdvi doctor
Write-Host ""
Write-Host "Wenn alles passt: 'docker compose up -d' und dann http://localhost:8420 oeffnen." -ForegroundColor Green
