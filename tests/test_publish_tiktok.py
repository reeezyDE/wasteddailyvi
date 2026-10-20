"""TikTok Content Posting API v2 – komplett mit respx gemockt."""

from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from wdvi.publish import tiktok as tt
from wdvi.publish.base import PublishError, TokenStore
from wdvi.publish.tiktok import TikTokPublisher, chunk_plan, chunk_ranges
from wdvi.schemas import PostRequest

API = "https://open.tiktokapis.com"
UPLOAD_URL = "https://open-upload.tiktokapis.com/video/?upload_id=123&upload_token=secret-upload-token"
MB = 1024 * 1024
NOW = 1_800_000_000.0


def ok(data: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200, json={"data": data or {}, "error": {"code": "ok", "message": "", "log_id": "L1"}}
    )


def api_err(code: str, message: str = "nope", status: int = 400) -> httpx.Response:
    return httpx.Response(
        status, json={"data": {}, "error": {"code": code, "message": message, "log_id": "L2"}}
    )


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
    settings.secrets.tiktok_client_key = "ck123"
    settings.secrets.tiktok_client_secret = "cs456"
    settings.config.dashboard.public_base_url = "http://localhost:8420"
    p = TikTokPublisher(settings, http=httpx.Client())
    clock = FakeClock()
    p._clock = clock
    p._sleep = clock.sleep
    p._now = lambda: NOW
    p.fake_clock = clock
    return p


def seed_token(settings, **over) -> None:
    tok = {
        "access_token": "act.OLD",
        "refresh_token": "rft.OLD",
        "open_id": "oid",
        "scope": "user.info.basic,video.upload,video.publish,video.list",
        "expires_at": NOW + 20 * 3600,
        "refresh_expires_at": NOW + 300 * 86400,
    }
    tok.update(over)
    TokenStore(settings, "tiktok").save(tok)


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00\x01" * 5000)  # 10 000 Bytes
    return p


def req(video, **kw) -> PostRequest:
    base = {
        "platform": "tiktok",
        "video_path": str(video),
        "title": "No way 💀",
        "description": "He did that #GTA6\n\n🎥 Clip: xQc on Twitch",
        "hashtags": ["GTA6"],
    }
    base.update(kw)
    return PostRequest(**base)


# ---------------------------------------------------------------------------- Chunk-Mathematik


def test_chunk_plan_rules():
    assert chunk_plan(1000) == (1000, 1)
    assert chunk_plan(4 * MB) == (4 * MB, 1)  # < 5 MB immer am Stück
    assert chunk_plan(64 * MB) == (64 * MB, 1)
    assert chunk_plan(64 * MB + 1) == (10 * MB, 6)
    assert chunk_plan(100 * MB) == (10 * MB, 10)
    assert chunk_plan(109 * MB) == (10 * MB, 10)  # letzter Chunk nimmt 19 MB auf
    with pytest.raises(PublishError):
        chunk_plan(0)


@pytest.mark.parametrize("size", [64 * MB + 1, 100 * MB, 109 * MB + 12345, 333 * MB + 7])
def test_chunk_ranges_cover_file(size):
    cs, n = chunk_plan(size)
    ranges = chunk_ranges(size, cs, n)
    assert len(ranges) == n
    assert ranges[0][0] == 0 and ranges[-1][1] == size - 1
    for (_, e1), (s2, _) in zip(ranges, ranges[1:], strict=False):
        assert s2 == e1 + 1
    for s, e in ranges[:-1]:
        assert e - s + 1 == cs
    last = ranges[-1][1] - ranges[-1][0] + 1
    assert cs <= last < 2 * cs


# ---------------------------------------------------------------------------- OAuth


def test_configured_and_auth_url(pub):
    assert pub.is_configured()
    assert not pub.is_connected()
    url = pub.auth_url("st4te")
    u = urlparse(url)
    assert u.scheme == "https" and u.netloc == "www.tiktok.com" and u.path == "/v2/auth/authorize/"
    q = parse_qs(u.query)
    assert q["client_key"] == ["ck123"]
    assert q["scope"] == ["user.info.basic,video.upload,video.publish,video.list"]
    assert q["response_type"] == ["code"]
    assert q["state"] == ["st4te"]
    # TikTok akzeptiert nur https-Redirects
    assert q["redirect_uri"] == ["https://localhost:8420/oauth/tiktok/callback"]
    assert "scope=user.info.basic,video.upload" in url


