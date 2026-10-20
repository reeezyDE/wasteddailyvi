"""Instagram API mit Instagram Login – komplett mit respx gemockt."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from wdvi.publish.base import PublishError, TokenStore
from wdvi.publish.instagram import InstagramPublisher
from wdvi.schemas import PostRequest

G = "https://graph.instagram.com"
GV = f"{G}/v23.0"
NOW = 1_800_000_000.0
DAY = 86400


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as r:
        yield r


@pytest.fixture
def pub(settings, router):
    settings.secrets.meta_app_id = "1234567890"
    settings.secrets.meta_app_secret = "app-secret-xyz"
    settings.config.dashboard.public_base_url = "http://localhost:8420/"
    p = InstagramPublisher(settings, http=httpx.Client())
    clock = FakeClock()
    p._clock = clock
    p._sleep = clock.sleep
    p._now = lambda: NOW
    p.fake_clock = clock
    return p


def seed(settings, **over):
    tok = {
        "access_token": "IGQ-long",
        "user_id": "17841400000000001",
        "username": "wasteddailyvi",
        "expires_at": NOW + 40 * DAY,
        "obtained_at": NOW - 20 * DAY,
        "source": "oauth",
    }
    tok.update(over)
    TokenStore(settings, "instagram").save(tok)


def form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "reel.mp4"
    p.write_bytes(b"\x11" * 4321)
    return p


def req(video) -> PostRequest:
    return PostRequest(
        platform="instagram",
        video_path=str(video),
        title="Wasted in 3 seconds 💀",
        description="Bro really tried it #GTA6\n\n🎥 Clip: xQc on Twitch",
        hashtags=["GTA6"],
    )


# ---------------------------------------------------------------------------- OAuth


def test_auth_url(pub):
    assert pub.is_configured() and not pub.is_connected()
    u = urlparse(pub.auth_url("abc"))
    assert (u.scheme, u.netloc, u.path) == ("https", "www.instagram.com", "/oauth/authorize")
    q = parse_qs(u.query)
    assert q["client_id"] == ["1234567890"]
    assert q["redirect_uri"] == ["https://localhost:8420/oauth/instagram/callback"]
    assert q["response_type"] == ["code"]
    assert q["state"] == ["abc"]
    assert q["scope"] == [
        "instagram_business_basic,instagram_business_content_publish,instagram_business_manage_insights"
    ]


def test_not_configured(settings):
    p = InstagramPublisher(settings, http=httpx.Client())
    assert not p.is_configured()
    with pytest.raises(PublishError, match="WDVI_META_APP_ID"):
        p.auth_url("x")


@pytest.mark.parametrize("wrapped", [True, False])
def test_code_exchange_flow(pub, router, settings, wrapped):
    entry = {"access_token": "IGQ-short", "user_id": 9876543210, "permissions": "instagram_business_basic"}
    short = router.post("https://api.instagram.com/oauth/access_token").respond(
        json={"data": [entry]} if wrapped else entry
    )
    long = router.get(f"{G}/access_token").respond(
        json={"access_token": "IGQ-long-NEW", "token_type": "bearer", "expires_in": 5183944}
    )
    me = router.get(f"{GV}/me").respond(
        json={"user_id": "17841400000000099", "username": "wasteddailyvi", "id": "9876543210"}
    )

    pub.handle_callback({"code": "AQBx-code#_", "state": "abc"})

    sent = form(short.calls[0].request)
    assert sent == {
        "client_id": ["1234567890"],
        "client_secret": ["app-secret-xyz"],
        "grant_type": ["authorization_code"],
        "redirect_uri": ["https://localhost:8420/oauth/instagram/callback"],
        "code": ["AQBx-code"],
    }
    p = long.calls[0].request.url.params
    assert p["grant_type"] == "ig_exchange_token"
    assert p["client_secret"] == "app-secret-xyz" and p["access_token"] == "IGQ-short"
    assert me.calls[0].request.url.params["access_token"] == "IGQ-long-NEW"

    tok = TokenStore(settings, "instagram").load()
    assert tok["access_token"] == "IGQ-long-NEW"
    assert tok["user_id"] == "17841400000000099"  # IG-Professional-ID aus /me
    assert tok["expires_at"] == NOW + 5183944
    assert pub.is_connected()


def test_code_exchange_error(pub, router):
    router.post("https://api.instagram.com/oauth/access_token").respond(
        400, json={"error_type": "OAuthException", "code": 400, "error_message": "Invalid authorization code"}
    )
    with pytest.raises(PublishError, match="Invalid authorization code"):
        pub.handle_callback({"code": "bad"})


def test_callback_error_param(pub):
    with pytest.raises(PublishError, match="abgebrochen"):
        pub.handle_callback(
            {
                "error": "access_denied",
                "error_reason": "user_denied",
                "error_description": "The user denied your request.",
            }
        )


def test_manual_pasted_token(pub, router, settings):
    me = router.get(f"{GV}/me").respond(json={"user_id": "17841400000000001", "username": "wasteddailyvi"})
    pub.handle_callback({"access_token": "  IGQ-pasted  ", "user_id": "17841400000000001"})
    assert me.calls[0].request.url.params["access_token"] == "IGQ-pasted"
    tok = TokenStore(settings, "instagram").load()
    assert tok["access_token"] == "IGQ-pasted" and tok["user_id"] == "17841400000000001"
    assert tok["source"] == "manual"
    assert tok["expires_at"] > NOW + 50 * DAY
    assert pub.is_connected()


def test_manual_pasted_token_without_user_id_uses_me(pub, router, settings):
    router.get(f"{GV}/me").respond(json={"user_id": "1784", "username": "wd"})
    pub.handle_callback({"access_token": "IGQ-pasted", "expires_in": "3600"})
    tok = TokenStore(settings, "instagram").load()
    assert tok["user_id"] == "1784" and tok["expires_at"] == NOW + 3600


def test_manual_pasted_invalid_token(pub, router, settings):
    router.get(f"{GV}/me").respond(
        400, json={"error": {"message": "Invalid OAuth access token.", "type": "OAuthException", "code": 190}}
    )
    with pytest.raises(PublishError, match="nicht akzeptiert"):
        pub.handle_callback({"access_token": "garbage", "user_id": "1"})
    assert TokenStore(settings, "instagram").load() is None


def test_expired_token_not_connected(pub, settings, video):
    seed(settings, expires_at=NOW - 1)
    assert not pub.is_connected()
    res = pub.publish(req(video))
    assert not res.ok and "abgelaufen" in res.error


# ---------------------------------------------------------------------------- Publish


def mock_publish_flow(router, statuses=("IN_PROGRESS", "IN_PROGRESS", "FINISHED")):
    create = router.post(f"{GV}/17841400000000001/media").respond(
        json={
            "id": "1790000000000555",
            "uri": "https://rupload.facebook.com/ig-api-upload/v23.0/1790000000000555",
        }
    )
    upload = router.post("https://rupload.facebook.com/ig-api-upload/v23.0/1790000000000555").respond(
        json={"success": True, "message": "Upload successful."}
    )
    status = router.get(f"{GV}/1790000000000555")
    status.side_effect = [
        httpx.Response(200, json={"status_code": s, "status": f"{s}: details", "id": "1790000000000555"})
        for s in statuses
    ]
    publish = router.post(f"{GV}/17841400000000001/media_publish").respond(json={"id": "18000000000000777"})
    permalink = router.get(f"{GV}/18000000000000777").respond(
        json={"permalink": "https://www.instagram.com/reel/ABCdef123/", "id": "18000000000000777"}
    )
    return create, upload, status, publish, permalink


def test_publish_full_flow(pub, router, settings, video):
    seed(settings)
    create, upload, status, publish, permalink = mock_publish_flow(router)
    res = pub.publish(req(video))
    assert res.ok and res.status == "posted", res.error
    assert res.remote_id == "18000000000000777"
    assert res.remote_url == "https://www.instagram.com/reel/ABCdef123/"

    f = form(create.calls[0].request)
    assert f["media_type"] == ["REELS"] and f["upload_type"] == ["resumable"]
    assert f["share_to_feed"] == ["true"] and f["access_token"] == ["IGQ-long"]
    assert f["caption"] == ["Wasted in 3 seconds 💀\n\nBro really tried it #GTA6\n\n🎥 Clip: xQc on Twitch"]

    up = upload.calls[0].request
    assert up.headers["Authorization"] == "OAuth IGQ-long"
    assert up.headers["offset"] == "0"
    assert up.headers["file_size"] == "4321"
    assert up.content == video.read_bytes()

    assert status.call_count == 3
    assert status.calls[0].request.url.params["fields"] == "status_code,status"
    assert pub.fake_clock.sleeps == [5.0, 5.0]
    assert form(publish.calls[0].request)["creation_id"] == ["1790000000000555"]
    assert permalink.calls[0].request.url.params["fields"] == "permalink"


def test_publish_share_to_feed_false(pub, router, settings, video):
    settings.config.publish.instagram.share_to_feed = False
    seed(settings)
    create, *_ = mock_publish_flow(router, statuses=("FINISHED",))
    assert pub.publish(req(video)).ok
    assert form(create.calls[0].request)["share_to_feed"] == ["false"]


def test_publish_container_error(pub, router, settings, video):
    seed(settings)
    _, _, _, publish, _ = mock_publish_flow(router, statuses=("IN_PROGRESS", "ERROR"))
    res = pub.publish(req(video))
    assert not res.ok and res.status == "failed"
    assert "ERROR" in res.error and "details" in res.error
    assert not publish.called


def test_publish_container_timeout(pub, router, settings, video):
    seed(settings)
    pub.poll_timeout_s = 20
    _, _, _, publish, _ = mock_publish_flow(router, statuses=("IN_PROGRESS",) * 10)
    res = pub.publish(req(video))
    assert not res.ok and "länger als" in res.error
    assert not publish.called


def test_publish_upload_failure(pub, router, settings, video):
    seed(settings)
    mock_publish_flow(router)
    router.post("https://rupload.facebook.com/ig-api-upload/v23.0/1790000000000555").respond(
        400,
        json={
            "debug_info": {
                "retriable": False,
                "type": "ProcessingFailedError",
                "message": "Request processing failed",
            }
        },
    )
    res = pub.publish(req(video))
    assert not res.ok and "Request processing failed" in res.error
    assert "IGQ-long" not in res.error


def test_publish_graph_error_mapping(pub, router, settings, video):
    seed(settings)
    router.post(f"{GV}/17841400000000001/media").respond(
        400,
        json={
            "error": {
                "message": "Error validating access token",
                "type": "OAuthException",
                "code": 190,
                "fbtrace_id": "x",
            }
        },
    )
    res = pub.publish(req(video))
    assert not res.ok and "neu verbinden" in res.error


def test_publish_not_ready_retry(pub, router, settings, video):
    seed(settings)
    _, _, _, publish, _ = mock_publish_flow(router, statuses=("FINISHED",))
    publish.side_effect = [
        httpx.Response(
            400,
            json={"error": {"message": "Media ID is not available", "code": 9007, "error_subcode": 2207027}},
        ),
        httpx.Response(200, json={"id": "18000000000000777"}),
    ]
    res = pub.publish(req(video))
    assert res.ok, res.error
    assert publish.call_count == 2


def test_auto_refresh_when_less_than_7_days(pub, router, settings, video):
    seed(settings, expires_at=NOW + 3 * DAY)
    refresh = router.get(f"{G}/refresh_access_token").respond(
        json={"access_token": "IGQ-refreshed", "token_type": "bearer", "expires_in": 5183944}
    )
    create, *_ = mock_publish_flow(router, statuses=("FINISHED",))
    res = pub.publish(req(video))
    assert res.ok, res.error
    p = refresh.calls[0].request.url.params
    assert p["grant_type"] == "ig_refresh_token" and p["access_token"] == "IGQ-long"
    assert form(create.calls[0].request)["access_token"] == ["IGQ-refreshed"]
    tok = TokenStore(settings, "instagram").load()
    assert tok["access_token"] == "IGQ-refreshed" and tok["expires_at"] == NOW + 5183944


def test_no_refresh_when_plenty_left(pub, router, settings):
    seed(settings, expires_at=NOW + 30 * DAY)
    refresh = router.get(f"{G}/refresh_access_token")
    router.get(f"{GV}/1/insights").respond(json={"data": []})
    pub.fetch_metrics("1")
    assert not refresh.called


def test_failed_refresh_keeps_valid_token(pub, router, settings, video):
    seed(settings, expires_at=NOW + 2 * DAY)
    router.get(f"{G}/refresh_access_token").respond(500, json={"error": {"message": "boom", "code": 2}})
    create, *_ = mock_publish_flow(router, statuses=("FINISHED",))
    assert pub.publish(req(video)).ok
    assert form(create.calls[0].request)["access_token"] == ["IGQ-long"]


# ---------------------------------------------------------------------------- Metriken


def test_fetch_metrics_insights(pub, router, settings):
    seed(settings)
    route = router.get(f"{GV}/18000000000000777/insights").respond(
        json={
            "data": [
                {"name": "views", "period": "lifetime", "values": [{"value": 15234}], "id": "a"},
                {"name": "likes", "period": "lifetime", "values": [{"value": 812}], "id": "b"},
                {"name": "comments", "period": "lifetime", "total_value": {"value": 44}, "id": "c"},
                {"name": "shares", "period": "lifetime", "values": [{"value": 31}], "id": "d"},
            ]
        }
    )
    m = pub.fetch_metrics("18000000000000777")
    assert (m.views, m.likes, m.comments, m.shares) == (15234, 812, 44, 31)
    assert route.calls[0].request.url.params["metric"] == "views,likes,comments,shares"


def test_fetch_metrics_fallback_fields(pub, router, settings):
    seed(settings)
    router.get(f"{GV}/18000000000000777/insights").respond(
        400, json={"error": {"message": "metric not supported", "code": 100}}
    )
    router.get(f"{GV}/18000000000000777").respond(json={"like_count": 5, "comments_count": 2})
    m = pub.fetch_metrics("18000000000000777")
    assert m.views is None and m.likes == 5 and m.comments == 2


def test_fetch_metrics_not_connected(pub):
    assert pub.fetch_metrics("1") is None


# ---------------------------------------------------------------------------- media_publish unklar
# Ab dem abgeschickten media_publish darf nur eine ausdrückliche Absage zu 'failed' führen – sonst legt die
# Pipeline einen neuen Container an und das Reel erscheint doppelt.

CAPTION = "Wasted in 3 seconds 💀\n\nBro really tried it #GTA6\n\n🎥 Clip: xQc on Twitch"


def container_status(router, *codes):
    """Statusabfragen des Containers: erst FINISHED (Verarbeitung), danach die angegebenen Codes."""
    route = router.get(f"{GV}/1790000000000555")
    route.side_effect = [
        httpx.Response(200, json={"status_code": c, "status": f"{c}: details", "id": "1790000000000555"})
        for c in ("FINISHED", *codes)
    ]
    return route


def test_publish_timeout_but_published_finds_media(pub, router, settings, video):
    seed(settings)
    create, _, _, publish, permalink = mock_publish_flow(router, statuses=("FINISHED",))
    container_status(router, "PUBLISHED")
    publish.side_effect = httpx.ReadTimeout("timed out")
    media = router.get(f"{GV}/17841400000000001/media").respond(
        json={
            "data": [
                {"id": "18000000000000999", "caption": "older post"},
                {"id": "18000000000000777", "caption": CAPTION},
            ]
        }
    )
    res = pub.publish(req(video))
    assert res.ok and res.status == "posted", res.error
    assert res.remote_id == "18000000000000777" and res.error is None
    assert res.remote_url == "https://www.instagram.com/reel/ABCdef123/"
    assert create.call_count == 1 and publish.call_count == 3
    assert media.calls[0].request.url.params["fields"] == "id,caption,timestamp"
    # media_publish bekommt ein längeres Timeout als normale API-Aufrufe
    assert publish.calls[0].request.extensions["timeout"]["read"] == 180.0
    assert permalink.called


def test_publish_unclear_then_rejected_checks_container(pub, router, settings, video):
    seed(settings)
    _, _, _, publish, _ = mock_publish_flow(router, statuses=("FINISHED",))
    container_status(router, "PUBLISHED")
    publish.side_effect = [
        httpx.Response(500, json={"error": {"message": "An unknown error occurred", "code": 1}}),
        httpx.Response(400, json={"error": {"message": "Media already published", "code": 100}}),
    ]
    router.get(f"{GV}/17841400000000001/media").respond(json={"data": []})
    res = pub.publish(req(video))
    assert res.ok and res.status == "posted"
    assert res.remote_id is None and res.remote_url is None
    assert "veröffentlicht" in res.error and "Media-ID" in res.error
    assert publish.call_count == 2


def test_publish_state_unknown_is_ok_with_note(pub, router, settings, video):
    seed(settings)
    create, upload, _, publish, _ = mock_publish_flow(router, statuses=("FINISHED",))
    container_status(router, "FINISHED", "FINISHED", "FINISHED")
    publish.mock(return_value=httpx.Response(502, text="Bad Gateway"))
    res = pub.publish(req(video))
    assert res.ok and res.status == "posted" and res.remote_id is None
    assert "Status unbekannt" in res.error and "nicht automatisch erneut" in res.error
    assert "IGQ-long" not in res.error
    assert create.call_count == 1 and upload.call_count == 1 and publish.call_count == 3


def test_publish_unclear_then_container_error_fails(pub, router, settings, video):
    seed(settings)
    _, _, _, publish, _ = mock_publish_flow(router, statuses=("FINISHED",))
    container_status(router, "ERROR")
    publish.side_effect = httpx.ReadTimeout("timed out")
    res = pub.publish(req(video))
    assert not res.ok and res.status == "failed" and "ERROR" in res.error


def test_publish_not_sent_fails_cleanly(pub, router, settings, video):
    """Verbindung kam nie zustande -> Meta hat nichts veröffentlicht -> 'failed' ist richtig (Retry)."""
    seed(settings)
    _, _, _, publish, _ = mock_publish_flow(router, statuses=("FINISHED",))
    publish.side_effect = httpx.ConnectError("no route")
    res = pub.publish(req(video))
    assert not res.ok and res.status == "failed" and "ConnectError" in res.error
    assert publish.call_count == 3


def test_publish_explicit_rejection_fails_without_retry(pub, router, settings, video):
    seed(settings)
    _, _, _, publish, _ = mock_publish_flow(router, statuses=("FINISHED",))
    publish.mock(
        return_value=httpx.Response(
            400, json={"error": {"message": "Application request limit reached", "code": 4}}
        )
    )
    res = pub.publish(req(video))
    assert not res.ok and "Rate-Limit" in res.error
    assert publish.call_count == 1
