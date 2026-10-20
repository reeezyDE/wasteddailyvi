"""YouTubeSource (RSS + Data-API-Suche), json3/VTT-Parser, fetch_video_context, make_moment_candidate."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from wdvi.config import Secrets, Settings
from wdvi.schemas import CandidateIn, SourceKind
from wdvi.sources import youtube as yt
from wdvi.sources.base import SourceError
from wdvi.sources.youtube import (
    API_BASE,
    RSS_URL,
    YouTubeSource,
    fetch_video_context,
    make_moment_candidate,
    parse_iso_duration,
    parse_json3,
    parse_vtt,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def entry(vid: str, published: str, *, shorts: bool = False, views: int | None = 4567) -> str:
    link = f"https://www.youtube.com/shorts/{vid}" if shorts else f"https://www.youtube.com/watch?v={vid}"
    stats = f'<media:statistics views="{views}"/>' if views is not None else ""
    return f"""
 <entry>
  <id>yt:video:{vid}</id><yt:videoId>{vid}</yt:videoId><yt:channelId>UCchan1</yt:channelId>
  <title>GTA 6 &amp; chaos {vid}</title>
  <link rel="alternate" href="{link}"/>
  <author><name>Chan &amp; Co</name><uri>https://www.youtube.com/channel/UCchan1</uri></author>
  <published>{published}</published><updated>{published}</updated>
  <media:group>
   <media:title>GTA 6 {vid}</media:title>
   <media:thumbnail url="https://i1.ytimg.com/vi/{vid}/hqdefault.jpg" width="480" height="360"/>
   <media:description>desc</media:description>
   <media:community><media:starRating count="123" average="5.00" min="1" max="5"/>{stats}</media:community>
  </media:group>
 </entry>"""


def feed(*entries: str) -> bytes:
    return ("""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
 <title>Chan &amp; Co</title>""" + "".join(entries) + "\n</feed>").encode()


@pytest.fixture
def yt_settings(settings: Settings) -> Settings:
    settings.config.sources.youtube.channel_ids = ["UCchan1"]
    settings.config.sources.youtube.max_age_hours = 48
    return settings


def with_key(s: Settings) -> Settings:
    return Settings(config=s.config, secrets=Secrets(_env_file=None, youtube_api_key="APIKEY123"))


# --------------------------------------------------------------------------- RSS


def test_is_configured(settings, yt_settings):
    settings_empty = Settings(config=settings.config.model_copy(deep=True), secrets=settings.secrets)
    settings_empty.config.sources.youtube.channel_ids = ["@handle_only"]
    src = YouTubeSource(settings_empty, http=httpx.Client())
    assert not src.is_configured()
    with pytest.raises(SourceError, match="channel_ids"):
        src.discover(NOW)
    assert YouTubeSource(yt_settings, http=httpx.Client()).is_configured()
    q_only = Settings(config=settings_empty.config, secrets=Secrets(_env_file=None, youtube_api_key="k"))
    q_only.config.sources.youtube.search_queries = ["gta 6"]
    assert YouTubeSource(q_only, http=httpx.Client()).is_configured()


def test_rss_discover_filters_shorts_and_age(yt_settings):
    body = feed(entry("NEW1", "2026-10-20T08:00:00+00:00"),
                entry("SHORT", "2026-10-20T09:00:00+00:00", shorts=True),
                entry("OLD1", "2026-10-10T08:00:00+00:00"),
                entry("NOV", "2026-10-19T20:00:00+00:00", views=None))
    with respx.mock(assert_all_called=True) as router:
        route = router.get(RSS_URL).mock(return_value=httpx.Response(200, content=body))
        out = YouTubeSource(yt_settings, http=httpx.Client()).discover(NOW)
    assert route.calls[0].request.url.params["channel_id"] == "UCchan1"
    assert [c.source_id for c in out] == ["NEW1", "NOV"]
    c = out[0]
    assert c.source == SourceKind.youtube_video
    assert c.url == "https://www.youtube.com/watch?v=NEW1"
    assert c.title == "GTA 6 & chaos NEW1" and c.creator_name == "Chan & Co"
    assert c.creator_url == "https://www.youtube.com/channel/UCchan1" and c.platform_label == "YouTube"
    assert c.view_count == 4567 and c.duration_s is None and c.engagement == {"likes": 123.0}
    assert c.thumbnail_url == "https://i1.ytimg.com/vi/NEW1/hqdefault.jpg"
    assert c.created_at == datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    assert out[1].view_count is None


def test_rss_partial_failure_and_total_failure(yt_settings):
    yt_settings.config.sources.youtube.channel_ids = ["UCchan1", "UCbroken"]

    def side(request: httpx.Request):
        if request.url.params["channel_id"] == "UCbroken":
            return httpx.Response(404)
        return httpx.Response(200, content=feed(entry("NEW1", "2026-10-20T08:00:00+00:00")))

    with respx.mock() as router:
        router.get(RSS_URL).mock(side_effect=side)
        out = YouTubeSource(yt_settings, http=httpx.Client()).discover(NOW)
    assert [c.source_id for c in out] == ["NEW1"]

    with respx.mock() as router:
        router.get(RSS_URL).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceError, match="nicht gefunden"):
            YouTubeSource(yt_settings, http=httpx.Client()).discover(NOW)


def test_rss_retries_once_on_5xx(yt_settings):
    with respx.mock() as router:
        route = router.get(RSS_URL).mock(side_effect=[
            httpx.Response(500), httpx.Response(200, content=feed(entry("NEW1", "2026-10-20T08:00:00+00:00")))])
        src = YouTubeSource(yt_settings, http=httpx.Client())
        src._sleep = lambda s: None
        out = src.discover(NOW)
    assert route.call_count == 2 and [c.source_id for c in out] == ["NEW1"]


def test_rss_enrichment_with_api_key_drops_short_videos(yt_settings):
    s = with_key(yt_settings)
    body = feed(entry("LONG", "2026-10-20T08:00:00+00:00"), entry("TINY", "2026-10-20T07:00:00+00:00"),
                entry("LIVE", "2026-10-20T06:00:00+00:00"))
    with respx.mock(assert_all_called=False) as router:
        router.get(RSS_URL).mock(return_value=httpx.Response(200, content=body))
        videos = router.get(f"{API_BASE}/videos").mock(return_value=httpx.Response(200, json={"items": [
            {"id": "LONG", "contentDetails": {"duration": "PT12M30S"},
             "statistics": {"viewCount": "99000", "likeCount": "500", "commentCount": "40"}},
            {"id": "TINY", "contentDetails": {"duration": "PT45S"}, "statistics": {"viewCount": "10"}},
            {"id": "LIVE", "contentDetails": {"duration": "P0D"}, "statistics": {}},
        ]}))
        src = YouTubeSource(s, http=httpx.Client())
        out = src.discover(NOW)
        src.discover(NOW)  # Details gecacht -> kein zweiter videos.list-Aufruf
    assert [c.source_id for c in out] == ["LONG"]
    assert out[0].duration_s == 750.0 and out[0].view_count == 99000
    assert out[0].engagement["comments"] == 40.0
    params = videos.calls[0].request.url.params
    assert params["part"] == "contentDetails,statistics" and params["key"] == "APIKEY123"
    assert sorted(params["id"].split(",")) == ["LIVE", "LONG", "TINY"]
    assert videos.call_count == 1


# --------------------------------------------------------------------------- Suche


def search_payload():
    return {"items": [
        {"id": {"kind": "youtube#video", "videoId": "S1"},
         "snippet": {"publishedAt": "2026-10-19T18:00:00Z", "channelId": "UCother", "title": "Insane GTA 6 &#39;heist&#39;",
                     "channelTitle": "Other &amp; Guy", "liveBroadcastContent": "none",
                     "thumbnails": {"high": {"url": "https://i.ytimg.com/vi/S1/hq.jpg"}}}},
        {"id": {"kind": "youtube#video", "videoId": "S2"},
         "snippet": {"publishedAt": "2026-10-19T19:00:00Z", "channelId": "UCother", "title": "short",
                     "channelTitle": "Other", "liveBroadcastContent": "none"}},
        {"id": {"kind": "youtube#video", "videoId": "S3"},
         "snippet": {"publishedAt": "2026-10-19T19:00:00Z", "channelId": "UCother", "title": "4h stream",
                     "channelTitle": "Other", "liveBroadcastContent": "none"}},
        {"id": {"kind": "youtube#video", "videoId": "S4"},
         "snippet": {"publishedAt": "2026-10-20T11:00:00Z", "channelId": "UCother", "title": "live now",
                     "channelTitle": "Other", "liveBroadcastContent": "live"}},
    ]}


def videos_payload():
    return {"items": [
        {"id": "S1", "contentDetails": {"duration": "PT25M"}, "statistics": {"viewCount": "250000"}},
        {"id": "S2", "contentDetails": {"duration": "PT59S"}, "statistics": {"viewCount": "1000000"}},
        {"id": "S3", "contentDetails": {"duration": "PT5H1M"}, "statistics": {"viewCount": "3000"}},
    ]}


def test_search_with_duration_filter_and_interval(yt_settings):
    s = with_key(yt_settings)
    s.config.sources.youtube.channel_ids = []
    s.config.sources.youtube.search_queries = ["gta 6 funny moments"]
    with respx.mock(assert_all_called=False) as router:
        search = router.get(f"{API_BASE}/search").mock(return_value=httpx.Response(200, json=search_payload()))
        videos = router.get(f"{API_BASE}/videos").mock(return_value=httpx.Response(200, json=videos_payload()))
        src = YouTubeSource(s, http=httpx.Client())
        out = src.discover(NOW)
        out2 = YouTubeSource(s, http=httpx.Client()).discover(NOW)  # neue Instanz, Zustand auf Platte
    assert [c.source_id for c in out] == ["S1"]
    c = out[0]
    assert c.source == SourceKind.youtube_video and c.title == "Insane GTA 6 'heist'"
    assert c.creator_name == "Other & Guy" and c.creator_url == "https://www.youtube.com/channel/UCother"
    assert c.duration_s == 1500.0 and c.view_count == 250000
    assert c.thumbnail_url == "https://i.ytimg.com/vi/S1/hq.jpg"
    p = search.calls[0].request.url.params
    assert p["q"] == "gta 6 funny moments" and p["type"] == "video" and p["order"] == "viewCount"
    assert p["publishedAfter"] == "2026-10-18T12:00:00Z" and p["maxResults"] == "25"
    assert sorted(videos.calls[0].request.url.params["id"].split(",")) == ["S1", "S2", "S3"]
    assert out2 == [] and search.call_count == 1  # Quota-Schutz: Mindestabstand zwischen Suchen
    state = json.loads((s.config.paths.cache_dir / "youtube_search_state.json").read_text())
    assert state["last_search"] == "2026-10-20T12:00:00Z"


def test_search_quota_exceeded_pauses(yt_settings):
    s = with_key(yt_settings)
    s.config.sources.youtube.channel_ids = []
    s.config.sources.youtube.search_queries = ["gta 6"]
    err = {"error": {"code": 403, "message": "quota", "errors": [{"reason": "quotaExceeded"}]}}
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{API_BASE}/search").mock(return_value=httpx.Response(403, json=err))
        with pytest.raises(SourceError, match="Kontingent") as ei:
            YouTubeSource(s, http=httpx.Client()).discover(NOW)
    assert "APIKEY123" not in str(ei.value)
    state = json.loads((s.config.paths.cache_dir / "youtube_search_state.json").read_text())
    paused = datetime.fromisoformat(state["paused_until"].replace("Z", "+00:00"))
    assert paused > NOW
    assert YouTubeSource(s, http=httpx.Client())._search_due(NOW) is False


def test_invalid_key_message_hides_key(yt_settings):
    s = with_key(yt_settings)
    s.config.sources.youtube.channel_ids = []
    s.config.sources.youtube.search_queries = ["gta 6"]
    err = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                     "errors": [{"reason": "badRequest"}]}}
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{API_BASE}/search").mock(return_value=httpx.Response(400, json=err))
        with pytest.raises(SourceError, match="API-Key ungültig") as ei:
            YouTubeSource(s, http=httpx.Client()).discover(NOW)
    assert "APIKEY123" not in str(ei.value)


def test_parse_iso_duration():
    assert parse_iso_duration("PT1H2M3S") == 3723
    assert parse_iso_duration("PT45S") == 45
    assert parse_iso_duration("P0D") == 0
    assert parse_iso_duration("P1DT1H") == 90000
    assert parse_iso_duration("PT12M") == 720
    assert parse_iso_duration("") is None
    assert parse_iso_duration("garbage") is None
    assert parse_iso_duration(None) is None


# --------------------------------------------------------------------------- Untertitel

JSON3 = {
    "wireMagic": "pb3",
    "events": [
        {"tStartMs": 0, "dDurationMs": 200000, "id": 1, "wpWinPosId": 1, "wsWinStyleId": 1},
        {"tStartMs": 1000, "dDurationMs": 4000, "wWinId": 1, "segs": [
            {"utf8": "no", "acAsrConf": 0}, {"utf8": " way", "tOffsetMs": 400, "acAsrConf": 0},
            {"utf8": " bro", "tOffsetMs": 900, "acAsrConf": 0}]},
        {"tStartMs": 2900, "dDurationMs": 2100, "wWinId": 1, "aAppend": 1, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 3000, "dDurationMs": 3000, "wWinId": 1, "segs": [
            {"utf8": "look", "acAsrConf": 0}, {"utf8": " at", "tOffsetMs": 300}, {"utf8": " this", "tOffsetMs": 600}]},
    ],
}


def test_parse_json3_word_level():
    tr = parse_json3(JSON3)
    assert tr is not None and tr.language == "en"
    assert [s.text for s in tr.segments] == ["no way bro", "look at this"]
    s0, s1 = tr.segments
    assert s0.start == 1.0 and s0.end == 3.0  # endet, wenn die nächste Zeile beginnt
    assert [(w.text, w.start) for w in s0.words] == [("no", 1.0), ("way", 1.4), ("bro", 1.9)]
    assert s0.words[0].end == 1.4 and s0.words[-1].end == 3.0
    assert s1.end == 6.0 and s1.words[-1].start == 3.6
    assert all(w.end >= w.start for w in tr.words())
    assert tr.text == "no way bro look at this"


def test_parse_json3_without_offsets_and_empty():
    manual = {"events": [{"tStartMs": 500, "dDurationMs": 2000, "segs": [{"utf8": "Hello there\nfriend"}]}]}
    tr = parse_json3(manual)
    assert tr.segments[0].text == "Hello there friend" and tr.segments[0].words == []
    assert tr.segments[0].start == 0.5 and tr.segments[0].end == 2.5
    assert parse_json3({"events": []}) is None
    assert parse_json3({}) is None


def test_parse_vtt_dedupes_rolling_lines():
    vtt = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:03.000 align:start position:0%
no<00:00:01.400><c> way</c><00:00:01.900><c> bro</c>

00:00:03.000 --> 00:00:05.000 align:start position:0%
no way bro
look<00:00:03.300><c> at</c><00:00:03.600><c> this</c>
"""
    tr = parse_vtt(vtt)
    assert [s.text for s in tr.segments] == ["no way bro", "look at this"]
    assert tr.segments[1].start == 3.0 and tr.segments[1].end == 5.0
    assert parse_vtt("WEBVTT\n\n") is None


