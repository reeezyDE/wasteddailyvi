"""Dashboard: HTTP-Basic-Auth, localhost-only ohne Passwort, CSRF-Schutz, Sicherheits-Header."""

from __future__ import annotations

from starlette.datastructures import Headers

from tests.test_dashboard_support import (  # noqa: F401  (Fixture)
    ORIGIN,
    PASSWORD,
    FakePipeline,
    basic,
    dash_fixture,
    make_client,
)
from wdvi.dashboard._security import host_matches, origin_allowed
from wdvi.dashboard.app import create_app


def test_missing_credentials_get_401_with_basic_challenge(dash):
    c = make_client(dash.app, password=None)
    r = c.get("/")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic ")
    assert "Anmeldung erforderlich" in r.text


def test_wrong_password_is_rejected(dash):
    c = make_client(dash.app, password="falsch")
    assert c.get("/").status_code == 401
    assert c.get("/health").status_code == 401


def test_correct_password_with_any_username(dash):
    for user in ("admin", "egal", ""):
        r = dash.client.get("/", headers={"Authorization": basic(PASSWORD, user=user)})
        assert r.status_code == 200, user


def test_malformed_authorization_header_is_401_not_500(dash):
    c = make_client(dash.app, password=None)
    for value in ("Basic", "Basic !!!nope!!!", "Bearer abc", "Basic " + "YWJj"):  # 'abc' ohne Doppelpunkt
        assert c.get("/", headers={"Authorization": value}).status_code == 401


def test_password_also_protects_media_and_post_routes(dash):
    (dash.clips_dir / "x.mp4").write_bytes(b"data")
    anon = make_client(dash.app, password=None)
    assert anon.get("/media/clips/x.mp4").status_code == 401
    assert anon.post("/creators", data={"platform_label": "Twitch", "handle": "a", "rule": "block"}).status_code == 401
    assert dash.client.get("/media/clips/x.mp4").status_code == 200


def test_static_assets_are_public(dash):
    anon = make_client(dash.app, password=None, client=("203.0.113.9", 1234))
    r = anon.get("/static/app.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]
    assert anon.get("/static/icon-180.png").status_code == 200


def test_no_password_allows_only_direct_localhost(settings, db):
    settings.secrets.dashboard_password = None
    app = create_app(settings, pipeline=FakePipeline(), publishers={})
    assert make_client(app, password=None).get("/").status_code == 200
    assert make_client(app, password=None, client=("127.0.0.1", 4000)).get("/").status_code == 200
    assert make_client(app, password=None, client=("::1", 4000)).get("/").status_code == 200

    remote = make_client(app, password=None, client=("192.168.1.50", 5555))
    r = remote.get("/")
    assert r.status_code == 403
    assert "WDVI_DASHBOARD_PASSWORD" in r.text
    # Docker-Bridge sieht aus wie ein fremder Host -> ebenfalls gesperrt
    assert make_client(app, password=None, client=("172.17.0.1", 1)).get("/health").status_code == 403


def test_no_password_behind_reverse_proxy_is_refused(settings, db):
    settings.secrets.dashboard_password = None
    app = create_app(settings, pipeline=FakePipeline(), publishers={})
    c = make_client(app, password=None, client=("127.0.0.1", 4000))
    for header in ("X-Forwarded-For", "X-Real-IP", "Forwarded", "CF-Connecting-IP"):
        r = c.get("/", headers={header: "198.51.100.7"})
        assert r.status_code == 403, header


def test_remote_client_with_password_is_allowed(dash):
    c = make_client(dash.app, client=("100.64.0.12", 5000))  # z. B. Handy über Tailscale
    assert c.get("/").status_code == 200


def test_too_many_wrong_passwords_get_429(dash):
    for i in range(10):  # Rateversuche: jedes Mal ein anderes Passwort
        bad = make_client(dash.app, password=f"rate-{i}", client=("10.0.0.9", 1))
        assert bad.get("/").status_code == 401
    r = bad.get("/")
    assert r.status_code == 429
    assert 500 <= int(r.headers["Retry-After"]) <= 600
    assert "10 Minuten" in r.text and "Docker" in r.text
    # auch mit richtigem Passwort vorübergehend gesperrt, andere Adressen nicht
    assert make_client(dash.app, client=("10.0.0.9", 1)).get("/").status_code == 429
    assert make_client(dash.app, client=("10.0.0.10", 1)).get("/").status_code == 200


def test_stale_saved_password_does_not_lock_out_shared_address(dash):
    # Docker: PC und Handy kommen über dieselbe Gateway-Adresse; das Handy hat ein altes Passwort gespeichert
    gateway = ("172.17.0.1", 40000)
    phone = make_client(dash.app, password="altes-passwort", client=gateway)
    for _ in range(50):
        assert phone.get("/").status_code == 401
    assert make_client(dash.app, client=gateway).get("/").status_code == 200


def test_lockout_expires_after_window(settings, db):
    from wdvi.dashboard._security import SecurityMiddleware

    now = [1000.0]
    settings.secrets.dashboard_password = PASSWORD
    app = create_app(settings, pipeline=FakePipeline(), publishers={})
    # Middleware mit steuerbarer Uhr vor die App setzen
    guarded = SecurityMiddleware(app, password=lambda: PASSWORD, max_failures=3, window_s=600.0,
                                 clock=lambda: now[0])
    for i in range(3):
        assert make_client(guarded, password=f"x{i}").get("/").status_code == 401
        now[0] += 60
    r = make_client(guarded).get("/")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) == 600 - 180
    now[0] += 600 - 180
    assert make_client(guarded).get("/").status_code == 200


