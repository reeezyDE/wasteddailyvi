#!/bin/sh
# WastedDailyVI – Ersteinrichtung unter Linux / WSL (im Projektordner):  sh scripts/setup.sh
set -e
cd "$(dirname "$0")/.."

command -v docker >/dev/null 2>&1 || { echo "Docker fehlt – siehe README (Installation)."; exit 1; }
[ -f .env ] || { cp .env.example .env; chmod 600 .env; echo ".env angelegt – bitte ausfüllen."; }
# Container-Benutzer = dein Benutzer, damit dir data/ und claude-home/ gehören (sonst nur mit sudo änderbar)
if ! grep -q '^APP_UID=' .env; then
    uid="$(id -u)"
    [ "$uid" = "0" ] && uid=1000   # als root ausgeführt: Standardbenutzer
    printf '\n# Benutzer-ID für das Docker-Image (von scripts/setup.sh eingetragen, siehe .env.example)\nAPP_UID=%s\n' \
        "$uid" >> .env
    echo "APP_UID=$uid in .env eingetragen."
fi
if [ -d config.yaml ]; then
    echo "config.yaml ist ein Ordner (von Docker angelegt) – wird entfernt."
    rmdir config.yaml 2>/dev/null || rm -r config.yaml
fi
[ -f config.yaml ] || { cp config.example.yaml config.yaml; echo "config.yaml angelegt."; }
mkdir -p data claude-home

echo "==> Baue das Docker-Image (beim ersten Mal 10–20 Minuten) …"
docker compose build
docker compose run --rm wdvi wdvi init
docker compose run --rm wdvi wdvi doctor || true
echo
echo "Wenn alles passt: 'docker compose up -d' und dann http://localhost:8420 öffnen."