# --------------------------------------------------------------------------- yt-dlp-Kontext


class FakeYDL:
    info: dict = {}
    raise_exc: Exception | None = None
    opts_seen: dict = {}

    def __init__(self, opts):
        FakeYDL.opts_seen = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        assert download is False
        if FakeYDL.raise_exc:
            raise FakeYDL.raise_exc
        return FakeYDL.info


@pytest.fixture
def fake_ydl(monkeypatch):
    import yt_dlp

    FakeYDL.info, FakeYDL.raise_exc = {}, None
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    return FakeYDL


def base_info(**kw) -> dict:
    info = {
        "id": "VID1", "title": "GTA 6 stream highlights", "channel": "Chan", "uploader": "Chan U",
        "duration": 3600, "description": "desc", "view_count": 12345,
        "chapters": [{"start_time": 0.0, "end_time": 600.0, "title": "Intro"}],
        "heatmap": [{"start_time": 0.0, "end_time": 36.0, "value": 1.0},
                    {"start_time": 36.0, "end_time": 72.0, "value": 0.25}, {"bad": 1}],
        "automatic_captions": {
            "de": [{"ext": "json3", "url": "https://www.youtube.com/api/timedtext?lang=de&fmt=json3"}],
            "en": [{"ext": "vtt", "url": "https://www.youtube.com/api/timedtext?lang=en&fmt=vtt"},
                   {"ext": "json3", "url": "https://www.youtube.com/api/timedtext?lang=en&fmt=json3"}],
            "en-orig": [{"ext": "srv1", "url": "https://www.youtube.com/api/timedtext?lang=en&fmt=srv1"},
                        {"ext": "json3", "url": "https://www.youtube.com/api/timedtext?lang=en&kind=asr&fmt=json3"}],
        },
    }
    info.update(kw)
    return info


