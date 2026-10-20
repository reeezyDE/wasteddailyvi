#!/bin/sh
# Docker-Entrypoint. Normalfall: Container läuft als Benutzer "app" -> Schreibrechte prüfen, Programm starten.
# Wird der Container als root gestartet (docker run --user root …), werden die Rechte der gemounteten
# Ordner (./data, ./claude-home) gerichtet und das Programm trotzdem als "app" gestartet.
set -e

APP_USER=app

if [ "$(id -u)" = "0" ]; then
    APP_UID="$(id -u "$APP_USER")"
    APP_GID="$(id -g "$APP_USER")"
    for d in /app/data /home/app/.claude; do
        mkdir -p "$d"
        # nur wenn nötig (z. B. Ordner wurde von Docker als root angelegt) – bei großen data/-Ordnern schneller
        if [ "$(stat -c %u "$d")" != "$APP_UID" ]; then
            chown -R "$APP_UID:$APP_GID" "$d" 2>/dev/null || true
        fi
    done
    export HOME=/home/app USER=app LOGNAME=app
    exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --clear-groups -- "$@"
fi

for d in /app/data /home/app/.claude; do
    if [ -d "$d" ] && [ ! -w "$d" ]; then
        echo "FEHLER: $d ist nicht beschreibbar (gehört $(stat -c %u:%g "$d"), Container läuft als $(id -u):$(id -g))." >&2
        echo "  Lösung (Linux/WSL, im Projektordner): sudo chown -R $(id -u):$(id -g) data claude-home" >&2
    fi
done

exec "$@"
