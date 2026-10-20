"""Review-Dashboard (FastAPI + Jinja2, ohne JS-Framework).

Seiten: /  Review-Queue (Video-Vorschau, Score, Begründung, editierbare Titel/Beschreibungen je
Plattform, Plattform-Checkboxen, Freigeben/Ablehnen) · /scheduled · /posted (mit Metriken) ·
/candidates (Pipeline-Status & Ablehnungsgründe) · /creators (Block/Allow-Liste) · /connections
(OAuth-Verbinden je Plattform, Token-Status, "Redirect-URL einfügen"-Fallback) · /health (JobRuns,
Konfig-Check). Medien unter /media/clips/... Auth: HTTP Basic (User beliebig, Passwort
WDVI_DASHBOARD_PASSWORD); ohne Passwort nur von 127.0.0.1 erreichbar.
"""

from __future__ import annotations

from fastapi import FastAPI

from wdvi.config import Settings


def create_app(settings: Settings, pipeline=None, publishers=None) -> FastAPI:
    raise NotImplementedError