def test_fetch_video_context_full(settings, fake_ydl):
    fake_ydl.info = base_info()
    with respx.mock(assert_all_called=True) as router:
        cap = router.get("https://www.youtube.com/api/timedtext").mock(
            return_value=httpx.Response(200, json=JSON3))
        ctx = fetch_video_context("https://www.youtube.com/watch?v=VID1", settings)
    assert cap.calls[0].request.url.params["kind"] == "asr"  # en-orig bevorzugt
    assert fake_ydl.opts_seen["skip_download"] is True and fake_ydl.opts_seen["quiet"] is True
    assert ctx.video_id == "VID1" and ctx.title == "GTA 6 stream highlights" and ctx.channel == "Chan"
    assert ctx.duration == 3600.0 and ctx.view_count == 12345 and ctx.description == "desc"
    assert ctx.chapters == [{"start_time": 0.0, "end_time": 600.0, "title": "Intro"}]
    assert [(h.start, h.end, h.value) for h in ctx.heatmap] == [(0.0, 36.0, 1.0), (36.0, 72.0, 0.25)]
    assert ctx.transcript is not None and ctx.transcript.segments[0].text == "no way bro"
    assert len(ctx.transcript.words()) == 6


def test_fetch_video_context_without_captions(settings, fake_ydl):
    fake_ydl.info = base_info(automatic_captions={}, heatmap=None, chapters=None, channel=None)
    ctx = fetch_video_context("https://www.youtube.com/watch?v=VID1", settings)
    assert ctx.transcript is None and ctx.heatmap == [] and ctx.chapters == []
    assert ctx.channel == "Chan U"