def test_csrf_post_without_origin_or_referer_is_blocked(dash):
    c = make_client(dash.app, origin=None)
    r = c.post("/creators", data={"platform_label": "Twitch", "handle": "abc", "rule": "block"},
               follow_redirects=False)
    assert r.status_code == 403
    assert "blockiert" in r.text


def test_csrf_foreign_origin_is_blocked(dash):
    for origin in ("https://evil.example", "http://testserver.evil.example", "http://testserver:9999", "null"):
        c = make_client(dash.app, origin=origin)
        r = c.post("/creators", data={"platform_label": "Twitch", "handle": "abc", "rule": "block"},
                   follow_redirects=False)
        assert r.status_code == 403, origin
    # auch die Review-Aktionen
    r = make_client(dash.app, origin="https://evil.example").post("/clips/1/approve", data={"platforms": "youtube"})
    assert r.status_code == 403
    assert dash.pipeline.approved == []


def test_csrf_same_origin_and_referer_fallback_pass(dash):
    ok = make_client(dash.app, origin=ORIGIN)
    r = ok.post("/creators", data={"platform_label": "Twitch", "handle": "abc", "rule": "block"},
                follow_redirects=False)
    assert r.status_code == 303
    ref = make_client(dash.app, origin=None)
    r = ref.post("/creators", data={"platform_label": "Twitch", "handle": "def", "rule": "block"},
                 headers={"Referer": "http://testserver/creators"}, follow_redirects=False)
    assert r.status_code == 303


def test_csrf_accepts_public_base_url_host_behind_proxy(settings, db):
    settings.secrets.dashboard_password = PASSWORD
    settings.config.dashboard.public_base_url = "https://wdvi.example.net"
    app = create_app(settings, pipeline=FakePipeline(), publishers={})
    c = make_client(app, origin="https://wdvi.example.net")
    r = c.post("/creators", data={"platform_label": "Twitch", "handle": "abc", "rule": "block"},
               follow_redirects=False)
    assert r.status_code == 303


def test_security_headers_present(dash):
    r = dash.client.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert make_client(dash.app, password=None).get("/").headers["x-frame-options"] == "DENY"


def test_api_docs_are_disabled(dash):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert dash.client.get(path).status_code == 404


def test_host_matching_rules():
    assert host_matches(("example.com", 443), "example.com")
    assert host_matches(("example.com", 80), "example.com")
    assert host_matches(("192.168.1.5", 8420), "192.168.1.5:8420")
    assert host_matches(("::1", 8420), "[::1]:8420")
    assert not host_matches(("192.168.1.5", 8421), "192.168.1.5:8420")
    assert not host_matches(("evil.com", 443), "example.com")
    assert not host_matches(("example.com", 8420), "example.com")


def test_origin_allowed_variants():
    h = Headers({"host": "pc.local:8420", "origin": "http://pc.local:8420"})
    assert origin_allowed(h)
    h = Headers({"host": "pc.local:8420", "origin": "null", "referer": "http://pc.local:8420/x"})
    assert origin_allowed(h)
    h = Headers({"host": "pc.local:8420", "origin": "javascript:alert(1)"})
    assert not origin_allowed(h)
    h = Headers({"host": "127.0.0.1:8420", "x-forwarded-host": "wdvi.example.net",
                 "origin": "https://wdvi.example.net"})
    assert origin_allowed(h)
    h = Headers({"host": "127.0.0.1:8420", "origin": "https://wdvi.example.net"})
    assert origin_allowed(h, extra_hosts=["wdvi.example.net"])
    assert not origin_allowed(h)