def test_redirect_uri_env_override(pub, settings, monkeypatch):
    from wdvi.config import Secrets

    monkeypatch.setenv("WDVI_TIKTOK_REDIRECT_URI", "https://wdvi.example.com/oauth/tiktok/callback")
    settings.secrets = Secrets(_env_file=None)  # wie beim Programmstart: Secrets lesen die Umgebung
    assert pub.redirect_uri() == "https://wdvi.example.com/oauth/tiktok/callback"


def test_not_configured(settings):
    p = TikTokPublisher(settings, http=httpx.Client())
    assert not p.is_configured()
    with pytest.raises(PublishError, match="WDVI_TIKTOK_CLIENT_KEY"):
        p.auth_url("x")


def test_handle_callback_exchanges_code(pub, router, settings):
    route = router.post(f"{API}/v2/oauth/token/").respond(
        json={
            "access_token": "act.NEW",
            "expires_in": 86400,
            "open_id": "oid-1",
            "refresh_expires_in": 31536000,
            "refresh_token": "rft.NEW",
            "scope": "user.info.basic,video.upload,video.publish,video.list",
            "token_type": "Bearer",
        }
    )
    pub.handle_callback({"code": "c0de*!", "state": "s", "scopes": "user.info.basic,video.upload"})
    sent = parse_qs(route.calls[0].request.content.decode())
    assert sent == {
        "client_key": ["ck123"],
        "client_secret": ["cs456"],
        "code": ["c0de*!"],
        "grant_type": ["authorization_code"],
        "redirect_uri": ["https://localhost:8420/oauth/tiktok/callback"],
    }
    tok = TokenStore(settings, "tiktok").load()
    assert tok["access_token"] == "act.NEW" and tok["refresh_token"] == "rft.NEW"
    assert tok["expires_at"] == NOW + 86400
    assert tok["refresh_expires_at"] == NOW + 31536000
    assert tok["open_id"] == "oid-1"
    assert pub.is_connected()


def test_handle_callback_errors(pub, router):
    with pytest.raises(PublishError, match="abgebrochen"):
        pub.handle_callback({"error": "access_denied", "error_description": "User cancelled"})
    with pytest.raises(PublishError, match="code"):
        pub.handle_callback({"state": "x"})
    router.post(f"{API}/v2/oauth/token/").respond(
        json={"error": "invalid_grant", "error_description": "Authorization code is expired.", "log_id": "x"}
    )
    with pytest.raises(PublishError, match="expired"):
        pub.handle_callback({"code": "old"})


def test_handle_callback_missing_upload_scope(pub, router, settings):
    router.post(f"{API}/v2/oauth/token/").respond(
        json={
            "access_token": "a",
            "expires_in": 86400,
            "refresh_token": "r",
            "refresh_expires_in": 100,
            "open_id": "o",
            "scope": "user.info.basic",
        }
    )
    with pytest.raises(PublishError, match="video.upload"):
        pub.handle_callback({"code": "c"})
    assert TokenStore(settings, "tiktok").load() is None


def test_token_refresh_before_expiry(pub, router, settings, video):
    seed_token(settings, expires_at=NOW + 60)  # läuft in 1 min ab
    token_route = router.post(f"{API}/v2/oauth/token/").respond(
        json={
            "access_token": "act.REFRESHED",
            "expires_in": 86400,
            "open_id": "oid",
            "refresh_expires_in": 31536000,
            "refresh_token": "rft.ROTATED",
            "scope": "x",
            "token_type": "Bearer",
        }
    )
    init = router.post(f"{API}/v2/post/publish/inbox/video/init/").mock(
        return_value=ok({"publish_id": "v_inbox_file~v2.1", "upload_url": UPLOAD_URL})
    )
    router.put(url__startswith="https://open-upload.tiktokapis.com/").respond(201)
    router.post(f"{API}/v2/post/publish/status/fetch/").mock(
        return_value=ok({"status": "SEND_TO_USER_INBOX"})
    )

    res = pub.publish(req(video))
    assert res.ok, res.error
    sent = parse_qs(token_route.calls[0].request.content.decode())
    assert sent["grant_type"] == ["refresh_token"] and sent["refresh_token"] == ["rft.OLD"]
    assert init.calls[0].request.headers["Authorization"] == "Bearer act.REFRESHED"
    tok = TokenStore(settings, "tiktok").load()
    assert tok["refresh_token"] == "rft.ROTATED" and tok["expires_at"] == NOW + 86400


