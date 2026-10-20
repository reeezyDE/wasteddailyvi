#!/bin/sh
# Sicherung von Datenbank, Konfiguration und OAuth-Tokens nach backups/wdvi-<Datum>.tar.gz
# (fertige Videos werden NICHT gesichert – die sind groß und lassen sich neu erzeugen).
# Aufruf im Projektordner:  sh scripts/backup.sh       Wiederherstellen: docs/BETRIEB.md -> "Sicherung"
set -e
cd "$(dirname "$0")/.."

if [ ! -f data/wdvi.db ]; then
    echo "Keine Datenbank gefunden (data/wdvi.db) – nichts zu sichern."
    exit 0
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p backups data/backup
DB="data/backup/wdvi-$STAMP.db"
# Konsistente Kopie über die SQLite-Backup-Schnittstelle (berücksichtigt auch data/wdvi.db-wal)
PYBACKUP="import sqlite3, sys; s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2]); \
s.backup(d); d.close(); s.close()"

if docker compose exec -T wdvi python -c "$PYBACKUP" data/wdvi.db "$DB" 2>/dev/null; then
    :   # Container läuft
elif command -v python3 >/dev/null 2>&1 && python3 -c "$PYBACKUP" data/wdvi.db "$DB" 2>/dev/null; then
    :   # Container gestoppt, Python auf dem PC vorhanden
elif docker compose run --rm --no-deps -T wdvi python -c "$PYBACKUP" data/wdvi.db "$DB" 2>/dev/null; then
    :   # Container gestoppt, Python nur im Image
else
    # letzter Ausweg: Dateien direkt kopieren – inkl. Write-Ahead-Log, sonst fehlen die letzten Änderungen
    echo "WARNUNG: Konsistente Sicherung nicht möglich – kopiere die Datenbank-Dateien direkt."
    rm -f "$DB"
    cp data/wdvi.db "$DB"
    [ -f data/wdvi.db-wal ] && cp data/wdvi.db-wal "$DB-wal"
fi

FILES="$DB"
[ -f "$DB-wal" ] && FILES="$FILES $DB-wal"
[ -f config.yaml ] && FILES="$FILES config.yaml"
[ -f .env ] && FILES="$FILES .env"
[ -d data/tokens ] && FILES="$FILES data/tokens"
[ -f data/client_secret.json ] && FILES="$FILES data/client_secret.json"

# shellcheck disable=SC2086
tar -czf "backups/wdvi-$STAMP.tar.gz" $FILES
rm -f "$DB" "$DB-wal"
chmod 600 "backups/wdvi-$STAMP.tar.gz" 2>/dev/null || true
echo "Sicherung: backups/wdvi-$STAMP.tar.gz (enthält Secrets – sicher aufbewahren!)"
