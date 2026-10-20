"""RedditSource: OAuth-Flow, öffentlicher Fallback, Filter (NSFW, Sticky, Score, Leak-Flair), Links."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from wdvi.config import Secrets, Settings
from wdvi.schemas import CandidateIn, SourceKind
from wdvi.sources.base import SourceError
from wdvi.sources.reddit import (
    OAUTH_BASE,
    OEMBED_URL,
    PUBLIC_BASE,
    TOKEN_URL,
    RedditSource,
    credit_linked_creator,
    linked_creator_from_info,
)
from wdvi.sources.twitch import API_BASE as TWITCH_API
from wdvi.sources.twitch import TOKEN_URL as TWITCH_TOKEN_URL

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
CREATED = datetime(2026, 10, 20, 9, 0, tzinfo=UTC).timestamp()


@pytest.fixture
def rd_settings(settings: Settings) -> Settings:
    settings.config.sources.reddit.min_score = 300
    return Settings(config=settings.config,
                    secrets=Secrets(_env_file=None, reddit_client_id="rid", reddit_client_secret="rsecret"))


def post(pid: str, score: int = 1000, **kw) -> dict:
    d = {
        "id": pid, "name": f"t3_{pid}", "title": f"Insane GTA 6 moment {pid}", "author": "gamer42",
        "subreddit": "GTA6", "permalink": f"/r/GTA6/comments/{pid}/insane_moment/",
        "url": f"https://v.redd.it/{pid}xyz", "domain": "v.redd.it", "is_video": True, "post_hint": "hosted:video",
        "media": {"reddit_video": {"fallback_url": f"https://v.redd.it/{pid}xyz/DASH_720.mp4?source=fallback",
                                   "hls_url": f"https://v.redd.it/{pid}xyz/HLSPlaylist.m3u8", "duration": 24}},
        "over_18": False, "stickied": False, "score": score, "num_comments": 120, "upvote_ratio": 0.97,
        "created_utc": CREATED, "link_flair_text": "Discussion",
        "preview": {"images": [{"source": {"url": "https://preview.redd.it/x.jpg?width=640&s=abc"}}]},
        "thumbnail": "https://b.thumbs.redditmedia.com/t.jpg",
    }
    d.update(kw)
    return d


def link_post(pid: str, url: str, domain: str, **kw) -> dict:
    return post(pid, url=url, domain=domain, is_video=False, post_hint="rich:video", media=None, **kw)


def listing(*posts: dict) -> dict:
    return {"kind": "Listing", "data": {"after": None, "children": [{"kind": "t3", "data": p} for p in posts]}}


ALL_POSTS = [
    post("vid1"),
    post("nsfw", over_18=True),
    post("sticky", stickied=True),
    post("low", score=50),
    post("leak1", link_flair_text="LEAK 🚨"),
    post("leak2", link_flair_text="Leaked Footage"),
    post("leaktitle", title="GTA 6 leaked gameplay!!"),
    post("text", is_video=False, post_hint="self", domain="self.GTA6", media=None, url="https://www.reddit.com/r/x"),
    post("img", is_video=False, post_hint="image", domain="i.redd.it", media=None, url="https://i.redd.it/a.jpg"),
    link_post("twitch1", "https://clips.twitch.tv/FunnyClipSlug", "clips.twitch.tv"),
    link_post("twitch2", "https://www.twitch.tv/xqc/clip/OtherSlug-abc", "twitch.tv"),
    link_post("kick1", "https://kick.com/streamer/clips/clip_01ABC", "kick.com"),
    link_post("kicklive", "https://kick.com/streamer", "kick.com"),
    link_post("yt1", "https://youtu.be/abcdEFGhijk", "youtu.be"),
    link_post("ytshort", "https://www.youtube.com/shorts/abcdEFGhijk", "youtube.com"),
    link_post("ytchan", "https://www.youtube.com/@somechannel", "youtube.com"),
    link_post("stream1", "https://streamable.com/xyz12", "streamable.com"),
    post("xpost", is_video=False, post_hint=None, media=None, url="/r/GTA6/comments/orig/",
         crosspost_parent_list=[post("orig")]),
    post("deleted", author="[deleted]"),
]


def mock_oauth(router: respx.MockRouter, hot: dict, top: dict | None = None):
    token = router.post(TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "rtok", "token_type": "bearer", "expires_in": 86400, "scope": "*"}))
    hot_r = router.get(f"{OAUTH_BASE}/r/GTA6/hot").mock(return_value=httpx.Response(200, json=hot))
    top_r = router.get(f"{OAUTH_BASE}/r/GTA6/top").mock(
        return_value=httpx.Response(200, json=top if top is not None else listing()))
    return token, hot_r, top_r


def test_is_configured(settings):
    assert RedditSource(settings, http=httpx.Client()).is_configured()  # öffentlicher Fallback
    settings.config.sources.reddit.subreddits = []
    assert not RedditSource(settings, http=httpx.Client()).is_configured()


def mock_oembed(router: respx.MockRouter, status: int = 200):
    return router.get(OEMBED_URL).mock(return_value=httpx.Response(status, json={
        "title": "crazy jump", "author_name": "Some Channel", "author_url": "https://www.youtube.com/@SomeChannel",
        "type": "video"} if status == 200 else None))


def test_oauth_flow_and_filters(rd_settings):
    with respx.mock(assert_all_called=True) as router:
        token, hot, top = mock_oauth(router, listing(*ALL_POSTS), listing(post("topvid", score=5000), post("vid1")))
        oembed = mock_oembed(router)
        src = RedditSource(rd_settings, http=httpx.Client())
        out = src.discover(NOW)

    ids = [c.source_id for c in out]
    assert sorted(ids) == sorted(["vid1", "twitch1", "twitch2", "kick1", "yt1", "ytshort", "stream1", "xpost",
                                  "topvid"])
    assert ids[0] == "topvid"  # nach Score sortiert
    # Token: einmal, Basic-Auth + User-Agent
    assert token.call_count == 1
    treq = token.calls[0].request
    assert treq.headers["authorization"].startswith("Basic ")
    assert treq.headers["user-agent"] == rd_settings.config.sources.reddit.user_agent
    assert b"grant_type=client_credentials" in treq.content
    hreq = hot.calls[0].request
    assert hreq.headers["authorization"] == "Bearer rtok"
    assert hreq.url.params["limit"] == "100" and "t" not in hreq.url.params
    assert top.calls[0].request.url.params["t"] == "day"

    by_id = {c.source_id: c for c in out}
    v = by_id["vid1"]
    assert v.source == SourceKind.reddit_post
    assert v.url == "https://www.reddit.com/r/GTA6/comments/vid1/insane_moment/"
    assert v.creator_name == "u/gamer42" and v.creator_url == "https://reddit.com/u/gamer42"
    assert v.platform_label == "Reddit" and v.view_count is None and v.duration_s == 24.0
    assert v.engagement == {"score": 1000.0, "num_comments": 120.0, "upvote_ratio": 0.97}
    assert v.created_at == datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    assert v.thumbnail_url == "https://preview.redd.it/x.jpg?width=640&s=abc"
    assert v.extra["media_url"].endswith("HLSPlaylist.m3u8")
    assert "linked_url" not in v.extra

    assert by_id["twitch1"].extra["linked_url"] == "https://clips.twitch.tv/FunnyClipSlug"
    assert by_id["twitch1"].extra["linked_platform"] == "Twitch"
    assert by_id["twitch2"].extra["linked_platform"] == "Twitch"
    assert by_id["kick1"].extra["linked_platform"] == "Kick"
    assert by_id["yt1"].extra["linked_platform"] == "YouTube"
    assert by_id["stream1"].extra["linked_url"] == "https://streamable.com/xyz12"
    assert "linked_platform" not in by_id["stream1"].extra
    assert by_id["xpost"].duration_s == 24.0  # Video aus dem Crosspost-Original

    # Credit: verlinkte Clips -> Original-Creator, der Reddit-Poster bleibt als "via" erhalten
    t2 = by_id["twitch2"]
    assert (t2.creator_name, t2.creator_handle, t2.creator_url, t2.platform_label) == \
        ("xqc", "xqc", "https://twitch.tv/xqc", "Twitch")
    assert t2.extra["via"] == "u/gamer42" and t2.extra["via_url"] == "https://reddit.com/u/gamer42"
    assert t2.extra["author"] == "gamer42"
    k1 = by_id["kick1"]
    assert (k1.creator_name, k1.creator_url, k1.platform_label) == ("streamer", "https://kick.com/streamer", "Kick")
    for vid in ("yt1", "ytshort"):  # YouTube-URLs nennen keinen Kanal -> oEmbed (gleiches Video: ein Aufruf)
        y = by_id[vid]
        assert (y.creator_name, y.creator_handle, y.creator_url, y.platform_label) == \
            ("Some Channel", "somechannel", "https://www.youtube.com/@SomeChannel", "YouTube")
        assert y.extra["via"] == "u/gamer42"
    assert oembed.call_count == 1
    assert oembed.calls[0].request.url.params["url"] == "https://www.youtube.com/watch?v=abcdEFGhijk"
    # clips.twitch.tv/<slug> ohne Twitch-Credentials: Kanal unbekannt -> ehrlicher Reddit-Credit
    t1 = by_id["twitch1"]
    assert (t1.creator_name, t1.platform_label) == ("u/gamer42", "Reddit") and "via" not in t1.extra
    for pid in ("vid1", "stream1", "xpost"):  # eigene Uploads / Streamable: Credit an den Poster
        assert by_id[pid].platform_label == "Reddit" and by_id[pid].creator_name == "u/gamer42"
        assert "via" not in by_id[pid].extra


@pytest.fixture
def rd_tw_settings(rd_settings: Settings) -> Settings:
    return Settings(config=rd_settings.config,
                    secrets=Secrets(_env_file=None, reddit_client_id="rid", reddit_client_secret="rsecret",
                                    twitch_client_id="cid", twitch_client_secret="csecret"))


def mock_twitch(router: respx.MockRouter, clips: list[dict], users: list[dict]):
    router.post(TWITCH_TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "ttok", "expires_in": 5000000, "token_type": "bearer"}))
    clips_r = router.get(f"{TWITCH_API}/clips").mock(return_value=httpx.Response(200, json={"data": clips}))
    users_r = router.get(f"{TWITCH_API}/users").mock(return_value=httpx.Response(200, json={"data": users}))
    return clips_r, users_r


def test_twitch_links_resolved_via_helix_and_cached(rd_tw_settings):
    posts = [link_post("t1", "https://clips.twitch.tv/FunnyClipSlug", "clips.twitch.tv"),
             link_post("t2", "https://www.twitch.tv/xqc/clip/OtherSlug-abc", "twitch.tv"),
             link_post("t3", "https://clips.twitch.tv/DeletedSlug", "clips.twitch.tv")]
    helix = [{"id": "FunnyClipSlug", "broadcaster_id": "1", "broadcaster_name": "Shroud"},
             {"id": "OtherSlug-abc", "broadcaster_id": "2", "broadcaster_name": "xQc"}]
    with respx.mock(assert_all_called=True) as router:
        mock_oauth(router, listing(*posts))
        clips_r, users_r = mock_twitch(router, helix, [{"id": "1", "login": "shroud"}, {"id": "2", "login": "xqc"}])
        src = RedditSource(rd_tw_settings, http=httpx.Client())
        out = {c.source_id: c for c in src.discover(NOW)}
        again = {c.source_id: c for c in src.discover(NOW)}

    assert clips_r.call_count == 1  # gebündelt und über Polls gecacht (auch "nicht gefunden")
    assert sorted(clips_r.calls[0].request.url.params.get_list("id")) == \
        ["DeletedSlug", "FunnyClipSlug", "OtherSlug-abc"]
    for res in (out, again):
        assert (res["t1"].creator_name, res["t1"].creator_handle, res["t1"].creator_url,
                res["t1"].platform_label) == ("Shroud", "shroud", "https://twitch.tv/shroud", "Twitch")
        assert res["t1"].extra["via"] == "u/gamer42"
        assert (res["t2"].creator_name, res["t2"].creator_url) == ("xQc", "https://twitch.tv/xqc")  # Anzeigename
        assert (res["t3"].creator_name, res["t3"].platform_label) == ("u/gamer42", "Reddit")


def test_creator_lookup_failures_are_not_fatal(rd_tw_settings):
    posts = [link_post("t1", "https://clips.twitch.tv/FunnyClipSlug", "clips.twitch.tv"),
             link_post("t2", "https://www.twitch.tv/xqc/clip/OtherSlug-abc", "twitch.tv"),
             link_post("y1", "https://youtu.be/abcdEFGhijk", "youtu.be"),
             link_post("y2", "https://www.youtube.com/watch?v=zyxwVUTsrqp", "youtube.com")]
    with respx.mock(assert_all_called=False) as router:
        mock_oauth(router, listing(*posts))
        router.post(TWITCH_TOKEN_URL).mock(return_value=httpx.Response(
            200, json={"access_token": "ttok", "expires_in": 5000000}))
        router.get(f"{TWITCH_API}/clips").mock(return_value=httpx.Response(503))
        oembed = router.get(OEMBED_URL).mock(side_effect=httpx.ConnectError("offline"))
        src = RedditSource(rd_tw_settings, http=httpx.Client())
        src._sleep = lambda _s: None
        out = {c.source_id: c for c in src.discover(NOW)}
        assert oembed.call_count == 1  # Netz weg -> keine weiteren oEmbed-Versuche in diesem Poll
        # nächster Poll: wieder erreichbar, das gelöschte/private Video wird nicht erneut gefragt
        chan_b = {"author_name": "Chan B", "author_url": "https://www.youtube.com/channel/UCx"}
        oembed.side_effect = [httpx.Response(404), httpx.Response(200, json=chan_b)]
        second = {c.source_id: c for c in src.discover(NOW)}
        third = {c.source_id: c for c in src.discover(NOW)}

    assert set(out) == {"t1", "t2", "y1", "y2"}
    assert (out["t1"].creator_name, out["t1"].platform_label) == ("u/gamer42", "Reddit")
    assert (out["t2"].creator_name, out["t2"].platform_label) == ("xqc", "Twitch")  # Kanal aus der URL
    assert out["y1"].platform_label == "Reddit" and out["y2"].platform_label == "Reddit"
    assert second["y1"].platform_label == "Reddit"  # 404 -> Reddit-Credit, gecacht
    assert (second["y2"].creator_name, second["y2"].creator_handle, second["y2"].creator_url) == \
        ("Chan B", None, "https://www.youtube.com/channel/UCx")
    assert oembed.call_count == 3 and third["y2"].creator_name == "Chan B"


def _linked(url: str, platform: str = "Twitch") -> CandidateIn:
    return CandidateIn(source=SourceKind.reddit_post, source_id="p1", url="https://www.reddit.com/r/GTA6/comments/p1/",
                       title="t", creator_name="u/clipposter", creator_handle="clipposter",
                       creator_url="https://reddit.com/u/clipposter", platform_label="Reddit",
                       created_at=NOW, extra={"author": "clipposter", "linked_url": url, "linked_platform": platform})


def test_credit_linked_creator_from_url_and_ytdlp_info():
    c = _linked("https://www.twitch.tv/shroud/clip/FunnyBigLlamaPogChamp")
    credited = credit_linked_creator(c)
    assert (credited.creator_name, credited.creator_handle, credited.creator_url, credited.platform_label) == \
        ("shroud", "shroud", "https://twitch.tv/shroud", "Twitch")
    assert credited.extra["via"] == "u/clipposter" and c.platform_label == "Reddit"  # Original unverändert
    # yt-dlp-Info nach dem Download: bei Twitch-Clips ist 'uploader' der Clipper, nicht der Streamer
    info = {"channel": "Shroud", "channel_id": "37402112", "uploader": "some_clipper", "uploader_id": "9"}
    from_info = credit_linked_creator(credited, info)
    assert (from_info.creator_name, from_info.creator_handle) == ("Shroud", "shroud")
    assert from_info.extra["via"] == "u/clipposter"
    assert credit_linked_creator(from_info) is from_info  # ohne Info: schon gutgeschrieben -> bleibt
    jp = credit_linked_creator(credited, {"channel": "日本語ストリーマー"})  # Info ohne Login
    assert (jp.creator_name, jp.creator_handle, jp.creator_url) == \
        ("日本語ストリーマー", "shroud", "https://twitch.tv/shroud")

    yt = _linked("https://youtu.be/abcdEFGhijk", "YouTube")
    assert credit_linked_creator(yt) is yt  # URL nennt keinen Kanal
    yt_info = {"channel": "GTA Guy", "uploader": "GTA Guy", "uploader_id": "@GtaGuy",
               "uploader_url": "https://www.youtube.com/@GtaGuy", "channel_url": "https://www.youtube.com/channel/UC1"}
    y = credit_linked_creator(yt, yt_info)
    assert (y.creator_name, y.creator_handle, y.creator_url, y.platform_label) == \
        ("GTA Guy", "gtaguy", "https://www.youtube.com/@GtaGuy", "YouTube")

    kick = credit_linked_creator(_linked("https://kick.com/xqc/clips/clip_01ABC", "Kick"),
                                 {"channel": "xqc", "uploader": "A_Clipper"})
    assert (kick.creator_name, kick.creator_url, kick.platform_label) == ("xqc", "https://kick.com/xqc", "Kick")

    # nicht betroffen: Streamable-Links, eigene Reddit-Uploads, andere Quellen
    stream = _linked("https://streamable.com/xyz12").model_copy(
        update={"extra": {"author": "clipposter", "linked_url": "https://streamable.com/xyz12"}})
    assert credit_linked_creator(stream, {"channel": "x"}) is stream
    tw = c.model_copy(update={"source": SourceKind.twitch_clip})
    assert credit_linked_creator(tw, info) is tw
    assert linked_creator_from_info("Twitch", {"uploader": "clipper_only"}) is None


def test_leak_title_allowed_when_policy_allow(rd_settings):
    rd_settings.config.filters.leak_policy = "allow"
    with respx.mock(assert_all_called=False) as router:
        mock_oauth(router, listing(post("leaktitle", title="GTA 6 leaked gameplay"), post("leakflair",
                                                                                         link_flair_text="leak")))
        out = RedditSource(rd_settings, http=httpx.Client()).discover(NOW)
    assert [c.source_id for c in out] == ["leaktitle"]  # Flair "leak" wird IMMER übersprungen


def test_token_cached_and_refreshed_on_401(rd_settings):
    with respx.mock(assert_all_called=False) as router:
        token, hot, top = mock_oauth(router, listing(post("vid1")))
        src = RedditSource(rd_settings, http=httpx.Client())
        src.discover(NOW)
        src.discover(NOW)
        assert token.call_count == 1
        hot.side_effect = [httpx.Response(401, json={"message": "Unauthorized"}),
                           httpx.Response(200, json=listing(post("vid1")))]
        out = src.discover(NOW)
        assert token.call_count == 2
        assert [c.source_id for c in out] == ["vid1"]


def test_bad_credentials(rd_settings):
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"message": "Unauthorized", "error": 401}))
        with pytest.raises(SourceError, match="WDVI_REDDIT_CLIENT_ID"):
            RedditSource(rd_settings, http=httpx.Client()).discover(NOW)


def test_public_fallback_without_credentials(settings):
    with respx.mock(assert_all_called=False) as router:
        token = router.post(TOKEN_URL)
        hot = router.get(f"{PUBLIC_BASE}/r/GTA6/hot.json").mock(
            return_value=httpx.Response(200, json=listing(post("vid1"))))
        router.get(f"{PUBLIC_BASE}/r/GTA6/top.json").mock(return_value=httpx.Response(200, json=listing()))
        out = RedditSource(settings, http=httpx.Client()).discover(NOW)
    assert not token.called
    assert [c.source_id for c in out] == ["vid1"]
    req = hot.calls[0].request
    assert req.headers["user-agent"] == settings.config.sources.reddit.user_agent
    assert "authorization" not in req.headers


def test_public_fallback_blocked(settings):
    with respx.mock(assert_all_called=False) as router:
        router.get(host="www.reddit.com").mock(return_value=httpx.Response(403, text="<html>blocked</html>"))
        with pytest.raises(SourceError, match="Reddit blockiert anonyme Anfragen"):
            RedditSource(settings, http=httpx.Client()).discover(NOW)


def test_rate_limit_waits_then_retries(rd_settings):
    with respx.mock(assert_all_called=False) as router:
        _, hot, _ = mock_oauth(router, listing())
        hot.side_effect = [httpx.Response(429, headers={"x-ratelimit-reset": "12"}),
                           httpx.Response(200, json=listing(post("vid1")))]
        src = RedditSource(rd_settings, http=httpx.Client())
        slept: list[float] = []
        src._sleep = slept.append
        out = src.discover(NOW)
    assert slept == [12.0]
    assert [c.source_id for c in out] == ["vid1"]


def test_one_listing_fails_other_ok(rd_settings):
    with respx.mock(assert_all_called=False) as router:
        _, hot, _ = mock_oauth(router, listing(), listing(post("topvid")))
        hot.mock(return_value=httpx.Response(404))
        out = RedditSource(rd_settings, http=httpx.Client()).discover(NOW)
    assert [c.source_id for c in out] == ["topvid"]


def test_garbage_response(rd_settings):
    with respx.mock(assert_all_called=False) as router:
        _, hot, top = mock_oauth(router, listing())
        hot.mock(return_value=httpx.Response(200, text="<html>nope</html>"))
        top.mock(return_value=httpx.Response(200, json={"kind": "Listing"}))
        with pytest.raises(SourceError, match="keine gültige Listing-Antwort"):
            RedditSource(rd_settings, http=httpx.Client()).discover(NOW)