def test_expired_refresh_token(pub, settings, video):
    seed_token(settings, expires_at=NOW - 10, refresh_expires_at=NOW - 5)
    assert not pub.is_connected()
    res = pub.publish(req(video))
    assert not res.ok and "neu verbinden" in res.error


def test_not_connected_publish_fails_cleanly(pub, video):
    res = pub.publish(req(video))
    assert not res.ok and res.status == "failed" and "nicht verbunden" in res.error


# ---------------------------------------------------------------------------- Inbox


def test_inbox_flow(pub, router, settings, video):
    seed_token(settings)
    init = router.post(f"{API}/v2/post/publish/inbox/video/init/").mock(
        return_value=ok({"publish_id": "v_inbox_file~v2.42", "upload_url": UPLOAD_URL})
    )
    put = router.put(url__startswith="https://open-upload.tiktokapis.com/video/").respond(201)
    status = router.post(f"{API}/v2/post/publish/status/fetch/")
    status.side_effect = [
        ok({"status": "PROCESSING_UPLOAD"}),
        ok({"status": "PROCESSING_UPLOAD"}),
        ok({"status": "SEND_TO_USER_INBOX"}),
    ]

    res = pub.publish(req(video))
    assert res.ok and res.status == "draft_sent", res.error
    assert res.remote_id == "v_inbox_file~v2.42"
    assert res.error is None

    body = json.loads(init.calls[0].request.content)
    assert body == {
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": 10000,
            "chunk_size": 10000,
            "total_chunk_count": 1,
        }
    }
    h = init.calls[0].request.headers
    assert h["Authorization"] == "Bearer act.OLD"
    assert h["Content-Type"].startswith("application/json")

    up = put.calls[0].request
    assert up.headers["Content-Type"] == "video/mp4"
    assert up.headers["Content-Length"] == "10000"
    assert up.headers["Content-Range"] == "bytes 0-9999/10000"
    assert up.content == video.read_bytes()

    assert json.loads(status.calls[0].request.content) == {"publish_id": "v_inbox_file~v2.42"}
    assert status.call_count == 3
    assert pub.fake_clock.sleeps == [5.0, 5.0]


def test_inbox_multi_chunk_upload(pub, router, settings, tmp_path, monkeypatch):
    # Kleine Grenzwerte, damit der Chunk-Pfad ohne 70-MB-Datei getestet wird
    monkeypatch.setattr(tt, "SINGLE_CHUNK_MAX", 1000)
    monkeypatch.setattr(tt, "CHUNK_SIZE", 300)
    seed_token(settings)
    data = bytes(range(256)) * 4 + b"tail-bytes"  # 1034 Bytes -> 3 Chunks: 300, 300, 434
    video = tmp_path / "big.mp4"
    video.write_bytes(data)
    init = router.post(f"{API}/v2/post/publish/inbox/video/init/").mock(
        return_value=ok({"publish_id": "v_inbox_file~v2.7", "upload_url": UPLOAD_URL})
    )
    put = router.put(url__startswith="https://open-upload.tiktokapis.com/video/")
    put.side_effect = [httpx.Response(206), httpx.Response(206), httpx.Response(201)]
    router.post(f"{API}/v2/post/publish/status/fetch/").mock(
        return_value=ok({"status": "SEND_TO_USER_INBOX"})
    )

    res = pub.publish(req(video))
    assert res.ok, res.error
    si = json.loads(init.calls[0].request.content)["source_info"]
    assert si == {"source": "FILE_UPLOAD", "video_size": 1034, "chunk_size": 300, "total_chunk_count": 3}
    ranges = [c.request.headers["Content-Range"] for c in put.calls]
    assert ranges == ["bytes 0-299/1034", "bytes 300-599/1034", "bytes 600-1033/1034"]
    assert b"".join(c.request.content for c in put.calls) == data
    assert [c.request.headers["Content-Length"] for c in put.calls] == ["300", "300", "434"]


