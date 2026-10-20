"""Dashboard: Geplant/Gepostet/Kandidaten/Creator/Status-Seiten und Formatierungshelfer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from tests.test_dashboard_support import (  # noqa: F401  (Fixture)
    FakePipeline,
    FakePublisher,
    add_candidate,
    add_clip,
    add_jobrun,
    add_metric,
    add_post,
    dash_fixture,
    get,
    make_client,
)
from wdvi.dashboard import _data, _fmt
from wdvi.dashboard.app import create_app
from wdvi.models import CreatorRule, Post

# --------------------------------------------------------------------------- /scheduled


def test_scheduled_shows_local_time_in_publish_timezone(dash):
    clip_id = add_clip(dash.clips_dir, thumb_path=None)
    when = datetime.now(UTC).replace(hour=23, minute=15, second=0, microsecond=0) + timedelta(days=2)
    add_post(clip_id, platform="tiktok", scheduled_at=when, title="Vice City chaos")
    add_post(clip_id, platform="youtube", status="posted", scheduled_at=when, title="schon online")
    add_post(clip_id, platform="instagram", status="canceled", scheduled_at=when, title="abgebrochen")

    html = dash.client.get("/scheduled").text
    local = when.astimezone(ZoneInfo("America/New_York"))
    assert local.strftime("%H:%M") in html
    assert when.isoformat() in html  # data-utc für die Gerätezeit per JS
    assert "Vice City chaos" in html and "TikTok" in html
    assert "schon online" not in html and "abgebrochen" not in html
    assert "New York" in html
    assert "1 anstehender Post" in html


def test_scheduled_respects_configured_timezone(settings, db):
    from tests.test_dashboard_support import PASSWORD

    settings.secrets.dashboard_password = PASSWORD
    settings.config.publish.timezone = "Europe/Berlin"
    app = create_app(settings, pipeline=FakePipeline(), publishers={})
    clip_id = add_clip(None)
    when = datetime(2026, 12, 1, 18, 0, tzinfo=UTC)  # Winterzeit: 19:00 in Berlin
    add_post(clip_id, scheduled_at=when)
    html = make_client(app).get("/scheduled").text
    assert "19:00" in html and "Berlin" in html


def test_cancel_scheduled_post(dash):
    clip_id = add_clip(None)
    pid = add_post(clip_id, platform="instagram")
    r = dash.client.post(f"/posts/{pid}/cancel", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/scheduled?flash=")
    page = dash.client.get(r.headers["location"]).text
    assert "Instagram-Post abgebrochen" in page
    post = get(Post, pid)
    assert post.status == "canceled"


def test_cancel_refuses_uploading_or_done_posts(dash):
    clip_id = add_clip(None)
    up = add_post(clip_id, status="uploading")
    done = add_post(clip_id, status="posted")
    r = dash.client.post(f"/posts/{up}/cancel", headers={"X-WDVI-Ajax": "1"})
    assert r.status_code == 400 and "läuft gerade" in r.json()["message"]
    r = dash.client.post(f"/posts/{done}/cancel", headers={"X-WDVI-Ajax": "1"})
    assert r.status_code == 400
    r = dash.client.post("/posts/999/cancel", headers={"X-WDVI-Ajax": "1"})
    assert "nicht gefunden" in r.json()["message"]
    assert get(Post, up).status == "uploading" and get(Post, done).status == "posted"


def test_cancel_already_uploaded_youtube_post_warns(dash):
    clip_id = add_clip(None)
    pid = add_post(clip_id, platform="youtube", remote_id="yt123")
    assert "Bereits bei YouTube hochgeladen" in dash.client.get("/scheduled").text
    r = dash.client.post(f"/posts/{pid}/cancel", headers={"X-WDVI-Ajax": "1"})
    assert r.json()["ok"] is True
    assert "zusätzlich" in r.json()["message"]


# --------------------------------------------------------------------------- /posted


def test_posted_shows_link_and_latest_metrics(dash):
    clip_id = add_clip(dash.clips_dir, thumb_path=None)
    now = datetime.now(UTC)
    pid = add_post(clip_id, platform="youtube", status="posted", posted_at=now - timedelta(days=3),
                   remote_url="https://youtube.com/shorts/abc123", title="Bester Clip")
    add_metric(pid, views=1000, likes=10, comments=1, shares=0, hours_after=24, fetched_at=now - timedelta(days=2))
    add_metric(pid, views=54321, likes=999, comments=42, shares=7, hours_after=72, fetched_at=now - timedelta(hours=1))
    add_post(clip_id, platform="tiktok", status="draft_sent", posted_at=now - timedelta(days=1), title="Entwurf-Clip")
    add_post(clip_id, platform="instagram", status="failed", title="Kaputt", error="Instagram-Token abgelaufen")
    add_post(clip_id, platform="youtube", status="scheduled", title="Kommt noch")

    html = dash.client.get("/posted").text
    assert 'href="https://youtube.com/shorts/abc123"' in html
    assert "54.321" in html and "999" in html and "72 h" in html
    assert ">1.000<" not in html  # alte Metrik nicht
    assert "Entwurf-Clip" in html and "Entwurf" in html
    assert "Kommt noch" not in html
    assert "Fehlgeschlagene Uploads" in html and "Instagram-Token abgelaufen" in html


def test_posted_empty(dash):
    assert "Noch nichts gepostet" in dash.client.get("/posted").text


# --------------------------------------------------------------------------- /candidates


def test_candidates_list_and_status_filter(dash):
    add_candidate(title="Guter Clip", status="rendered", prescore=7.25)
    add_candidate(title="Zu langweilig", status="rejected", status_reason="Virality-Score 40 < 70")
    add_candidate(title="Neu gefunden", status="new")

    html = dash.client.get("/candidates").text
    for t in ("Guter Clip", "Zu langweilig", "Neu gefunden", "Virality-Score 40 &lt; 70", "Prescore 7.2"):
        assert t in html
    assert "Abgelehnt" in html and "Gerendert" in html

    html = dash.client.get("/candidates?status=rejected").text
    assert "Zu langweilig" in html
    assert "Guter Clip" not in html and "Neu gefunden" not in html
    assert 'class="pill status-rejected active"' in html

    html = dash.client.get("/candidates?status=failed").text
    assert "Keine Kandidaten mit diesem Status" in html


# --------------------------------------------------------------------------- /creators


def test_creators_page_explains_block_for_optouts(dash):
    html = dash.client.get("/creators").text
    assert "Opt-out" in html and "Takedown" in html
    assert "Noch keine Regeln" in html


def test_creator_rules_add_update_delete(dash):
    r = dash.client.post("/creators", data={"platform_label": "Twitch", "handle": "  @XQC ", "rule": "block",
                                            "note": "Opt-out per DM"}, follow_redirects=False)
    assert r.status_code == 303
    page = dash.client.get(r.headers["location"]).text
    assert "Regel für xqc (Twitch) angelegt" in page
    assert "Opt-out per DM" in page

    # gleicher Creator per Link -> Update statt Duplikat
    r = dash.client.post("/creators", data={"platform_label": "Twitch", "handle": "https://www.twitch.tv/xqc",
                                            "rule": "boost"}, headers={"X-WDVI-Ajax": "1"})
    assert r.json()["ok"] and "aktualisiert" in r.json()["message"]
    rules = _data.creator_rules()
    assert len(rules) == 1 and rules[0]["handle"] == "xqc" and rules[0]["rule"] == "boost"

    r = dash.client.post(f"/creators/{rules[0]['id']}/delete", follow_redirects=False)
    assert "gelöscht" in dash.client.get(r.headers["location"]).text
    assert _data.creator_rules() == []


def test_creator_rule_validation(dash):
    for data in (
        {"platform_label": "Twitch", "handle": "x", "rule": "ban"},
        {"platform_label": "MySpace", "handle": "x", "rule": "block"},
        {"platform_label": "Twitch", "handle": "  @ ", "rule": "block"},
    ):
        r = dash.client.post("/creators", data=data, headers={"X-WDVI-Ajax": "1"})
        assert r.status_code == 400, data
    assert _data.creator_rules() == []
    r = dash.client.post("/creators/12345/delete", headers={"X-WDVI-Ajax": "1"})
    assert r.status_code == 400


def test_normalize_handle():
    assert _data.normalize_handle("@XQC") == "xqc"
    assert _data.normalize_handle("https://www.twitch.tv/xQc/clips") == "xqc"
    assert _data.normalize_handle("kick.com/AdinRoss") == "adinross"
    assert _data.normalize_handle("https://www.reddit.com/user/Some_User/") == "some_user"
    assert _data.normalize_handle("https://www.youtube.com/@GTAFan") == "gtafan"
    assert _data.normalize_handle("  Some   Channel  ") == "some channel"
    assert _data.normalize_handle("") == ""


def test_creator_rules_are_stored_lowercase_in_db(dash):
    dash.client.post("/creators", data={"platform_label": "Kick", "handle": "AdinRoss", "rule": "allow"})
    from sqlmodel import select

    from wdvi import db as dbmod

    with dbmod.session_scope() as s:
        rows = s.exec(select(CreatorRule)).all()
    assert [(r.platform_label, r.handle, r.rule) for r in rows] == [("Kick", "adinross", "allow")]


# --------------------------------------------------------------------------- /health


def test_health_shows_latest_jobrun_per_job_and_counts(dash):
    add_jobrun("discover:twitch", ok=False, message="alter Fehler", minutes_ago=60)
    add_jobrun("discover:twitch", ok=True, message="14 neue Kandidaten", minutes_ago=3)
    add_jobrun("process", ok=False, message="RenderError: ffmpeg kaputt", minutes_ago=1)
    add_jobrun("stats", ok=True, message="ok", minutes_ago=60 * 10)  # alle 3 h erwartet -> überfällig
    add_candidate(status="new")
    add_candidate(status="rejected")
    add_clip(None)

    html = dash.client.get("/health").text
    assert "discover:twitch" in html and "14 neue Kandidaten" in html
    assert "alter Fehler" not in html
    assert "1× Fehler in 24 h" in html
    assert "RenderError: ffmpeg kaputt" in html and "chip-bad" in html
    assert "überfällig" in html
    assert "vor 3 Min." in html
    assert "claude-opus-5-5" in html
    assert "Anthropic-API" in html
    assert "x264" in html  # Encoder-Einstellung der Test-Config
    assert "Wartet auf Review" in html


def test_health_shows_secret_presence_but_never_values(settings, db):
    values = {
        "twitch_client_id": "TWITCHID_9f8e7d",
        "twitch_client_secret": "TWITCHSECRET_1a2b3c",
        "reddit_client_id": "REDDITID_77aa",
        "reddit_client_secret": "REDDITSECRET_88bb",
        "youtube_api_key": "AIzaSyYOUTUBEKEY_123",
        "google_client_secrets_file": "/very/secret/path/client_secret_xyz.json",
        "tiktok_client_key": "TIKTOKKEY_c0ffee",
        "tiktok_client_secret": "TIKTOKSECRET_beef",
        "meta_app_id": "METAAPPID_4242",
        "meta_app_secret": "METASECRET_abcd",
        "dashboard_password": "DASHPASS_s3cr3t!",
        "anthropic_api_key": "sk-ant-api03-SUPERSECRET",
    }
    for k, v in values.items():
        setattr(settings.secrets, k, v)
    pubs = {"youtube": FakePublisher("youtube"), "instagram": FakePublisher("instagram", configured=False)}
    app = create_app(settings, pipeline=FakePipeline(), publishers=pubs)
    c = make_client(app, password=values["dashboard_password"])
    add_jobrun("process")

    html = c.get("/health").text
    assert "WDVI_TWITCH_CLIENT_SECRET" in html
    assert "ANTHROPIC_API_KEY" in html
    assert "gesetzt" in html
    assert "Datei NICHT gefunden" in html  # client_secret.json existiert nicht
    for page in ("/", "/health", "/connections", "/scheduled", "/posted", "/candidates", "/creators"):
        body = c.get(page).text
        for secret in values.values():
            assert secret not in body, (page, secret)


def test_health_marks_missing_secrets(dash):
    html = dash.client.get("/health").text
    assert "fehlt" in html
    assert "Noch keine Job-Läufe" in html


# --------------------------------------------------------------------------- Formatierung


def test_fmt_helpers():
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    assert _fmt.ago(now - timedelta(seconds=10), now) == "gerade eben"
    assert _fmt.ago(now - timedelta(minutes=5), now) == "vor 5 Min."
    assert _fmt.ago(now - timedelta(hours=3), now) == "vor 3 Std."
    assert _fmt.ago(now - timedelta(days=4), now) == "vor 4 Tagen"
    assert _fmt.ago(now + timedelta(hours=2), now) == "in 2 Std."
    assert _fmt.ago(datetime(2026, 10, 20, 11, 0), now) == "vor 1 Std."  # naiv = UTC
    assert _fmt.ago(None) == "–"
    assert _fmt.fmt_int(1234567) == "1,2 Mio."
    assert _fmt.fmt_int(12345) == "12.345"
    assert _fmt.fmt_int(None) == "–"
    assert _fmt.fmt_duration(42.4) == "0:42"
    assert _fmt.fmt_duration(3725) == "1:02:05"
    assert _fmt.safe_url("https://x.com/a") == "https://x.com/a"
    for bad in ("javascript:alert(1)", "data:text/html,x", "//evil.com", "", None, "ftp://x"):
        assert _fmt.safe_url(bad) == ""
    tz, ok = _fmt.load_tz("America/New_York")
    assert ok
    assert _fmt.fmt_local(datetime(2026, 10, 21, 23, 0, tzinfo=UTC), tz) == "Mi 21.10. 19:00"
    # Tagesgrenzen gelten in der Zielzeitzone: 02:00 UTC am 21.10. = 22:00 am 20.10. in New York
    assert _fmt.fmt_day(datetime(2026, 10, 21, 2, 0, tzinfo=UTC), tz, now) == "Heute"
    assert _fmt.fmt_day(datetime(2026, 10, 21, 14, 0, tzinfo=UTC), tz, now) == "Morgen"
    assert _fmt.fmt_day(datetime(2026, 10, 23, 14, 0, tzinfo=UTC), tz, now) == "Fr 23.10."
    tz, ok = _fmt.load_tz("Mars/Olympus_Mons")
    assert not ok and tz is UTC
    assert _fmt.redact("x?code=abc&state=1 Bearer eyJ.abc") == "x?code=***&state=1 Bearer ***"
    assert _fmt.tz_label("America/New_York") == "New York"
