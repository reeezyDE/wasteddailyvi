#!/bin/sh
# WastedDailyVI aktualisieren: Sicherung, neuen Code holen, Image neu bauen (mit neuester yt-dlp-Version,
# Deno und Claude-CLI), Container neu starten, Systemcheck.
# Aufruf im Projektordner (Linux/WSL):  sh scripts/update.sh
# Aus der Windows-PowerShell:           wsl sh -c "cd ~/wasteddailyvi && sh scripts/update.sh"
set -e
cd "$(dirname "$0")/.."

echo "==> Sicherung der Datenbank"
sh scripts/backup.sh || echo "WARNUNG: Sicherung fehlgeschlagen – Update läuft trotzdem weiter."

echo "==> Neuen Code holen"
if ! git pull --ff-only; then
    echo ""
    echo "FEHLER: Der neue Code konnte nicht geholt werden."
    echo "  Meist hast du eine mitgelieferte Datei (z. B. docker-compose.yml) selbst geändert."
    echo "  Ansehen:    git status   und   git diff"
    echo "  Verwerfen:  git checkout -- <Datei>     danach erneut: sh scripts/update.sh"
    echo "  Eigene Einstellungen gehören in .env (Port, Heimnetz, CPU-Build …), config.yaml oder"
    echo "  docker-compose.override.yml – die bleiben bei Updates unangetastet."
    exit 1
fi

echo "==> Image bauen (nur geänderte Teile; beim ersten Mal 10–20 Minuten)"
# WDVI_REFRESH ändert sich bei jedem Update -> yt-dlp/Deno/Claude-CLI werden frisch installiert,
# torch & Co. bleiben im Cache. --pull holt Sicherheitsupdates des Basis-Images.
WDVI_REFRESH="$(date +%s)" docker compose build --pull

echo "==> Container neu starten (wartet ggf., bis ein laufender Upload fertig ist)"
docker compose up -d

echo "==> Systemcheck"
docker compose exec -T wdvi wdvi doctor || true
echo "Fertig. Dashboard wie gewohnt im Browser öffnen (Standard: http://localhost:8420)."
