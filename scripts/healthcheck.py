"""Docker-Healthcheck (läuft im Container, keine Abhängigkeiten außer der Standardbibliothek).

Gesund heißt:
  1. das Dashboard antwortet (200, oder 401 = Passwortschutz aktiv – die App selbst hat geantwortet), und
  2. die Hintergrund-Jobs laufen: die Datenbank ist lesbar und der Minuten-Job "schedule"/"publish" ist in
     den letzten 15 Minuten gestartet (sonst hängt der Scheduler oder die Datenbank).
Exit-Code 0 = gesund, 1 = krank. Docker zeigt das in 'docker compose ps' als (healthy)/(unhealthy).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import urllib.error
import urllib.request

PORT = os.environ.get("WDVI_PORT", "8420")
DB = os.environ.get("WDVI_HEALTH_DB", "/app/data/wdvi.db")
MAX_JOB_AGE_MIN = 15


def _dashboard_ok() -> str | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=8) as r:
            return None if r.status == 200 else f"Dashboard antwortet mit HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return None if e.code == 401 else f"Dashboard antwortet mit HTTP {e.code}"
    except Exception as e:  # Timeout, Verbindung abgelehnt, ...
        return f"Dashboard nicht erreichbar: {e}"


def _jobs_ok() -> str | None:
    if not os.path.exists(DB):
        return f"Datenbank {DB} fehlt"
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
        try:
            (n,) = con.execute(
                "SELECT count(*) FROM jobrun WHERE job IN ('schedule', 'publish') "
                "AND started_at > datetime('now', ?)",
                (f"-{MAX_JOB_AGE_MIN} minutes",),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        return f"Datenbank nicht lesbar: {e}"
    return None if n else f"Hintergrund-Jobs seit {MAX_JOB_AGE_MIN} min nicht gelaufen (Scheduler hängt?)"


def main() -> int:
    problems = [p for p in (_dashboard_ok(), _jobs_ok()) if p]
    if problems:
        print("; ".join(problems))
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
