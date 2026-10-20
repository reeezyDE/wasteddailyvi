"""KickSource mit injizierter Fake-Session (curl_cffi-ähnlich): Parsing, Fallback, Cloudflare."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from wdvi.schemas import SourceKind
from wdvi.sources.base import SourceError
from wdvi.sources.kick import KickSource

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
PRIMARY = "https://kick.com/api/v2/categories/grand-theft-auto-vi/clips"
FALLBACK = "https://kick.com/api/v2/clips"


class FakeResp:
    def __init__(self, status: int = 200, body=None, text: str | None = None, headers: dict | None = None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else ("" if body is None else "json")
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, routes: dict[str, list[FakeResp] | FakeResp]):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, dict(params or {})))
        r = self.routes.get(url)
        if r is None:
            return FakeResp(404, {"message": "not found"})
        if isinstance(r, list):
            return r.pop(0) if len(r) > 1 else r[0]
        return r


def kclip(cid: str, views: int | None, *, slug: str | None = "streamer1", **kw) -> dict:
    d = {
        "id": cid, "title": f"clip {cid}", "clip_url": f"https://clips.kick.com/clips/{cid}/playlist.m3u8",
        "video_url": f"https://clips.kick.com/clips/{cid}/video.mp4", "thumbnail_url": f"https://t/{cid}.webp",
        "duration": 45, "views": views, "likes": 12, "created_at": "2026-10-20T09:30:00.000000Z",
        "livestream_id": "777", "is_mature": False,
        "category": {"id": 1, "name": "Grand Theft Auto VI", "slug": "grand-theft-auto-vi"},
        "creator": {"id": 5, "username": "clipperguy", "slug": "clipperguy"},
    }
    if slug:
        d["channel"] = {"id": 99, "username": "Streamer1", "slug": slug}
    d.update(kw)
    return d


def test_is_configured(settings):
    assert KickSource(settings, session=FakeSession({})).is_configured() is True


def test_primary_endpoint_parsing_and_filters(settings):
    settings.config.sources.kick.min_views = 300
    sess = FakeSession({PRIMARY: FakeResp(200, {"clips": [
        kclip("clip_A", 5000), kclip("clip_B", 100), kclip("clip_C", None, slug=None, view_count=900),
        {"id": None}, "garbage",
    ], "nextCursor": None})})
    out = KickSource(settings, session=sess).discover(NOW)
    assert [c.source_id for c in out] == ["clip_A", "clip_C"]
    url, params = sess.calls[0]
    assert url == PRIMARY and params == {"cursor": "0", "sort": "view", "time": "day"}

    a = out[0]
    assert a.source == SourceKind.kick_clip
    assert a.url == "https://kick.com/streamer1/clips/clip_A"
    assert a.creator_name == "Streamer1" and a.creator_handle == "streamer1"
    assert a.creator_url == "https://kick.com/streamer1" and a.platform_label == "Kick"
    assert a.view_count == 5000 and a.duration_s == 45.0 and a.engagement == {"likes": 12.0}
    assert a.extra["media_url"] == "https://clips.kick.com/clips/clip_A/playlist.m3u8"
    assert a.created_at == datetime(2026, 10, 20, 9, 30, tzinfo=UTC)
    # ohne Kanal-Slug: direkte Medien-URL als Fallback
    assert out[1].url == "https://clips.kick.com/clips/clip_C/playlist.m3u8"


def test_pagination_follows_cursor(settings):
    sess = FakeSession({PRIMARY: [
        FakeResp(200, {"clips": [kclip("clip_A", 5000)], "nextCursor": "abc"}),
        FakeResp(200, {"clips": [kclip("clip_B", 4000)], "nextCursor": None}),
    ]})
    out = KickSource(settings, session=sess).discover(NOW)
    assert [c.source_id for c in out] == ["clip_A", "clip_B"]
    assert sess.calls[1][1]["cursor"] == "abc"


def test_fallback_endpoint_when_primary_404(settings):
    settings.config.sources.kick.time_range = "week"
    sess = FakeSession({FALLBACK: FakeResp(200, {"data": {"clips": [kclip("clip_X", 1000)]}})})
    out = KickSource(settings, session=sess).discover(NOW)
    assert [c.source_id for c in out] == ["clip_X"]
    assert sess.calls[0][0] == PRIMARY
    assert sess.calls[1] == (FALLBACK, {"category": "grand-theft-auto-vi", "sort": "view", "time": "week"})


def test_cloudflare_403(settings):
    sess = FakeSession({PRIMARY: FakeResp(403, None, text="<html>Just a moment...</html>"),
                        FALLBACK: FakeResp(403, None, text="<html>Just a moment...</html>")})
    with pytest.raises(SourceError, match="Kick blockiert die Anfrage \\(Cloudflare\\)"):
        KickSource(settings, session=sess).discover(NOW)


def test_cloudflare_challenge_with_200(settings):
    html = "<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>"
    sess = FakeSession({PRIMARY: FakeResp(200, None, text=html), FALLBACK: FakeResp(200, None, text=html)})
    with pytest.raises(SourceError, match="Cloudflare"):
        KickSource(settings, session=sess).discover(NOW)


def test_unknown_format_and_errors(settings):
    sess = FakeSession({PRIMARY: FakeResp(200, {"weird": True}), FALLBACK: FakeResp(500, {"x": 1})})
    with pytest.raises(SourceError, match="nicht abrufbar"):
        KickSource(settings, session=sess).discover(NOW)


def test_network_exception(settings):
    class Boom:
        def get(self, *a, **kw):
            raise OSError("connection reset")

    with pytest.raises(SourceError, match="nicht erreichbar"):
        KickSource(settings, session=Boom()).discover(NOW)


def test_one_category_fails_other_succeeds(settings):
    settings.config.sources.kick.category_slugs = ["grand-theft-auto-vi", "gta-v"]
    sess = FakeSession({
        PRIMARY: FakeResp(403, None, text="blocked"), FALLBACK: FakeResp(403, None, text="blocked"),
        "https://kick.com/api/v2/categories/gta-v/clips": FakeResp(200, [kclip("clip_Z", 999)]),
    })
    out = KickSource(settings, session=sess).discover(NOW)
    assert [c.source_id for c in out] == ["clip_Z"]