def test_upload_retries_server_error(pub, router, settings, video):
    seed_token(settings)
    router.post(f"{API}/v2/post/publish/inbox/video/init/").mock(
        return_value=ok({"publish_id": "p", "upload_url": UPLOAD_URL})
    )
    put = router.put(url__startswith="https://open-upload.tiktokapis.com/")
    put.side_effect = [httpx.Response(503), httpx.ConnectError("boom"), httpx.Response(201)]
    router.post(f"{API}/v2/post/publish/status/fetch/").mock(
        return_value=ok({"status": "SEND_TO_USER_INBOX"})
    )
    assert pub.publish(req(video)).ok
    assert put.call_count == 3


def test_inbox_failed_status(pub, router, settings, video):
    seed_token(settings)
    router.post(f"{API}/v2/post/publish/inbox/video/init/").mock(
        return_value=ok({"publish_id": "p", "upload_url": UPLOAD_URL})
    )
    router.put(url__startswith="https://open-upload.tiktokapis.com/").respond(201)
    router.post(f"{API}/v2/post/publish/status/fetch/").mock(
        return_value=ok({"status": "FAILED", "fail_reason": "frame_rate_check_failed"})
    )
    res = pub.publish(req(video))
    assert not res.ok and res.status == "failed"
    assert "Bildrate" in res.error and "frame_rate_check_failed" in res.error


def test_inbox_poll_timeout_is_ok_with_note(pub, router, settings, video):
    seed_token(settings)
    router.post(f"{API}/v2/post/publish/inbox/video/init/").mock(
        return_value=ok({"publish_id": "p", "upload_url": UPLOAD_URL})
    )
    router.put(url__startswith="https://open-upload.tiktokapis.com/").respond(201)
    router.post(f"{API}/v2/post/publish/status/fetch/").mock(return_value=ok({"status": "PROCESSING_UPLOAD"}))
    res = pub.publish(req(video))
    # Kein Fehler -> kein Doppel-Upload durch Retry; Hinweis im error-Feld
    assert res.ok and res.status == "draft_sent"
    assert "PROCESSING_UPLOAD" in res.error
    assert pub.fake_clock.t >= pub.poll_timeout_s


def test_api_error_code_mapped(pub, router, settings, video):
    seed_token(settings)
    router.post(f"{API}/v2/post/publish/inbox/video/init/").mock(
        return_value=api_err("spam_risk_too_many_pending_share", "too many pending")
    )
    res = pub.publish(req(video))
    assert not res.ok
    assert "Entwürfe" in res.error and "spam_risk_too_many_pending_share" in res.error


def test_error_code_checked_even_on_http_200(pub, router, settings, video):
    seed_token(settings)
    router.post(f"{API}/v2/post/publish/inbox/video/init/").respond(
        200, json={"data": {}, "error": {"code": "rate_limit_exceeded", "message": "slow down"}}
    )
    res = pub.publish(req(video))
    assert not res.ok and "Rate-Limit" in res.error


def test_access_token_invalid_triggers_refresh_and_retry(pub, router, settings, video):
    seed_token(settings)
    router.post(f"{API}/v2/oauth/token/").respond(
        json={
            "access_token": "act.FRESH",
            "expires_in": 86400,
            "refresh_token": "rft.2",
            "refresh_expires_in": 999,
            "open_id": "oid",
        }
    )
    init = router.post(f"{API}/v2/post/publish/inbox/video/init/")
    init.side_effect = [
        api_err("access_token_invalid", status=401),
        ok({"publish_id": "p", "upload_url": UPLOAD_URL}),
    ]
    router.put(url__startswith="https://open-upload.tiktokapis.com/").respond(201)
    router.post(f"{API}/v2/post/publish/status/fetch/").mock(
        return_value=ok({"status": "SEND_TO_USER_INBOX"})
    )
    res = pub.publish(req(video))
    assert res.ok, res.error
    assert init.calls[1].request.headers["Authorization"] == "Bearer act.FRESH"


def test_missing_video_file(pub, tmp_path):
    res = pub.publish(req(tmp_path / "missing.mp4"))
    assert not res.ok and "nicht gefunden" in res.error


def test_errors_never_leak_upload_token(pub, router, settings, video):
    seed_token(settings)
    router.post(f"{API}/v2/post/publish/inbox/video/init/").mock(
        return_value=ok({"publish_id": "p", "upload_url": UPLOAD_URL})
    )
    router.put(url__startswith="https://open-upload.tiktokapis.com/").respond(403, text="denied")
    res = pub.publish(req(video))
    assert not res.ok and "HTTP 403" in res.error
    assert "secret-upload-token" not in res.error and "act.OLD" not in res.error