def test_fetch_video_context_caption_download_fails(settings, fake_ydl):
    fake_ydl.info = base_info()
    with respx.mock() as router:
        router.get("https://www.youtube.com/api/timedtext").mock(return_value=httpx.Response(429))
        ctx = fetch_video_context("https://www.youtube.com/watch?v=VID1", settings)
    assert ctx.transcript is None and ctx.duration == 3600.0


def test_fetch_video_context_vtt_fallback(settings, fake_ydl):
    fake_ydl.info = base_info(automatic_captions={"en": [
        {"ext": "vtt", "url": "https://www.youtube.com/api/timedtext?lang=en&fmt=vtt"}]})
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.500\nhello chat\n"
    with respx.mock() as router:
        router.get("https://www.youtube.com/api/timedtext").mock(return_value=httpx.Response(200, text=vtt))
        ctx = fetch_video_context("https://www.youtube.com/watch?v=VID1", settings)
    assert ctx.transcript.text == "hello chat"


def test_fetch_video_context_errors(settings, fake_ydl):
    import yt_dlp

    fake_ydl.raise_exc = yt_dlp.utils.DownloadError("ERROR: [youtube] VID1: Video unavailable")
    with pytest.raises(SourceError, match="nicht abrufbar: \\[youtube\\] VID1: Video unavailable"):
        fetch_video_context("https://www.youtube.com/watch?v=VID1", settings)

    fake_ydl.raise_exc = None
    fake_ydl.info = base_info(duration=None, is_live=True)
    with pytest.raises(SourceError, match="live"):
        fetch_video_context("https://www.youtube.com/watch?v=VID1", settings)


