"""Pipeline-Filter: Creator-Regeln, Vorfilter, Sprache, Musik, Dedup, Claude-Ablehnungen, Fehler."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlmodel import select

from tests.test_pipeline_support import (
    NOW,
    FakeRateLimitedLLM,
    FakeSource,
    MediaFakes,
    make_analysis,
    make_cand,
    make_pipeline,
    make_plan,
)
from wdvi import db as dbmod
from wdvi.brain import claude as brain_claude
from wdvi.media.download import DownloadError
from wdvi.models import Candidate, Clip, CreatorRule, JobRun
from wdvi.pipeline import STALE_PROCESSING, safe_name
from wdvi.schemas import SourceKind


def _cand(key: str) -> Candidate:
    with dbmod.session_scope() as s:
        return s.exec(select(Candidate).where(Candidate.key == key)).one()


def _rule(platform: str, handle: str, rule: str) -> None:
    with dbmod.session_scope() as s:
        s.add(CreatorRule(platform_label=platform, handle=handle, rule=rule))


def _setup(settings, monkeypatch, cands, **kw):
    fakes = MediaFakes(monkeypatch)
    p = make_pipeline(settings, sources=[FakeSource("twitch", cands)], **kw)
    p.discover(now=NOW)
    return p, fakes


# --------------------------------------------------------------------------- Creator-Regeln


def test_blocked_creator_is_skipped_on_discover(settings, db, monkeypatch):
    _rule("Twitch", "xqc", "block")
    p, fakes = _setup(settings, monkeypatch, [make_cand("ClipA")])
    row = _cand("twitch_clip:ClipA")
    assert row.status == "skipped" and "Block-Liste" in row.status_reason
    assert p.process_cycle(now=NOW) == 0 and fakes.downloads == []


def test_block_matches_display_name_and_reddit_linked_creator(settings, db, monkeypatch):
    _rule("twitch", "@SomeStreamer", "block")
    reddit = make_cand(
        "abc",
        source=SourceKind.reddit_post,
        platform="Reddit",
        creator="u/poster",
        handle="poster",
        extra={
            "linked_platform": "Twitch",
            "linked_url": "https://www.twitch.tv/somestreamer/clip/FunnyClip",
        },
    )
    by_name = make_cand("ClipB", creator="SomeStreamer", handle=None)
    p, _ = _setup(settings, monkeypatch, [reddit, by_name])
    assert _cand(reddit.key).status == "skipped"
    assert _cand(by_name.key).status == "skipped"


def test_rule_added_later_skips_in_cycle(settings, db, monkeypatch):
    p, fakes = _setup(settings, monkeypatch, [make_cand("ClipA")])
    _rule("Twitch", "xqc", "block")
    assert p.process_cycle(now=NOW) == 0
    assert _cand("twitch_clip:ClipA").status == "skipped" and fakes.downloads == []


def test_boost_and_allow_raise_prescore(settings, db, monkeypatch):
    _rule("Twitch", "boosted", "boost")
    _rule("Twitch", "allowed", "allow")
    cands = [
        make_cand("A", creator="Plain", handle="plain"),
        make_cand("B", creator="Boosted", handle="boosted"),
        make_cand("C", creator="Allowed", handle="allowed"),
    ]
    _setup(settings, monkeypatch, cands)
    a, b, c = (_cand(f"twitch_clip:{x}").prescore for x in "ABC")
    assert b > c > a


# --------------------------------------------------------------------------- billige Vorfilter


def test_cheap_filters_skip_before_download(settings, db, monkeypatch):
    cands = [
        make_cand("Old", age_h=72),
        make_cand("Long", duration=400.0),
        make_cand("Short", duration=2.0),
        make_cand("German", language="de"),
        make_cand("Good"),
    ]
    p, fakes = _setup(settings, monkeypatch, cands)
    assert p.process_cycle(now=NOW) == 1
    assert fakes.downloads == ["twitch_clip:Good"]
    reasons = {k: _cand(f"twitch_clip:{k}").status_reason for k in ("Old", "Long", "Short", "German")}
    assert all(_cand(f"twitch_clip:{k}").status == "skipped" for k in reasons)
    assert "alt" in reasons["Old"] and "lang" in reasons["Long"] and "kurz" in reasons["Short"]
    assert "de" in reasons["German"]


def test_candidates_per_cycle_and_rate_limit_budget(settings, db, monkeypatch):
    settings.config.filters.candidates_per_cycle = 2
    cands = [make_cand(f"C{i}", views=1000 * (i + 1)) for i in range(4)]
    p, fakes = _setup(settings, monkeypatch, cands)
    assert p.process_cycle(now=NOW) == 2
    assert fakes.plans == ["twitch_clip:C3", "twitch_clip:C2"]

    p2 = make_pipeline(settings, llm=FakeRateLimitedLLM(remaining=1))
    assert p2.process_cycle(now=NOW) == 1
    p3 = make_pipeline(settings, llm=FakeRateLimitedLLM(remaining=0))
    assert p3.process_cycle(now=NOW) == 0
    assert _cand("twitch_clip:C0").status == "new"


def test_llm_not_configured_keeps_queue(settings, db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings.secrets.anthropic_api_key = None
    MediaFakes(monkeypatch)
    p = make_pipeline(settings, llm=None, sources=[FakeSource("twitch", [make_cand("ClipA")])])
    p.discover(now=NOW)
    assert p.process_cycle(now=NOW) == 0
    assert _cand("twitch_clip:ClipA").status == "new"
    with dbmod.session_scope() as s:
        run = s.exec(select(JobRun).where(JobRun.job == "process")).one()
    assert run.ok is False and "ANTHROPIC_API_KEY" in run.message


# --------------------------------------------------------------------------- Media-Filter


def test_language_filter(settings, db, monkeypatch):
    cands = [make_cand("Spanish"), make_cand("Quiet"), make_cand("Unsure")]
    p, fakes = _setup(settings, monkeypatch, cands)
    fakes.analysis_by_key["twitch_clip:Spanish"] = make_analysis(
        "x", language="es", speech_ratio=0.6, phashes=["0000000000000001"]
    )
    fakes.analysis_by_key["twitch_clip:Quiet"] = make_analysis(
        "x", language="es", speech_ratio=0.1, phashes=["ffffffffffffffff"]
    )
    fakes.analysis_by_key["twitch_clip:Unsure"] = make_analysis(
        "x", language="cy", speech_ratio=0.5, language_prob=0.3, phashes=["00ff00ff00ff00ff"]
    )
    assert p.process_cycle(now=NOW) == 2
    sp = _cand("twitch_clip:Spanish")
    assert sp.status == "rejected" and "'es'" in sp.status_reason
    assert _cand("twitch_clip:Quiet").status == "rendered"  # kaum Sprache -> Sprache egal
    assert _cand("twitch_clip:Unsure").status == "rendered"  # unsichere Erkennung -> kein Ablehnungsgrund
    assert "twitch_clip:Spanish" not in fakes.plans


def test_music_filter(settings, db, monkeypatch):
    p, fakes = _setup(settings, monkeypatch, [make_cand("Song"), make_cand("Unknown")])
    fakes.analysis_by_key["twitch_clip:Song"] = make_analysis(
        "x", music_prob=0.8, phashes=["0000000000000001"]
    )
    fakes.analysis_by_key["twitch_clip:Unknown"] = make_analysis(
        "x", music_prob=None, phashes=["ffffffffffffffff"]
    )
    assert p.process_cycle(now=NOW) == 1
    song = _cand("twitch_clip:Song")
    assert song.status == "rejected" and "Musik" in song.status_reason
    assert _cand("twitch_clip:Unknown").status == "rendered"  # nicht gemessen -> nicht ablehnen

    settings.config.filters.music_policy = "allow"
    with dbmod.session_scope() as s:
        row = s.exec(select(Candidate).where(Candidate.key == "twitch_clip:Song")).one()
        row.status = "new"
    assert p.process_cycle(now=NOW) == 1


def test_dedup_against_recent_clips(settings, db, monkeypatch):
    first = make_cand("First", views=9000)
    repost = make_cand(
        "rp1",
        source=SourceKind.reddit_post,
        platform="Reddit",
        creator="u/x",
        handle="x",
        views=None,
        engagement={"score": 5000},
    )
    p, fakes = _setup(settings, monkeypatch, [first, repost])
    fakes.analysis_by_key[first.key] = make_analysis("x", phashes=["ffd8c0a0b0e0f000", "0f0f0f0f0f0f0f0f"])
    fakes.analysis_by_key[repost.key] = make_analysis("x", phashes=["ffd8c0a0b0e0f001", "0f0f0f0f0f0f0f0e"])
    settings.config.filters.candidates_per_cycle = 1
    assert p.process_cycle(now=NOW) == 1
    assert p.process_cycle(now=NOW) == 0
    rows = {c.key: _cand(c.key) for c in (first, repost)}
    assert sorted(r.status for r in rows.values()) == ["rejected", "rendered"]
    dup = next(r for r in rows.values() if r.status == "rejected")
    assert "Duplikat" in dup.status_reason
    # manuelles Neu-Verarbeiten desselben Kandidaten ist kein Duplikat von sich selbst
    done = next(c for c in (first, repost) if rows[c.key].status == "rendered")
    cid = p.add_candidate(done, force=True)  # Clip liegt noch im Review -> nur mit --force
    assert p.process_candidate(cid) is not None


# --------------------------------------------------------------------------- Claude-Entscheidungen


def test_plan_rejections(settings, db, monkeypatch):
    cands = [make_cand("Leak"), make_cand("Meh"), make_cand("Low"), make_cand("Unsafe")]
    p, fakes = _setup(settings, monkeypatch, cands)
    fakes.plan_by_key["twitch_clip:Leak"] = make_plan(is_leak_or_unreleased=True)  # keep=True, aber Leak
    fakes.plan_by_key["twitch_clip:Meh"] = make_plan(keep=False, reject_reason="nothing happens")
    fakes.plan_by_key["twitch_clip:Low"] = make_plan(virality_score=40)
    fakes.plan_by_key["twitch_clip:Unsafe"] = make_plan(is_unsafe=True)
    assert p.process_cycle(now=NOW) == 0
    assert "Leak" in _cand("twitch_clip:Leak").status_reason
    meh = _cand("twitch_clip:Meh")
    assert meh.status == "rejected" and "nothing happens" in meh.status_reason
    assert meh.extra["llm"]["score"] == 88
    assert "40" in _cand("twitch_clip:Low").status_reason
    assert _cand("twitch_clip:Unsafe").status == "rejected"
    assert fakes.renders == []

    settings.config.filters.leak_policy = "allow"
    cid = p.add_candidate(make_cand("Leak"))
    assert p.process_candidate(cid) is not None


def test_refusal_and_rate_limit(settings, db, monkeypatch):
    cands = [
        make_cand("Refused", views=90000),
        make_cand("Limited", views=50000),
        make_cand("Later", views=1000),
    ]
    p, fakes = _setup(settings, monkeypatch, cands)
    limited = brain_claude.LLMError("Anthropic-Rate-Limit (429)")
    limited.retryable = True
    fakes.plan_by_key["twitch_clip:Refused"] = brain_claude.LLMRefusal("refused")
    fakes.plan_by_key["twitch_clip:Limited"] = limited
    assert p.process_cycle(now=NOW) == 0
    assert _cand("twitch_clip:Refused").status == "rejected"
    lim = _cand("twitch_clip:Limited")
    assert lim.status == "new" and "später" in lim.status_reason
    assert _cand("twitch_clip:Later").status == "new"  # Zyklus abgebrochen, zurück in die Warteschlange
    assert "twitch_clip:Later" not in fakes.downloads
    # Claude pausiert: nächster Zyklus macht nichts
    n_dl = len(fakes.downloads)
    assert p.process_cycle(now=NOW) == 0 and len(fakes.downloads) == n_dl
    # nach der Pause geht es weiter; die Analyse liegt im Cache (kein zweites Whisper)
    p._llm_pause_until = None
    del fakes.plan_by_key["twitch_clip:Limited"]
    analyses_before = fakes.analyses.count(safe_name("twitch_clip:Limited"))
    assert p.process_cycle(now=NOW) == 2
    assert fakes.analyses.count(safe_name("twitch_clip:Limited")) == analyses_before


def test_non_retryable_llm_error_fails(settings, db, monkeypatch):
    p, fakes = _setup(settings, monkeypatch, [make_cand("Bad")])
    fakes.plan_by_key["twitch_clip:Bad"] = brain_claude.LLMError("Anthropic-API-Key ungültig (401)")
    assert p.process_cycle(now=NOW) == 0
    row = _cand("twitch_clip:Bad")
    assert row.status == "failed" and "401" in row.status_reason


# --------------------------------------------------------------------------- Fehler


def test_errors_mark_failed_and_cycle_continues(settings, db, monkeypatch):
    cands = [make_cand("Broken", views=90000), make_cand("Crash", views=50000), make_cand("Fine", views=1000)]
    p, fakes = _setup(settings, monkeypatch, cands)
    fakes.download_errors["twitch_clip:Broken"] = DownloadError("Clip wurde gelöscht")
    fakes.plan_by_key["twitch_clip:Crash"] = KeyError("boom")
    assert p.process_cycle(now=NOW) == 1
    broken = _cand("twitch_clip:Broken")
    assert broken.status == "failed" and broken.status_reason.startswith("Download")
    crash = _cand("twitch_clip:Crash")
    assert crash.status == "failed" and "KeyError" in crash.status_reason
    assert _cand("twitch_clip:Fine").status == "rendered"


def test_render_error_marks_failed(settings, db, monkeypatch):
    from wdvi.render import renderer

    p, _ = _setup(settings, monkeypatch, [make_cand("ClipA")])

    def boom(*a, **kw):
        raise renderer.RenderError("ffmpeg: Invalid data")

    monkeypatch.setattr(renderer, "render_clip", boom)
    assert p.process_cycle(now=NOW) == 0
    row = _cand("twitch_clip:ClipA")
    assert row.status == "failed" and row.status_reason.startswith("Rendern")
    with dbmod.session_scope() as s:
        assert s.exec(select(Clip)).all() == []


def test_stale_processing_is_recovered(settings, db, monkeypatch):
    p, fakes = _setup(settings, monkeypatch, [make_cand("Stuck")])
    from wdvi.pipeline import utcnow

    with dbmod.session_scope() as s:
        row = s.exec(select(Candidate)).one()
        row.status = "processing"
        row.updated_at = utcnow() - STALE_PROCESSING - timedelta(minutes=5)
    assert p.process_cycle(now=utcnow()) == 1
    assert _cand("twitch_clip:Stuck").extra["process_resets"] == 1


def test_fresh_processing_is_not_touched(settings, db, monkeypatch):
    p, fakes = _setup(settings, monkeypatch, [make_cand("Busy")])
    from wdvi.pipeline import utcnow

    with dbmod.session_scope() as s:
        row = s.exec(select(Candidate)).one()
        row.status = "processing"
        row.updated_at = utcnow()  # "frisch" nach Wanduhr (Stale-Erkennung nutzt die echte Zeit, nicht NOW)
    assert p.process_cycle(now=NOW) == 0  # könnte gerade in einem anderen Prozess laufen
    assert _cand("twitch_clip:Busy").status == "processing"


@pytest.mark.parametrize("dur,expected", [(60.0, "skipped"), (3000.0, "mined")])
def test_youtube_video_duration_check_in_mining(settings, db, monkeypatch, dur, expected):
    from wdvi.brain import editor
    from wdvi.sources import youtube as yt_source

    MediaFakes(monkeypatch)
    monkeypatch.setattr(
        yt_source,
        "fetch_video_context",
        lambda url, s: yt_source.VideoContext(video_id="v", title="t", channel="c", duration=dur),
    )
    monkeypatch.setattr(editor, "mine_moments", lambda *a, **kw: [])
    video = make_cand("v", source=SourceKind.youtube_video, platform="YouTube", duration=None)
    p = make_pipeline(settings, sources=[FakeSource("youtube", [video])])
    p.discover(now=NOW)
    p.process_cycle(now=NOW)
    assert _cand(video.key).status == expected


def test_failure_streak_pauses_processing(settings, db, monkeypatch):
    settings.config.filters.candidates_per_cycle = 6
    cands = [make_cand(f"C{i}", views=1000 * (10 - i)) for i in range(6)]
    p, fakes = _setup(settings, monkeypatch, cands)
    for c in cands:
        fakes.download_errors[c.key] = DownloadError("Clip/Video ist nicht (mehr) verfügbar.")
    assert p.process_cycle(now=NOW) == 0
    assert len(fakes.downloads) == 3  # nach 3 Fehlschlägen in Folge Schluss
    # vermutlich systemisch: die drei kommen einmalig nach der Pause erneut dran
    first = [_cand(c.key) for c in cands[:3]]
    assert all(r.status == "new" and r.extra["streak_reverts"] == 1 and r.extra["next_try_at"] for r in first)
    assert all(_cand(c.key).status == "new" for c in cands[3:])
    with dbmod.session_scope() as s:
        run = s.exec(select(JobRun).where(JobRun.job == "process")).one()
    assert (
        run.ok is False and "Fehlschläge in Folge" in run.message and "nicht (mehr) verfügbar" in run.message
    )
    assert "Pause für 10 min" in run.message
    # während der Pause passiert nichts
    assert p.process_cycle(now=NOW) == 0 and len(fakes.downloads) == 3
    # nach der Pause geht es mit den übrigen weiter (die drei warten noch auf ihren Termin)
    p._fail_pause_until = None
    fakes.download_errors.clear()
    assert p.process_cycle(now=NOW) == 3
    assert sorted(fakes.downloads[3:]) == sorted(c.key for c in cands[3:])


def test_failure_streak_pause_grows_and_reverts_only_once(settings, db, monkeypatch):
    from wdvi.pipeline import FAIL_PAUSE

    settings.config.filters.candidates_per_cycle = 3
    cands = [make_cand(f"C{i}", views=1000 * (10 - i)) for i in range(3)]
    p, fakes = _setup(settings, monkeypatch, cands)
    for c in cands:
        fakes.download_errors[c.key] = DownloadError("Clip/Video ist nicht (mehr) verfügbar.")
    p.process_cycle(now=NOW)
    first_pause = p._fail_pause_until
    # Pause und Termine "abgelaufen": dieselben drei scheitern erneut -> jetzt endgültig 'failed'
    p._fail_pause_until = None
    with dbmod.session_scope() as s:
        for row in s.exec(select(Candidate)).all():
            row.extra = {**row.extra, "next_try_at": None}
    p.process_cycle(now=NOW)
    assert all(_cand(c.key).status == "failed" for c in cands)
    assert p._fail_pause_until - first_pause > FAIL_PAUSE  # zweite Pause doppelt so lang
