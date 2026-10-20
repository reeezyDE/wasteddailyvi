"""YouTube Data API v3 – Fake-Service-Objekt statt echter Google-Aufrufe."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httplib2
import pytest
from googleapiclient.errors import HttpError

from wdvi.publish import youtube as yt
from wdvi.publish.base import PublishError, TokenStore
from wdvi.publish.youtube import YouTubePublisher, map_http_error
from wdvi.schemas import PostRequest

CLIENT_SECRETS = {
    "web": {
        "client_id": "123-abc.apps.googleusercontent.com",
        "client_secret": "GOCSPX-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost:8420/oauth/youtube/callback"],
    }
}


def http_error(status: int, reason: str, message: str = "msg") -> HttpError:
    content = json.dumps(
        {
            "error": {
                "code": status,
                "message": message,
                "errors": [{"reason": reason, "domain": "youtube", "message": message}],
            }
        }
    )
    return HttpError(httplib2.Response({"status": status}), content.encode())


class FakeInsert:
    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def next_chunk(self):
        self.calls += 1
        item = self.outcomes.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeExec:
    def __init__(self, result) -> None:
        self.result = result

    def execute(self):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeVideos:
    def __init__(self, svc: FakeService) -> None:
        self.svc = svc

    def insert(self, part, body, media_body):
        self.svc.insert_args = {"part": part, "body": body, "media_body": media_body}
        return self.svc.insert_request

    def list(self, part, id):  # noqa: A002 - wie in der Google-API
        self.svc.list_args = {"part": part, "id": id}
        return FakeExec(self.svc.list_result)

    def update(self, part, body):
        self.svc.update_args.append({"part": part, "body": body})
        return FakeExec(self.svc.update_result)


class FakeService:
    def __init__(self, outcomes=None, list_result=None, update_result=None) -> None:
        self.insert_request = FakeInsert(
            outcomes or [(None, {"id": "dQw4w9WgXcQ", "status": {"uploadStatus": "uploaded"}})]
        )
        self.list_result = list_result
        self.update_result = update_result if update_result is not None else {"id": "x"}
        self.insert_args: dict = {}
        self.list_args: dict = {}
        self.update_args: list[dict] = []

    def videos(self):
        return FakeVideos(self)


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "short.mp4"
    p.write_bytes(b"\x00" * 2048)
    return p


@pytest.fixture
def secrets_file(settings, tmp_path):
    f = tmp_path / "client_secret.json"
    f.write_text(json.dumps(CLIENT_SECRETS), encoding="utf-8")
    settings.secrets.google_client_secrets_file = str(f)
    settings.config.dashboard.public_base_url = "http://localhost:8420"
    return f


def make(settings, svc: FakeService) -> YouTubePublisher:
    p = YouTubePublisher(settings, http=svc)
    p.sleeps = []
    p._sleep = p.sleeps.append
    return p


def req(video, **kw) -> PostRequest:
    base = {
        "platform": "youtube",
        "video_path": str(video),
        "title": "He really did that 💀",
        "description": "desc <3 #GTA6",
        "hashtags": ["GTA6", "#gtavi", "GTAVI", "gta 6 clips"],
    }
    base.update(kw)
    return PostRequest(**base)


# ---------------------------------------------------------------------------- Publish


def test_publish_scheduled_with_publish_at(settings, video):
    svc = FakeService()
    pub = make(settings, svc)
    when = (datetime.now(UTC) + timedelta(hours=3)).replace(microsecond=0)
    res = pub.publish(req(video, publish_at=when))
    assert res.ok and res.status == "scheduled", res.error
    assert res.remote_id == "dQw4w9WgXcQ"
    assert res.remote_url == "https://youtube.com/shorts/dQw4w9WgXcQ"
    args = svc.insert_args
    assert args["part"] == "snippet,status"
    st = args["body"]["status"]
    assert st["privacyStatus"] == "private"
    assert st["publishAt"] == when.strftime("%Y-%m-%dT%H:%M:%SZ") and st["publishAt"].endswith("Z")
    assert st["selfDeclaredMadeForKids"] is False
    sn = args["body"]["snippet"]
    assert sn["title"] == "He really did that 💀 #Shorts"
    assert sn["categoryId"] == "20"
    assert sn["tags"] == ["GTA6", "gtavi", "gta6clips"]
    assert "<" not in sn["description"]
    media = args["media_body"]
    assert media.mimetype() == "video/mp4" and media.resumable() and media.chunksize() == 8 * 1024 * 1024


def test_publish_now_uses_config_privacy(settings, video):
    settings.config.publish.youtube.privacy = "unlisted"
    svc = FakeService()
    res = make(settings, svc).publish(req(video))
    assert res.ok and res.status == "posted"
    st = svc.insert_args["body"]["status"]
    assert st == {"selfDeclaredMadeForKids": False, "privacyStatus": "unlisted"}


def test_publish_past_or_disabled_schedule_posts_immediately(settings, video):
    svc = FakeService()
    res = make(settings, svc).publish(req(video, publish_at=datetime.now(UTC) - timedelta(minutes=5)))
    assert res.status == "posted" and "publishAt" not in svc.insert_args["body"]["status"]
    assert svc.insert_args["body"]["status"]["privacyStatus"] == "public"

    settings.config.publish.youtube.native_schedule = False
    svc = FakeService()
    res = make(settings, svc).publish(req(video, publish_at=datetime.now(UTC) + timedelta(hours=5)))
    assert res.status == "posted" and "publishAt" not in svc.insert_args["body"]["status"]


def test_naive_publish_at_is_utc(settings, video):
    svc = FakeService()
    naive = (datetime.now(UTC) + timedelta(hours=2)).replace(tzinfo=None, microsecond=0)
    make(settings, svc).publish(req(video, publish_at=naive))
    assert svc.insert_args["body"]["status"]["publishAt"] == naive.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_resumable_retry_on_5xx_and_connection_errors(settings, video):
    svc = FakeService(
        outcomes=[
            http_error(503, "backendError"),
            ConnectionResetError("reset"),
            (object(), None),
            (None, {"id": "abc123"}),
        ]
    )
    pub = make(settings, svc)
    res = pub.publish(req(video))
    assert res.ok, res.error
    assert svc.insert_request.calls == 4
    assert len(pub.sleeps) == 2


def test_non_retriable_http_error_is_mapped(settings, video):
    svc = FakeService(outcomes=[http_error(403, "quotaExceeded", "The request cannot be completed")])
    res = make(settings, svc).publish(req(video))
    assert not res.ok and res.status == "failed"
    assert "Kontingent" in res.error
    assert svc.insert_request.calls == 1


@pytest.mark.parametrize(
    "status,reason,expect",
    [
        (403, "quotaExceeded", "Kontingent"),
        (400, "uploadLimitExceeded", "Upload-Limit"),
        (403, "forbidden", "verweigert"),
        (401, "unauthorized", "neu verbinden"),
        (401, "", "neu verbinden"),
        (400, "invalidTitle", "Titel"),
        (500, "weird", "HTTP 500"),
    ],
)
def test_map_http_error(status, reason, expect):
    msg = map_http_error(http_error(status, reason))
    assert expect in msg


def test_audit_hint_on_forbidden_and_upload_limit():
    assert "Audit" in map_http_error(http_error(403, "forbidden"))
    assert "Audit" in map_http_error(http_error(400, "uploadLimitExceeded"))
    assert "Audit" not in map_http_error(http_error(403, "quotaExceeded"))


def test_retries_exhausted(settings, video):
    svc = FakeService(outcomes=[http_error(500, "backendError")] * (yt.MAX_RETRIES + 1))
    res = make(settings, svc).publish(req(video))
    assert not res.ok and "HTTP 500" in res.error


def test_private_lock_note(settings, video):
    svc = FakeService(
        outcomes=[(None, {"id": "x1", "status": {"uploadStatus": "uploaded", "privacyStatus": "private"}})]
    )
    res = make(settings, svc).publish(req(video))
    assert res.ok and res.status == "posted"
    assert res.error and "Audit" in res.error


def test_rejected_upload(settings, video):
    svc = FakeService(
        outcomes=[
            (None, {"id": "x1", "status": {"uploadStatus": "rejected", "rejectionReason": "duplicate"}})
        ]
    )
    res = make(settings, svc).publish(req(video))
    assert not res.ok and "duplicate" in res.error


def test_missing_file(settings, tmp_path):
    res = make(settings, FakeService()).publish(req(tmp_path / "nope.mp4"))
    assert not res.ok and "nicht gefunden" in res.error


def test_not_connected_without_service(settings, video, secrets_file):
    pub = YouTubePublisher(settings)
    assert pub.is_configured() and not pub.is_connected()
    res = pub.publish(req(video))
    assert not res.ok and "nicht verbunden" in res.error


def test_long_title_and_description_limits(settings, video):
    svc = FakeService()
    make(settings, svc).publish(req(video, title="t" * 300, description="💀" * 3000))
    sn = svc.insert_args["body"]["snippet"]
    assert len(sn["title"]) <= 100
    assert len(sn["description"].encode("utf-8")) <= 5000


# ---------------------------------------------------------------------------- Metriken


def test_fetch_metrics(settings):
    svc = FakeService(
        list_result={
            "items": [
                {
                    "id": "abc",
                    "statistics": {
                        "viewCount": "12345",
                        "likeCount": "678",
                        "favoriteCount": "0",
                        "commentCount": "9",
                    },
                }
            ]
        }
    )
    m = make(settings, svc).fetch_metrics("abc")
    assert (m.views, m.likes, m.comments, m.shares) == (12345, 678, 9, None)
    assert svc.list_args == {"part": "statistics,status", "id": "abc"}


def test_fetch_metrics_skips_private_video(settings):
    """Gesperrtes (nicht geprüftes API-Projekt) oder zurückgezogenes Video: ~0 Views dürfen nicht als
    'schlechtester Post' in Claudes Feedback-Beispiele landen."""
    item = {"statistics": {"viewCount": "0", "likeCount": "0"}, "status": {"privacyStatus": "private"}}
    svc = FakeService(list_result={"items": [item]})
    assert make(settings, svc).fetch_metrics("locked") is None
    # bewusst privat konfiguriert -> Metriken trotzdem liefern
    settings.config.publish.youtube.privacy = "private"
    assert make(settings, svc).fetch_metrics("locked").views == 0


def test_fetch_metrics_missing_and_errors(settings):
    assert make(settings, FakeService(list_result={"items": []})).fetch_metrics("gone") is None
    assert make(settings, FakeService(list_result=http_error(403, "forbidden"))).fetch_metrics("x") is None
    m = make(
        settings, FakeService(list_result={"items": [{"statistics": {"viewCount": "5"}}]})
    ).fetch_metrics("y")
    assert m.views == 5 and m.likes is None


# ---------------------------------------------------------------------------- Status / Zurückziehen


def test_check_status(settings):
    def status_of(list_result):
        return make(settings, FakeService(list_result=list_result)).check_status("vid")

    assert status_of({"items": [{"status": {"privacyStatus": "private", "publishAt": "x"}}]}) == "private"
    assert status_of({"items": [{"status": {"privacyStatus": "public"}}]}) == "public"
    assert status_of({"items": []}) == "missing"
    assert status_of(http_error(403, "quotaExceeded")) is None
    assert status_of(ConnectionResetError("reset")) is None
    assert make(settings, FakeService()).check_status("") is None
    assert "Audit" in yt.LOCKED_PRIVATE_HINT


def test_unschedule_makes_scheduled_video_private(settings):
    st = {
        "privacyStatus": "private",
        "publishAt": "2026-10-21T18:00:00Z",
        "selfDeclaredMadeForKids": False,
        "embeddable": False,
        "license": "creativeCommon",
        "uploadStatus": "processed",
    }
    svc = FakeService(list_result={"items": [{"id": "vid", "status": st}]})
    msg = make(settings, svc).unschedule("vid")
    assert "zurückgezogen" in msg
    assert svc.list_args == {"part": "status", "id": "vid"}
    assert len(svc.update_args) == 1
    upd = svc.update_args[0]
    assert upd["part"] == "status"
    assert upd["body"]["id"] == "vid"
    body_status = upd["body"]["status"]
    assert body_status["privacyStatus"] == "private" and "publishAt" not in body_status
    assert body_status["selfDeclaredMadeForKids"] is False
    # vorhandene Felder bleiben erhalten (videos.update setzt fehlende zurück)
    assert body_status["embeddable"] is False and body_status["license"] == "creativeCommon"
    assert "uploadStatus" not in body_status


def test_unschedule_nothing_to_do_or_already_public(settings):
    svc = FakeService(list_result={"items": []})
    assert "nicht (mehr) vorhanden" in make(settings, svc).unschedule("gone")
    assert svc.update_args == []

    svc = FakeService(list_result={"items": [{"status": {"privacyStatus": "private"}}]})
    assert "nichts zurückzuziehen" in make(settings, svc).unschedule("priv")
    assert svc.update_args == []

    for st in ({"privacyStatus": "public"}, {"privacyStatus": "public", "publishAt": "2026-10-20T18:00:00Z"}):
        svc = FakeService(list_result={"items": [{"status": st}]})
        with pytest.raises(PublishError, match="bereits veröffentlicht"):
            make(settings, svc).unschedule("pub")
        assert svc.update_args == []


def test_unschedule_without_manage_scope(settings):
    svc = FakeService(
        list_result={
            "items": [{"status": {"privacyStatus": "private", "publishAt": "2026-10-21T18:00:00Z"}}]
        },
        update_result=http_error(403, "insufficientPermissions", "Request had insufficient scopes"),
    )
    with pytest.raises(PublishError, match="neu verbinden") as ei:
        make(settings, svc).unschedule("vid")
    assert "youtube.force-ssl" in str(ei.value) and "Studio" in str(ei.value)


def test_unschedule_quota_error_is_not_reported_as_missing_scope(settings):
    svc = FakeService(
        list_result={
            "items": [{"status": {"privacyStatus": "private", "publishAt": "2026-10-21T18:00:00Z"}}]
        },
        update_result=http_error(403, "quotaExceeded"),
    )
    with pytest.raises(PublishError, match="Kontingent") as ei:
        make(settings, svc).unschedule("vid")
    assert "force-ssl" not in str(ei.value) and "Studio" in str(ei.value)


def test_unschedule_network_error_and_missing_id(settings):
    svc = FakeService(list_result=ConnectionResetError("reset"))
    with pytest.raises(PublishError, match="nicht erreichbar"):
        make(settings, svc).unschedule("vid")
    with pytest.raises(PublishError):
        make(settings, FakeService()).unschedule("")


def test_scopes_include_manage_scope():
    assert yt.UPLOAD_SCOPE in yt.SCOPES and yt.MANAGE_SCOPE in yt.SCOPES
    assert yt.MANAGE_SCOPE.endswith("/youtube.force-ssl")


# ---------------------------------------------------------------------------- OAuth


def test_is_configured_needs_existing_file(settings, tmp_path):
    pub = YouTubePublisher(settings)
    assert not pub.is_configured()
    settings.secrets.google_client_secrets_file = str(tmp_path / "missing.json")
    assert not pub.is_configured()
    with pytest.raises(PublishError, match="WDVI_GOOGLE_CLIENT_SECRETS_FILE"):
        pub.auth_url("s")


def test_auth_url_stores_pkce_verifier(settings, secrets_file):
    pub = YouTubePublisher(settings)
    url = pub.auth_url("state-123")
    q = parse_qs(urlparse(url).query)
    assert q["state"] == ["state-123"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["include_granted_scopes"] == ["true"]
    assert q["redirect_uri"] == ["http://localhost:8420/oauth/youtube/callback"]
    assert set(q["scope"][0].split()) == set(yt.SCOPES)
    pending = TokenStore(settings, "youtube_pending").load()
    verifier = pending["state-123"]["code_verifier"]
    assert verifier and len(verifier) >= 43
    if "code_challenge" in q:
        assert q["code_challenge_method"] == ["S256"]


def test_handle_callback_restores_verifier_and_saves_creds(settings, secrets_file, monkeypatch):
    from google_auth_oauthlib.flow import Flow

    pub = YouTubePublisher(settings)
    pub.auth_url("st-1")
    pub.auth_url("st-2")
    stored = TokenStore(settings, "youtube_pending").load()["st-1"]["code_verifier"]
    seen = {}

    def fake_fetch_token(self, **kwargs):
        seen["verifier"] = self.code_verifier
        seen["code"] = kwargs.get("code")
        self.oauth2session.token = {
            "access_token": "ya29.token",
            "refresh_token": "1//refresh",
            "token_type": "Bearer",
            "expires_in": 3599,
            "scope": yt.SCOPES,
            "expires_at": 9999999999,
        }
        return self.oauth2session.token

    monkeypatch.setattr(Flow, "fetch_token", fake_fetch_token)
    pub.handle_callback({"code": "4/0AX-code", "state": "st-1", "scope": " ".join(yt.SCOPES)})

    assert seen == {"verifier": stored, "code": "4/0AX-code"}
    info = TokenStore(settings, "youtube").load()
    assert info["token"] == "ya29.token" and info["refresh_token"] == "1//refresh"
    assert info["client_id"] == CLIENT_SECRETS["web"]["client_id"]
    assert pub.is_connected()
    pending = TokenStore(settings, "youtube_pending").load()
    assert "st-1" not in pending and "st-2" in pending  # nur der benutzte Vorgang wird entfernt


def test_handle_callback_unknown_state(settings, secrets_file):
    pub = YouTubePublisher(settings)
    with pytest.raises(PublishError, match="abgelaufener Anmeldevorgang"):
        pub.handle_callback({"code": "x", "state": "never-issued"})


def test_handle_callback_error_and_missing_scope(settings, secrets_file, monkeypatch):
    from google_auth_oauthlib.flow import Flow

    pub = YouTubePublisher(settings)
    with pytest.raises(PublishError, match="abgebrochen"):
        pub.handle_callback({"error": "access_denied"})

    pub.auth_url("st")

    def fake_fetch_token(self, **kwargs):
        self.oauth2session.token = {
            "access_token": "a",
            "refresh_token": "r",
            "token_type": "Bearer",
            "scope": ["https://www.googleapis.com/auth/youtube.readonly"],
        }

    monkeypatch.setattr(Flow, "fetch_token", fake_fetch_token)
    with pytest.raises(PublishError, match="youtube.upload"):
        pub.handle_callback({"code": "c", "state": "st"})
    assert TokenStore(settings, "youtube").load() is None


def test_credentials_refresh_persists_token(settings, monkeypatch):
    from google.oauth2.credentials import Credentials

    TokenStore(settings, "youtube").save(
        {
            "token": "old",
            "refresh_token": "1//refresh",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "cid",
            "client_secret": "csecret",
            "scopes": yt.SCOPES,
            "expiry": "2020-01-01T00:00:00Z",
        }
    )

    def fake_refresh(self, request):
        self.token = "ya29.new"
        self.expiry = datetime(2099, 1, 1)

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    pub = YouTubePublisher(settings)
    creds = pub._credentials()
    assert creds.token == "ya29.new"
    info = TokenStore(settings, "youtube").load()
    assert info["token"] == "ya29.new" and info["refresh_token"] == "1//refresh"
    assert info["expiry"].startswith("2099-01-01")


def test_credentials_refresh_error_message(settings, monkeypatch):
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    TokenStore(settings, "youtube").save(
        {
            "token": "old",
            "refresh_token": "r",
            "client_id": "c",
            "client_secret": "s",
            "expiry": "2020-01-01T00:00:00Z",
        }
    )

    def boom(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", boom)
    with pytest.raises(PublishError, match="Testing"):
        YouTubePublisher(settings)._credentials()


def test_service_built_from_credentials(settings, monkeypatch, video):
    """Ohne injiziertes Service-Objekt wird build() mit den Credentials aufgerufen."""
    from google.oauth2.credentials import Credentials

    TokenStore(settings, "youtube").save(
        {
            "token": "t",
            "refresh_token": "r",
            "client_id": "c",
            "client_secret": "s",
            "expiry": "2099-01-01T00:00:00Z",
        }
    )
    called = {}
    svc = FakeService()

    def fake_build(name, version, credentials, cache_discovery):
        called.update(name=name, version=version, creds=credentials, cache=cache_discovery)
        return svc

    import googleapiclient.discovery

    monkeypatch.setattr(googleapiclient.discovery, "build", fake_build)
    pub = YouTubePublisher(settings)
    res = pub.publish(req(video))
    assert res.ok, res.error
    assert called["name"] == "youtube" and called["version"] == "v3" and called["cache"] is False
    assert isinstance(called["creds"], Credentials)


def test_handle_callback_without_manage_scope_still_connects(settings, secrets_file, monkeypatch, caplog):
    """Granulare Zustimmung: ohne youtube.force-ssl klappt der Upload, nur unschedule() nicht."""
    from google_auth_oauthlib.flow import Flow

    pub = YouTubePublisher(settings)
    pub.auth_url("st")

    def fake_fetch_token(self, **kwargs):
        self.oauth2session.token = {
            "access_token": "a",
            "refresh_token": "r",
            "token_type": "Bearer",
            "expires_in": 3599,
            "expires_at": 9999999999,
            "scope": [yt.UPLOAD_SCOPE, "https://www.googleapis.com/auth/youtube.readonly"],
        }

    monkeypatch.setattr(Flow, "fetch_token", fake_fetch_token)
    with caplog.at_level("WARNING", logger="wdvi.publish.youtube"):
        pub.handle_callback({"code": "c", "state": "st"})
    assert pub.is_connected()
    assert any("force-ssl" in r.getMessage() for r in caplog.records)
