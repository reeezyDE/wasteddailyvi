"""TwitchSource: Token-Caching, Paginierung, Filter, Login-Auflösung, 401/429 (alles respx-gemockt)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from wdvi.config import Secrets, Settings
from wdvi.schemas import SourceKind
from wdvi.sources.base import SourceError
from wdvi.sources.twitch import API_BASE, TOKEN_URL, TwitchSource

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def tw_settings(settings: Settings) -> Settings:
    settings.config.sources.twitch.min_views = 300
    settings.config.sources.twitch.languages = ["en"]
    return Settings(config=settings.config,
                    secrets=Secrets(_env_file=None, twitch_client_id="cid", twitch_client_secret="csecret"))


def clip(cid: str, views: int, *, lang: str = "en", bid: str = "100", bname: str = "xQc",
         featured: bool = False, created: str = "2026-10-20T10:00:00Z") -> dict:
    return {
        "id": cid, "url": f"https://clips.twitch.tv/{cid}", "embed_url": f"https://clips.twitch.tv/embed?clip={cid}",
        "broadcaster_id": bid, "broadcaster_name": bname, "creator_id": "9", "creator_name": "clipper",
        "video_id": "555", "game_id": "42", "language": lang, "title": f"title {cid}", "view_count": views,
        "created_at": created, "thumbnail_url": f"https://thumb/{cid}.jpg", "duration": 28.5,
        "vod_offset": 1234, "is_featured": featured,
    }


def mock_api(router: respx.MockRouter, pages: list[list[dict]], users: list[dict] | None = None,
             token_status: int = 200):
    token = router.post(TOKEN_URL).mock(return_value=httpx.Response(
        token_status, json={"access_token": "tok", "expires_in": 5000000, "token_type": "bearer"}
        if token_status == 200 else {"status": 403, "message": "invalid client secret"}))
    games = router.get(f"{API_BASE}/games").mock(return_value=httpx.Response(
        200, json={"data": [{"id": "42", "name": "Grand Theft Auto VI"}]}))

    def clips_side_effect(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        idx = int(after.removeprefix("c")) if after else 0
        body: dict = {"data": pages[idx], "pagination": {}}
        if idx + 1 < len(pages):
            body["pagination"] = {"cursor": f"c{idx + 1}"}
        return httpx.Response(200, json=body)

    clips = router.get(f"{API_BASE}/clips").mock(side_effect=clips_side_effect)
    users_route = router.get(f"{API_BASE}/users").mock(return_value=httpx.Response(
        200, json={"data": users if users is not None else [{"id": "100", "login": "xqc", "display_name": "xQc"}]}))
    return token, games, clips, users_route


def test_is_configured(settings, tw_settings):
    assert TwitchSource(tw_settings, http=httpx.Client()).is_configured()
    src = TwitchSource(settings, http=httpx.Client())
    assert not src.is_configured()
    with pytest.raises(SourceError, match="WDVI_TWITCH_CLIENT_ID"):
        src.discover(NOW)


def test_discover_pagination_filters_and_logins(tw_settings):
    pages = [
        [clip("A", 5000), clip("B", 4000, lang="de"), clip("C", 3000, bid="200", bname="日本語ストリーマー")],
        [clip("D", 2000, featured=True), clip("E", 100)],
    ]
    users = [{"id": "100", "login": "xqc"}, {"id": "200", "login": "jpstreamer"}]
    with respx.mock(assert_all_called=True) as router:
        token, games, clips_route, users_route = mock_api(router, pages, users)
        src = TwitchSource(tw_settings, http=httpx.Client())
        out = src.discover(NOW)

    assert [c.source_id for c in out] == ["A", "C", "D"]  # B: Sprache, E: zu wenige Views
    assert clips_route.call_count == 2
    first = clips_route.calls[0].request.url.params
    assert first["game_id"] == "42" and first["first"] == "100"
    assert first["started_at"] == "2026-10-19T12:00:00Z" and first["ended_at"] == "2026-10-20T12:00:00Z"
    assert clips_route.calls[1].request.url.params["after"] == "c1"
    hdrs = clips_route.calls[0].request.headers
    assert hdrs["Client-Id"] == "cid" and hdrs["Authorization"] == "Bearer tok"
    # Login-Auflösung gebündelt in einem Aufruf
    assert users_route.call_count == 1
    assert sorted(users_route.calls[0].request.url.params.get_list("id")) == ["100", "200"]

    a = out[0]
    assert a.source == SourceKind.twitch_clip
    assert a.url == "https://clips.twitch.tv/A"
    assert a.creator_name == "xQc" and a.creator_handle == "xqc" and a.creator_url == "https://twitch.tv/xqc"
    assert a.platform_label == "Twitch" and a.view_count == 5000 and a.duration_s == 28.5
    assert a.created_at == datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    for k in ("broadcaster_id", "broadcaster_login", "video_id", "vod_offset", "clip_creator_name", "is_featured"):
        assert k in a.extra
    assert a.extra["broadcaster_login"] == "xqc" and a.extra["vod_offset"] == 1234
    assert a.extra["clip_creator_name"] == "clipper"
    c = out[1]
    assert c.creator_name == "日本語ストリーマー" and c.creator_url == "https://twitch.tv/jpstreamer"
    assert out[2].extra["is_featured"] is True


def test_token_cached_across_polls_and_games_cached(tw_settings):
    with respx.mock(assert_all_called=False) as router:
        token, games, clips_route, users_route = mock_api(router, [[clip("A", 5000)]])
        src = TwitchSource(tw_settings, http=httpx.Client())
        src.discover(NOW)
        src.discover(NOW)
    assert token.call_count == 1
    assert games.call_count == 1
    assert users_route.call_count == 1  # Login gecacht
    assert clips_route.call_count == 2


def test_token_refreshed_when_expired(tw_settings):
    with respx.mock(assert_all_called=False) as router:
        token, *_ = mock_api(router, [[clip("A", 5000)]])
        src = TwitchSource(tw_settings, http=httpx.Client())
        t = [1000.0]
        src._clock = lambda: t[0]
        src.discover(NOW)
        t[0] += 5000000  # weit nach Ablauf
        src.discover(NOW)
    assert token.call_count == 2


def test_refresh_once_on_401(tw_settings):
    with respx.mock(assert_all_called=False) as router:
        token, games, clips_route, _ = mock_api(router, [[clip("A", 5000)]])
        games.side_effect = [httpx.Response(401, json={"message": "Invalid OAuth token"}),
                             httpx.Response(200, json={"data": [{"id": "42", "name": "Grand Theft Auto VI"}]})]
        out = TwitchSource(tw_settings, http=httpx.Client()).discover(NOW)
    assert token.call_count == 2
    assert [c.source_id for c in out] == ["A"]


def test_persistent_401_raises(tw_settings):
    with respx.mock(assert_all_called=False) as router:
        _, games, *_ = mock_api(router, [[clip("A", 5000)]])
        games.mock(return_value=httpx.Response(401, json={"message": "Invalid OAuth token"}))
        with pytest.raises(SourceError, match="401"):
            TwitchSource(tw_settings, http=httpx.Client()).discover(NOW)


def test_429_waits_for_ratelimit_reset(tw_settings, monkeypatch):
    monkeypatch.setattr("wdvi.sources._util.time.time", lambda: 1_000_000.0)
    with respx.mock(assert_all_called=False) as router:
        _, _, clips_route, _ = mock_api(router, [[clip("A", 5000)]])
        ok = clips_route.side_effect
        responses = iter([httpx.Response(429, headers={"Ratelimit-Reset": "1000007"}, json={})])

        def side(request):
            try:
                return next(responses)
            except StopIteration:
                return ok(request)

        clips_route.side_effect = side
        src = TwitchSource(tw_settings, http=httpx.Client())
        slept: list[float] = []
        src._sleep = slept.append
        out = src.discover(NOW)
    assert slept == [7.0]
    assert len(out) == 1


def test_bad_credentials_message(tw_settings):
    with respx.mock(assert_all_called=False) as router:
        mock_api(router, [[]], token_status=403)
        with pytest.raises(SourceError, match="Twitch-Anmeldung fehlgeschlagen.*invalid client secret"):
            TwitchSource(tw_settings, http=httpx.Client()).discover(NOW)


def test_unknown_game(tw_settings):
    with respx.mock(assert_all_called=False) as router:
        _, games, *_ = mock_api(router, [[]])
        games.mock(return_value=httpx.Response(200, json={"data": []}))
        with pytest.raises(SourceError, match="game_names"):
            TwitchSource(tw_settings, http=httpx.Client()).discover(NOW)


def test_login_lookup_failure_falls_back_to_display_name(tw_settings):
    with respx.mock(assert_all_called=False) as router:
        _, _, _, users_route = mock_api(router, [[clip("A", 5000, bname="SomeStreamer")]])
        users_route.mock(return_value=httpx.Response(500, json={"message": "boom"}))
        src = TwitchSource(tw_settings, http=httpx.Client())
        src._sleep = lambda s: None
        out = src.discover(NOW)
    assert out[0].creator_handle == "somestreamer"
    assert out[0].creator_url == "https://twitch.tv/somestreamer"


def test_max_clips_per_poll_and_early_stop(tw_settings):
    tw_settings.config.sources.twitch.max_clips_per_poll = 2
    with respx.mock(assert_all_called=False) as router:
        _, _, clips_route, _ = mock_api(router, [[clip("A", 5000), clip("B", 4000), clip("C", 3000)],
                                                 [clip("D", 2000)]])
        out = TwitchSource(tw_settings, http=httpx.Client()).discover(NOW)
    assert [c.source_id for c in out] == ["A", "B"]
    assert clips_route.call_count == 1

    tw_settings.config.sources.twitch.max_clips_per_poll = 100
    with respx.mock(assert_all_called=False) as router:
        _, _, clips_route, _ = mock_api(router, [[clip("A", 5000), clip("B", 200)], [clip("C", 100)]])
        out = TwitchSource(tw_settings, http=httpx.Client()).discover(NOW)
    assert [c.source_id for c in out] == ["A"]
    assert clips_route.call_count == 1  # letzte Seite schon unter min_views -> keine weitere Seite


def test_network_error_becomes_source_error(tw_settings):
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("no route"))
        with pytest.raises(SourceError, match="nicht erreichbar"):
            TwitchSource(tw_settings, http=httpx.Client()).discover(NOW)


def test_clip_broadcasters_lookup(settings, tw_settings):
    """Für Reddit-Links auf clips.twitch.tv/<slug>: Slug -> (Anzeigename, Login), gebündelt."""
    data = [clip("SlugA", 10, bid="100", bname="xQc"), clip("SlugB", 10, bid="200", bname="日本語ストリーマー")]
    with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 9999}))
        clips_route = router.get(f"{API_BASE}/clips").mock(return_value=httpx.Response(200, json={"data": data}))
        router.get(f"{API_BASE}/users").mock(return_value=httpx.Response(
            200, json={"data": [{"id": "100", "login": "xqc"}, {"id": "200", "login": "jpstreamer"}]}))
        src = TwitchSource(tw_settings, http=httpx.Client())
        out = src.clip_broadcasters(["SlugA", "SlugB", "Gone", "SlugA", ""])
    assert out == {"SlugA": ("xQc", "xqc"), "SlugB": ("日本語ストリーマー", "jpstreamer")}
    assert clips_route.calls[0].request.url.params.get_list("id") == ["SlugA", "SlugB", "Gone"]
    assert TwitchSource(settings, http=httpx.Client()).clip_broadcasters(["SlugA"]) == {}  # ohne Credentials
