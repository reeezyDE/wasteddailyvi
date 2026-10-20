"""Dashboard: Verbindungen, OAuth-Start/Callback, Redirect-URL einfügen, Instagram-Token, state-Prüfung."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from tests.test_dashboard_support import (  # noqa: F401  (Fixture)
    FakePipeline,
    FakePublisher,
    dash_fixture,
    make_client,
)
from wdvi.dashboard._state import FlashStore, OAuthStates, parse_pasted_redirect
from wdvi.dashboard.app import create_app
from wdvi.publish.base import PublishError


def _start(dash, platform: str) -> str:
    r = dash.client.get(f"/oauth/{platform}/start", follow_redirects=False)
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    assert loc.startswith(f"https://auth.example.com/{platform}/authorize")
    state = parse_qs(urlsplit(loc).query)["state"][0]
    assert dash.publishers[platform].states == [state]
    return state


def _flash(dash, resp) -> str:
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/connections?flash=")
    return dash.client.get(resp.headers["location"]).text


def test_connections_page_shows_status_per_publisher(dash):
    dash.publishers["instagram"].configured = False
    html = dash.client.get("/connections").text
    assert "YouTube" in html and "TikTok" in html and "Instagram" in html
    assert "● Verbunden" in html
    assert "● Nicht verbunden" in html
    assert "● Nicht eingerichtet" in html
    assert "WDVI_META_APP_ID" in html
    assert 'href="/oauth/youtube/start"' in html and 'href="/oauth/tiktok/start"' in html
    assert 'href="/oauth/instagram/start"' not in html
    assert "https://localhost:8420/oauth/tiktok/callback" in html
    assert 'action="/oauth/tiktok/paste"' in html
    assert 'action="/oauth/instagram/token"' in html
    assert 'action="/oauth/youtube/disconnect"' in html


def test_connections_page_survives_broken_publisher(dash):
    def boom():
        raise RuntimeError("token file kaputt")

    dash.publishers["tiktok"].is_connected = boom
    html = dash.client.get("/connections").text
    assert "token file kaputt" in html


def test_oauth_start_and_callback_success(dash):
    state = _start(dash, "youtube")
    r = dash.client.get(f"/oauth/youtube/callback?code=4/abc&state={state}&scope=x%20y", follow_redirects=False)
    page = _flash(dash, r)
    assert "YouTube ist jetzt verbunden" in page
    assert dash.publishers["youtube"].callbacks == [{"code": "4/abc", "state": state, "scope": "x y"}]


def test_oauth_state_is_single_use(dash):
    state = _start(dash, "youtube")
    dash.client.get(f"/oauth/youtube/callback?code=a&state={state}")
    r = dash.client.get(f"/oauth/youtube/callback?code=b&state={state}", follow_redirects=False)
    assert "Ungültiger oder abgelaufener Anmeldevorgang" in _flash(dash, r)
    assert len(dash.publishers["youtube"].callbacks) == 1


def test_oauth_callback_rejects_unknown_missing_or_foreign_state(dash):
    tiktok_state = _start(dash, "tiktok")
    for qs in ("code=a&state=erfunden", "code=a", f"code=a&state={tiktok_state}"):
        r = dash.client.get(f"/oauth/youtube/callback?{qs}", follow_redirects=False)
        assert "Ungültiger oder abgelaufener" in _flash(dash, r), qs
    assert dash.publishers["youtube"].callbacks == []
    # der TikTok-state bleibt für TikTok gültig
    r = dash.client.get(f"/oauth/tiktok/callback?code=a&state={tiktok_state}", follow_redirects=False)
    assert "TikTok ist jetzt verbunden" in _flash(dash, r)


def test_oauth_state_expires_after_15_minutes(dash):
    states = dash.app.state.dashboard.states
    t = [1_000_000.0]
    states.clock = lambda: t[0]
    state = _start(dash, "youtube")
    t[0] += 15 * 60 + 1
    r = dash.client.get(f"/oauth/youtube/callback?code=a&state={state}", follow_redirects=False)
    assert "abgelaufen" in _flash(dash, r)
    assert dash.publishers["youtube"].callbacks == []


def test_oauth_callback_error_from_publisher_is_shown_redacted(dash):
    dash.publishers["tiktok"].callback_error = PublishError("Token-Tausch fehlgeschlagen: code=SECRETCODE ungültig")
    state = _start(dash, "tiktok")
    r = dash.client.get(f"/oauth/tiktok/callback?code=SECRETCODE&state={state}", follow_redirects=False)
    page = _flash(dash, r)
    assert "TikTok-Verbindung fehlgeschlagen" in page
    assert "SECRETCODE" not in page


def test_oauth_start_failure_is_reported(dash):
    dash.publishers["tiktok"].auth_error = PublishError("TikTok ist nicht konfiguriert: WDVI_TIKTOK_CLIENT_KEY fehlt")
    r = dash.client.get("/oauth/tiktok/start", follow_redirects=False)
    page = _flash(dash, r)
    assert "Anmeldung kann nicht starten" in page and "WDVI_TIKTOK_CLIENT_KEY" in page
    assert len(dash.app.state.dashboard.states) == 0


def test_oauth_unknown_platform_is_404(dash):
    assert dash.client.get("/oauth/myspace/start").status_code == 404
    assert dash.client.get("/oauth/myspace/callback?code=a&state=b").status_code == 404
    r = dash.client.post("/oauth/myspace/paste", data={"url": "https://x/?code=a&state=b"})
    assert r.status_code == 404


def test_paste_redirect_url_completes_connection(dash):
    state = _start(dash, "instagram")
    url = f"https://localhost:8420/oauth/instagram/callback?code=AQB123&state={state}#_"
    r = dash.client.post("/oauth/instagram/paste", data={"url": f"  {url}  "}, follow_redirects=False)
    assert "Instagram ist jetzt verbunden" in _flash(dash, r)
    assert dash.publishers["instagram"].callbacks == [{"code": "AQB123", "state": state}]


def test_paste_query_string_only_and_ajax(dash):
    state = _start(dash, "tiktok")
    r = dash.client.post("/oauth/tiktok/paste", data={"url": f"?code=xyz&scopes=video.upload&state={state}"},
                         headers={"X-WDVI-Ajax": "1"})
    assert r.json()["ok"] is True
    assert dash.publishers["tiktok"].callbacks == [{"code": "xyz", "scopes": "video.upload", "state": state}]


def test_paste_validates_state_and_code(dash):
    _start(dash, "tiktok")
    r = dash.client.post("/oauth/tiktok/paste", data={"url": "https://localhost/cb?code=a&state=falsch"},
                         follow_redirects=False)
    assert "Ungültiger oder abgelaufener" in _flash(dash, r)
    r = dash.client.post("/oauth/tiktok/paste", data={"url": "https://localhost/cb?state=abc"},
                         follow_redirects=False)
    assert "kein „code=“" in _flash(dash, r)
    r = dash.client.post("/oauth/tiktok/paste", data={"url": "hallo welt"}, follow_redirects=False)
    assert "kein „code=“" in _flash(dash, r)
    assert dash.publishers["tiktok"].callbacks == []


def test_paste_passes_provider_error_to_publisher(dash):
    dash.publishers["tiktok"].callback_error = PublishError("TikTok-Anmeldung abgebrochen: access_denied")
    state = _start(dash, "tiktok")
    r = dash.client.post("/oauth/tiktok/paste",
                         data={"url": f"https://localhost/cb?error=access_denied&state={state}"},
                         follow_redirects=False)
    assert "TikTok-Anmeldung abgebrochen" in _flash(dash, r)


def test_instagram_token_form(dash):
    r = dash.client.post("/oauth/instagram/token", data={"access_token": " IGQVJtoken ", "user_id": "178414"},
                         follow_redirects=False)
    assert "Instagram-Token gespeichert" in _flash(dash, r)
    assert dash.publishers["instagram"].callbacks == [{"access_token": "IGQVJtoken", "user_id": "178414"}]

    r = dash.client.post("/oauth/instagram/token", data={"access_token": "tok2"}, headers={"X-WDVI-Ajax": "1"})
    assert r.json()["ok"]
    assert dash.publishers["instagram"].callbacks[-1] == {"access_token": "tok2"}


def test_instagram_token_form_validation_and_errors(dash):
    r = dash.client.post("/oauth/instagram/token", data={"access_token": ""}, headers={"X-WDVI-Ajax": "1"})
    assert r.status_code == 400
    r = dash.client.post("/oauth/instagram/token", data={"access_token": "t", "user_id": "abc"},
                         headers={"X-WDVI-Ajax": "1"})
    assert r.status_code == 400 and "Ziffern" in r.json()["message"]
    dash.publishers["instagram"].callback_error = PublishError("Eingefügtes Instagram-Token wird nicht akzeptiert")
    r = dash.client.post("/oauth/instagram/token", data={"access_token": "t"}, headers={"X-WDVI-Ajax": "1"})
    assert r.status_code == 400 and "nicht akzeptiert" in r.json()["message"]


def test_instagram_token_route_404_without_instagram(settings, db):
    from tests.test_dashboard_support import PASSWORD

    settings.secrets.dashboard_password = PASSWORD
    app = create_app(settings, pipeline=FakePipeline(), publishers={"youtube": FakePublisher("youtube")})
    r = make_client(app).post("/oauth/instagram/token", data={"access_token": "t"})
    assert r.status_code == 404


def test_disconnect(dash):
    r = dash.client.post("/oauth/youtube/disconnect", follow_redirects=False)
    assert "YouTube getrennt" in _flash(dash, r)
    assert dash.publishers["youtube"].disconnected == 1


def test_callback_requires_auth(dash):
    state = _start(dash, "youtube")
    anon = make_client(dash.app, password=None)
    assert anon.get(f"/oauth/youtube/callback?code=a&state={state}").status_code == 401
    assert dash.publishers["youtube"].callbacks == []


def test_parse_pasted_redirect():
    assert parse_pasted_redirect("https://h/cb?code=a%2Fb&state=s#_") == {"code": "a/b", "state": "s"}
    assert parse_pasted_redirect("https://h/cb#access_token=t&user_id=1") == {"access_token": "t", "user_id": "1"}
    assert parse_pasted_redirect("code=x&state=y") == {"code": "x", "state": "y"}
    assert parse_pasted_redirect("?code=x") == {"code": "x"}
    assert parse_pasted_redirect("") == {}
    assert parse_pasted_redirect("http://[broken") == {}


def test_oauth_states_and_flash_store():
    t = [0.0]
    st = OAuthStates(clock=lambda: t[0])
    s1 = st.create("youtube")
    assert len(s1) >= 30
    assert not st.consume("tiktok", s1)
    assert st.consume("youtube", s1)
    assert not st.consume("youtube", s1)
    s2 = st.create("youtube")
    t[0] = 899
    assert st.consume("youtube", s2)
    s3 = st.create("youtube")
    t[0] += 900
    assert not st.consume("youtube", s3)
    for _ in range(150):
        st.create("tiktok")
    assert len(st) <= 100

    f = FlashStore(clock=lambda: t[0])
    tok = f.add("ok", "Hallo")
    assert f.pop(tok) == {"kind": "ok", "text": "Hallo"}
    assert f.pop(tok) is None
    assert f.pop(None) is None