# ---------------------------------------------------------------------------- Direct


def creator_info(options: list[str], **extra) -> httpx.Response:
    data = {
        "creator_avatar_url": "https://x",
        "creator_username": "wasteddailyvi",
        "creator_nickname": "WD",
        "privacy_level_options": options,
        "comment_disabled": False,
        "duet_disabled": False,
        "stitch_disabled": False,
        "max_video_post_duration_sec": 600,
    }
    data.update(extra)
    return ok(data)


def test_direct_public_post(pub, router, settings, video):
    settings.config.publish.tiktok.mode = "direct"
    settings.config.publish.tiktok.privacy_level = "PUBLIC_TO_EVERYONE"
    seed_token(settings)
    info = router.post(f"{API}/v2/post/publish/creator_info/query/").mock(
        return_value=creator_info(["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"])
    )
    init = router.post(f"{API}/v2/post/publish/video/init/").mock(
        return_value=ok({"publish_id": "v_pub_file~v2-1.99", "upload_url": UPLOAD_URL})
    )
    router.put(url__startswith="https://open-upload.tiktokapis.com/").respond(201)
    status = router.post(f"{API}/v2/post/publish/status/fetch/")
    status.side_effect = [
        ok({"status": "PROCESSING_UPLOAD"}),
        ok({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7412345678901234567]}),
    ]

    res = pub.publish(req(video))
    assert res.ok and res.status == "posted", res.error
    assert res.remote_id == "7412345678901234567"
    assert res.remote_url == "https://www.tiktok.com/@wasteddailyvi/video/7412345678901234567"
    assert res.error is None
    assert info.called
    body = json.loads(init.calls[0].request.content)
    pi = body["post_info"]
    assert pi["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert pi["title"] == "No way 💀\n\nHe did that #GTA6\n\n🎥 Clip: xQc on Twitch"
    assert pi["disable_duet"] is False and pi["disable_comment"] is False and pi["disable_stitch"] is False
    assert pi["video_cover_timestamp_ms"] == 1000
    assert body["source_info"] == {
        "source": "FILE_UPLOAD",
        "video_size": 10000,
        "chunk_size": 10000,
        "total_chunk_count": 1,
    }


def test_direct_privacy_fallback_self_only(pub, router, settings, video, caplog):
    settings.config.publish.tiktok.mode = "direct"
    settings.config.publish.tiktok.privacy_level = "PUBLIC_TO_EVERYONE"
    seed_token(settings)
    router.post(f"{API}/v2/post/publish/creator_info/query/").mock(return_value=creator_info(["SELF_ONLY"]))
    init = router.post(f"{API}/v2/post/publish/video/init/").mock(
        return_value=ok({"publish_id": "v_pub_file~v2-1.5", "upload_url": UPLOAD_URL})
    )
    router.put(url__startswith="https://open-upload.tiktokapis.com/").respond(201)
    router.post(f"{API}/v2/post/publish/status/fetch/").mock(
        return_value=ok({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": []})
    )

    with caplog.at_level(logging.WARNING, logger="wdvi.publish.tiktok"):
        res = pub.publish(req(video))
    assert res.ok and res.status == "posted"
    assert json.loads(init.calls[0].request.content)["post_info"]["privacy_level"] == "SELF_ONLY"
    assert res.remote_id == "v_pub_file~v2-1.5" and res.remote_url is None
    assert "SELF_ONLY" in res.error and "Audit" in res.error
    assert any("Audit" in r.getMessage() for r in caplog.records)


def test_direct_respects_creator_comment_setting(pub, router, settings, video):
    settings.config.publish.tiktok.mode = "direct"
    seed_token(settings)
    router.post(f"{API}/v2/post/publish/creator_info/query/").mock(
        return_value=creator_info(["PUBLIC_TO_EVERYONE"], comment_disabled=True)
    )
    init = router.post(f"{API}/v2/post/publish/video/init/").mock(
        return_value=ok({"publish_id": "p", "upload_url": UPLOAD_URL})
    )
    router.put(url__startswith="https://open-upload.tiktokapis.com/").respond(201)
    router.post(f"{API}/v2/post/publish/status/fetch/").mock(return_value=ok({"status": "PUBLISH_COMPLETE"}))
    assert pub.publish(req(video)).ok
    assert json.loads(init.calls[0].request.content)["post_info"]["disable_comment"] is True


def test_direct_unaudited_error(pub, router, settings, video):
    settings.config.publish.tiktok.mode = "direct"
    seed_token(settings)
    router.post(f"{API}/v2/post/publish/creator_info/query/").mock(return_value=creator_info(["SELF_ONLY"]))
    router.post(f"{API}/v2/post/publish/video/init/").mock(
        return_value=api_err("unaudited_client_can_only_post_to_private_accounts", status=403)
    )
    res = pub.publish(req(video))
    assert not res.ok
    assert "PRIVATES Konto" in res.error and "inbox" in res.error


def test_direct_duration_check(pub, router, settings, sample_video):
    settings.config.publish.tiktok.mode = "direct"
    seed_token(settings)
    router.post(f"{API}/v2/post/publish/creator_info/query/").mock(
        return_value=creator_info(["SELF_ONLY"], max_video_post_duration_sec=5)
    )
    init = router.post(f"{API}/v2/post/publish/video/init/")
    res = pub.publish(req(sample_video))  # 8 s > 5 s
    assert not res.ok and "max. 5 s" in res.error
    assert not init.called


# ---------------------------------------------------------------------------- Metriken


def test_fetch_metrics(pub, router, settings):
    seed_token(settings)
    route = router.post(host="open.tiktokapis.com", path="/v2/video/query/").mock(
        return_value=ok(
            {
                "videos": [
                    {
                        "id": "7412345678901234567",
                        "view_count": 1500,
                        "like_count": 120,
                        "comment_count": 7,
                        "share_count": 3,
                    }
                ],
                "cursor": 0,
                "has_more": False,
            }
        )
    )
    m = pub.fetch_metrics("7412345678901234567")
    assert (m.views, m.likes, m.comments, m.shares) == (1500, 120, 7, 3)
    r = route.calls[0].request
    assert r.url.params["fields"] == "id,view_count,like_count,comment_count,share_count"
    assert json.loads(r.content) == {"filters": {"video_ids": ["7412345678901234567"]}}


def test_fetch_metrics_resolves_publish_id(pub, router, settings):
    seed_token(settings)
    router.post(f"{API}/v2/post/publish/status/fetch/").mock(
        return_value=ok({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [42]})
    )
    router.post(host="open.tiktokapis.com", path="/v2/video/query/").mock(
        return_value=ok(
            {"videos": [{"id": "42", "view_count": 9, "like_count": 1, "comment_count": 0, "share_count": 0}]}
        )
    )
    assert pub.fetch_metrics("v_inbox_file~v2.1").views == 9


def test_fetch_metrics_errors_return_none(pub, router, settings):
    seed_token(settings)
    router.post(host="open.tiktokapis.com", path="/v2/video/query/").mock(
        return_value=api_err("scope_not_authorized")
    )
    assert pub.fetch_metrics("1") is None
    router.post(f"{API}/v2/post/publish/status/fetch/").mock(
        return_value=ok({"status": "SEND_TO_USER_INBOX"})
    )
    assert pub.fetch_metrics("v_inbox_file~x") is None


# ---------------------------------------------------------------------------- nach dem Upload: kein 'failed'
# Ist das Video vollständig bei TikTok, veröffentlicht TikTok es selbst. Ein 'failed' würde die Pipeline zu
# einem zweiten Upload bringen (Doppelpost) – nur TikToks ausdrückliches FAILED zählt als Fehlschlag.


def direct_setup(router, settings):
    settings.config.publish.tiktok.mode = "direct"
    seed_token(settings)
    router.post(f"{API}/v2/post/publish/creator_info/query/").mock(
        return_value=creator_info(["PUBLIC_TO_EVERYONE", "SELF_ONLY"])
    )
    init = router.post(f"{API}/v2/post/publish/video/init/").mock(
        return_value=ok({"publish_id": "v_pub_file~v2-1.77", "upload_url": UPLOAD_URL})
    )
    put = router.put(url__startswith="https://open-upload.tiktokapis.com/").respond(201)
    status = router.post(f"{API}/v2/post/publish/status/fetch/")
    return init, put, status


def test_direct_gateway_error_while_polling_is_not_failed(pub, router, settings, video):
    init, put, status = direct_setup(router, settings)
    status.mock(return_value=httpx.Response(502, text="Bad Gateway"))  # kein JSON -> Code http_502
    res = pub.publish(req(video))
    assert res.ok and res.status == "posted", res.error
    assert res.remote_id == "v_pub_file~v2-1.77" and res.remote_url is None
    assert "nicht abrufbar" in res.error and "prüfen" in res.error and "http_502" in res.error
    assert init.call_count == 1 and put.call_count == 1
    assert status.call_count == tt.POLL_MAX_ERRORS


def test_inbox_token_refresh_error_while_polling_is_not_failed(pub, router, settings, video):
    seed_token(settings)
    router.post(f"{API}/v2/post/publish/inbox/video/init/").mock(
        return_value=ok({"publish_id": "v_inbox_file~v2.9", "upload_url": UPLOAD_URL})
    )
    router.put(url__startswith="https://open-upload.tiktokapis.com/").respond(201)
    status = router.post(f"{API}/v2/post/publish/status/fetch/")
    status.mock(return_value=api_err("access_token_invalid", status=401))
    token = router.post(f"{API}/v2/oauth/token/").respond(
        json={"error": "invalid_grant", "error_description": "Refresh token is invalid"}
    )
    res = pub.publish(req(video))
    assert res.ok and res.status == "draft_sent", res.error
    assert res.remote_id == "v_inbox_file~v2.9"
    assert "nicht abrufbar" in res.error and "Refresh token is invalid" in res.error
    assert token.called
    assert "act.OLD" not in res.error and "rft.OLD" not in res.error


def test_poll_recovers_from_transient_errors(pub, router, settings, video):
    _init, _put, status = direct_setup(router, settings)
    status.side_effect = [
        httpx.ConnectError("dns"),
        httpx.Response(502, text="Bad Gateway"),
        api_err("rate_limit_exceeded", status=429),
        ok({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ["7400000000000000001"]}),
    ]
    res = pub.publish(req(video))
    assert res.ok and res.status == "posted" and res.error is None
    assert res.remote_id == "7400000000000000001"


def test_poll_unexpected_api_error_is_not_failed(pub, router, settings, video):
    _init, _put, status = direct_setup(router, settings)
    status.mock(return_value=api_err("invalid_params", status=400))
    res = pub.publish(req(video))
    assert res.ok and res.status == "posted"
    assert "Status nicht abrufbar" in res.error


def test_explicit_failed_status_still_fails(pub, router, settings, video):
    _init, _put, status = direct_setup(router, settings)
    status.side_effect = [
        httpx.Response(502, text="Bad Gateway"),
        ok({"status": "FAILED", "fail_reason": "spam_risk_text"}),
    ]
    res = pub.publish(req(video))
    assert not res.ok and res.status == "failed" and "Spam" in res.error


def test_upload_error_but_all_bytes_arrived_continues(pub, router, settings, video):
    init, put, status = direct_setup(router, settings)
    # Antwort auf den (einzigen) Chunk geht verloren, der Retry wird abgelehnt – TikTok hat aber alles
    put.side_effect = [httpx.ReadTimeout("lost"), httpx.Response(416), httpx.Response(416)]
    status.side_effect = [
        ok({"status": "PROCESSING_UPLOAD", "uploaded_bytes": 10000}),
        ok({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ["7400000000000000002"]}),
    ]
    res = pub.publish(req(video))
    assert res.ok and res.status == "posted", res.error
    assert res.remote_id == "7400000000000000002"
    assert init.call_count == 1 and put.call_count == 2


def test_upload_error_with_incomplete_upload_fails(pub, router, settings, video):
    _init, put, status = direct_setup(router, settings)
    put.respond(403, text="denied")
    status.mock(return_value=ok({"status": "PROCESSING_UPLOAD", "uploaded_bytes": 0}))
    res = pub.publish(req(video))
    assert not res.ok and res.status == "failed" and "HTTP 403" in res.error
    assert status.call_count == 1


def test_poll_timeout_with_errors_mentions_last_error(pub, router, settings, video):
    _init, _put, status = direct_setup(router, settings)
    pub.poll_timeout_s = 12
    status.side_effect = [ok({"status": "PROCESSING_UPLOAD"})] * 2 + [
        httpx.Response(503, text="unavailable")
    ] * 3
    res = pub.publish(req(video))
    assert res.ok and res.status == "posted"
    assert "PROCESSING_UPLOAD" in res.error and "http_503" in res.error
