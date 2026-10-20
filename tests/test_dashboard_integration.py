"""Dashboard mit den echten Publisher-Klassen (nur deren öffentliche Schnittstelle, kein Netzwerk)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from tests.test_dashboard_support import PASSWORD, FakePipeline, add_clip, make_client
from wdvi.dashboard.app import create_app


def _app(settings, **secrets):
    settings.secrets.dashboard_password = PASSWORD
    for k, v in secrets.items():
        setattr(settings.secrets, k, v)
    settings.config.publish.tiktok.enabled = True
    settings.config.publish.instagram.enabled = True
    # publishers=None -> Dashboard baut die echten Publisher aus der Konfiguration
    return create_app(settings, pipeline=FakePipeline(), publishers=None)


def test_connections_with_real_unconfigured_publishers(settings, db):
    c = make_client(_app(settings))
    html = c.get("/connections").text
    assert html.count("● Nicht eingerichtet") == 3
    assert "WDVI_GOOGLE_CLIENT_SECRETS_FILE" in html
    assert "/oauth/tiktok/callback" in html  # Redirect-URI des echten Publishers
    r = c.get("/oauth/tiktok/start", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/connections?flash=")
    assert "Anmeldung kann nicht starten" in c.get(r.headers["location"]).text
    r = c.get("/oauth/youtube/start")
    assert "Anmeldung kann nicht starten" in r.text


def test_real_tiktok_publisher_start_redirects_with_state(settings, db):
    c = make_client(_app(settings, tiktok_client_key="ck_test", tiktok_client_secret="cs_test"))
    html = c.get("/connections").text
    assert "cs_test" not in html
    r = c.get("/oauth/tiktok/start", follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("https://")
    state = parse_qs(urlsplit(loc).query)["state"][0]
    assert len(state) >= 30
    # falscher state wird abgewiesen, bevor der Publisher irgendetwas tauscht
    r = c.post("/oauth/tiktok/paste", data={"url": "https://localhost:8420/oauth/tiktok/callback?code=x&state=bad"},
               headers={"X-WDVI-Ajax": "1"})
    assert r.status_code == 400 and "abgelaufen" in r.json()["message"]


def test_review_platforms_follow_real_publishers(settings, db):
    c = make_client(_app(settings))
    add_clip(None)
    html = c.get("/").text
    for p in ("youtube", "tiktok", "instagram"):
        assert f'name="platforms" value="{p}"' in html


def test_empty_later_page_redirects_to_first(settings, db):
    c = make_client(_app(settings))
    r = c.get("/?page=5", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