def test_real_ytdlp_class_is_used_by_default():
    # Schutz gegen versehentlich kaputten Import-Pfad (kein Netz nötig)
    import yt_dlp

    assert hasattr(yt_dlp, "YoutubeDL")
    assert callable(yt.fetch_video_context)


# --------------------------------------------------------------------------- Momente


def parent() -> CandidateIn:
    return CandidateIn(
        source=SourceKind.youtube_video, source_id="VID1", url="https://www.youtube.com/watch?v=VID1",
        title="GTA 6 stream highlights", creator_name="Chan", creator_url="https://www.youtube.com/channel/UCchan1",
        platform_label="YouTube", created_at=datetime(2026, 10, 19, 18, 0, tzinfo=UTC), duration_s=3600.0,
        view_count=50000, extra={"channel_id": "UCchan1", "via": "rss"},
    )


def test_make_moment_candidate():
    p = parent()
    m = make_moment_candidate(p, 120.0, 151.5, 87, "funny fail")
    assert m.source == SourceKind.youtube_moment and m.source_id == "VID1" and m.url == p.url
    assert m.start_s == 120.0 and m.end_s == 151.5 and m.duration_s == 31.5
    assert m.parent_key == p.key == "youtube_video:VID1"
    assert m.key == "youtube_moment:VID1@120.0-151.5"
    assert m.title == p.title and m.creator_name == "Chan" and m.platform_label == "YouTube"
    assert m.extra["moment_score"] == 87 and m.extra["moment_why"] == "funny fail"
    assert m.extra["channel_id"] == "UCchan1"
    assert p.extra == {"channel_id": "UCchan1", "via": "rss"}  # Eltern-extra unverändert
    m2 = make_moment_candidate(p, 3590.0, 3700.0, 50, "end")
    assert m2.end_s == 3600.0  # auf Videolänge begrenzt
    with pytest.raises(ValueError):
        make_moment_candidate(p, 100.0, 90.0, 50, "bad")
