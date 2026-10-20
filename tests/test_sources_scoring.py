"""prescore: Monotonie, Quellgewichte, Boni, nie Exception; same_twitch_moment."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from wdvi.schemas import CandidateIn, SourceKind
from wdvi.sources.scoring import prescore, same_twitch_moment

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def cand(source: SourceKind = SourceKind.twitch_clip, *, views: int | None = 10000, hours: float = 2.0,
         sid: str = "A", **kw) -> CandidateIn:
    base = dict(source=source, source_id=sid, url="https://x", title="t", creator_name="c",
                platform_label="Twitch", created_at=NOW - timedelta(hours=hours), view_count=views)
    base.update(kw)
    return CandidateIn(**base)


def test_monotonic_in_views_and_age():
    scores = [prescore(cand(views=v), NOW) for v in (0, 10, 100, 1000, 10000, 100000)]
    assert scores == sorted(scores) and len(set(scores)) == len(scores)
    by_age = [prescore(cand(hours=h), NOW) for h in (1, 2, 6, 24, 48)]
    assert by_age == sorted(by_age, reverse=True)
    # sehr frische Clips: Mindestalter 0.5 h verhindert Explosion
    assert prescore(cand(hours=0.01), NOW) == prescore(cand(hours=0.5), NOW)
    assert prescore(cand(views=10000, hours=2), NOW) == pytest.approx(math.log10(1 + 5000), rel=1e-3)


def test_source_weights_and_bonuses():
    tw = prescore(cand(), NOW)
    kick = prescore(cand(SourceKind.kick_clip, platform_label="Kick"), NOW)
    ytv = prescore(cand(SourceKind.youtube_video, platform_label="YouTube"), NOW)
    assert tw > kick > ytv
    assert kick == pytest.approx(tw * 0.9, rel=1e-3) and ytv == pytest.approx(tw * 0.5, rel=1e-3)
    featured = prescore(cand(extra={"is_featured": True}), NOW)
    assert featured == pytest.approx(tw + 0.3, rel=1e-3)


def test_reddit_uses_score():
    low = prescore(cand(SourceKind.reddit_post, views=None, engagement={"score": 100}), NOW)
    high = prescore(cand(SourceKind.reddit_post, views=None, engagement={"score": 5000}), NOW)
    assert 0 < low < high
    assert high == pytest.approx(math.log10(1 + 5000 * 20 / 2), rel=1e-3)
    assert prescore(cand(SourceKind.reddit_post, views=None), NOW) == 0.0


def test_youtube_moment_uses_moment_score():
    def moment(score, views=50000):
        return cand(SourceKind.youtube_moment, views=views, platform_label="YouTube", start_s=10.0, end_s=40.0,
                    extra={"moment_score": score})

    s = [prescore(moment(x), NOW) for x in (0, 40, 80, 100, 150)]
    assert s[0] < s[1] < s[2] < s[3] == s[4]  # moment_score auf 100 begrenzt
    assert s[2] - s[1] == pytest.approx(40 / 25, rel=1e-3)
    assert prescore(moment(80, views=1_000_000), NOW) > prescore(moment(80, views=1000), NOW)
    # Moment mit hohem Score schlägt einen durchschnittlichen Twitch-Clip nicht um Größenordnungen
    assert 2.0 < prescore(moment(80), NOW) < 6.0


@pytest.mark.parametrize("kind", list(SourceKind))
def test_never_raises_and_is_finite(kind):
    weird = [
        cand(kind, views=None),
        cand(kind, views=0, hours=-5),  # created_at in der Zukunft
        cand(kind, views=-100, engagement={"score": float("nan")}, extra={"moment_score": "abc"}),
        cand(kind, views=10**15, engagement={"score": float("inf")}, extra={"moment_score": float("inf")}),
        cand(kind, created_at=datetime(2026, 10, 20, 10, 0)),  # naive
    ]
    for c in weird:
        v = prescore(c, NOW)
        assert isinstance(v, float) and math.isfinite(v) and v >= 0
    assert isinstance(prescore(cand(kind), datetime(2026, 10, 20, 12, 0)), float)  # naive now


def twc(sid, bid="1", vid="9", off=100, dur=30.0, source=SourceKind.twitch_clip):
    return cand(source, sid=sid, duration_s=dur, extra={"broadcaster_id": bid, "video_id": vid, "vod_offset": off})


def test_same_twitch_moment():
    assert same_twitch_moment(twc("A", off=100), twc("B", off=115))
    assert not same_twitch_moment(twc("A", off=100), twc("B", off=200))
    assert not same_twitch_moment(twc("A"), twc("B", bid="2"))
    assert not same_twitch_moment(twc("A"), twc("B", vid="8"))
    assert not same_twitch_moment(twc("A", vid=""), twc("B", vid=""))
    assert not same_twitch_moment(twc("A", off=None), twc("B", off=None))
    assert same_twitch_moment(twc("A", off=100), twc("B", off=125), tolerance_s=30)
    # langer Clip enthält kurzen Clip (Offsets 40 s auseinander, aber volle Überlappung)
    assert same_twitch_moment(twc("A", off=100, dur=60.0), twc("B", off=140, dur=15.0))
    assert not same_twitch_moment(twc("A", off=100, dur=10.0), twc("B", off=140, dur=10.0))
    assert same_twitch_moment(twc("A"), twc("A", off=None))  # derselbe Clip
    assert not same_twitch_moment(twc("A"), twc("B", source=SourceKind.kick_clip))
