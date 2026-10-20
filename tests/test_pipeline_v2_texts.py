"""Edit-Engine v2 in der Pipeline: Transkript der behaltenen Stellen in der YouTube-Beschreibung,
debate_score im Clip, Plan-Tags als YouTube-Tags, #GTA6 nur bei GTA-Inhalten."""

from __future__ import annotations

from datetime import timedelta

from sqlmodel import select

from tests.test_pipeline_support import (
    NOW,
    FakePublisher,
    FakeSource,
    MediaFakes,
    make_analysis,
    make_cand,
    make_pipeline,
    make_plan,
)
from wdvi import db as dbmod
from wdvi.models import Clip, Post
from wdvi.pipeline import _post_hashtags
from wdvi.publish import text
from wdvi.publish.youtube import youtube_tags
from wdvi.schemas import PlatformText, Segment, Transcript, Word

WORDS = [
    ("Is", 0.2, 0.4), ("PlayStation", 0.4, 0.9), ("better", 0.9, 1.2),
    ("uhh", 2.2, 2.8),  # wird per remove_ranges herausgeschnitten
    ("than", 3.1, 3.3), ("Xbox?", 3.3, 3.8),
    ("bye", 12.0, 12.4),  # hinter trim_end
]


def _analysis(path="x"):
    a = make_analysis(path)
    words = [Word(text=t, start=s, end=e) for t, s, e in WORDS]
    tr = Transcript(language="en", segments=[Segment(text=" ".join(w[0] for w in WORDS), start=0.2, end=12.4,
                                                     words=words)])
    return a.model_copy(update={"transcript": tr})


def _plan(**kw):
    base = dict(
        trim_start=0.0, trim_end=10.0, remove_ranges=[(2.0, 3.0)], debate_score=91,
        tags=["gta 6", "ishowspeed", "ps5 vs xbox"], hashtags=["GTA6", "gaming", "ps5"],
        titles=PlatformText(youtube="Is PS5 better than Xbox? 🤔 GTA 6", tiktok="PS5 or Xbox? 🤔",
                            instagram="PS5 or Xbox? 🤔"),
    )
    base.update(kw)
    return make_plan(**base)


def _run(settings, monkeypatch, plan, publishers=None):
    fakes = MediaFakes(monkeypatch)
    fakes.analysis_by_key["twitch_clip:ClipA"] = _analysis()
    fakes.plan_by_key["twitch_clip:ClipA"] = plan
    p = make_pipeline(settings, sources=[FakeSource("twitch", [make_cand("ClipA")])], publishers=publishers or {})
    p.discover(now=NOW)
    assert p.process_cycle(now=NOW) == 1
    with dbmod.session_scope() as s:
        clip = s.exec(select(Clip)).one()
    return p, clip


def test_clip_texts_get_kept_transcript_and_debate_score(settings, db, monkeypatch):
    _, clip = _run(settings, monkeypatch, _plan())
    spoken = "Is PlayStation better than Xbox?"
    assert clip.analysis["transcript_kept"] == spoken
    assert clip.analysis["debate_score"] == 91 and clip.plan["debate_score"] == 91
    assert clip.plan["tags"] == ["gta 6", "ishowspeed", "ps5 vs xbox"]
    yt = clip.descriptions["youtube"]
    assert f"{text.FAN_DISCLAIMER}\n\n{text.TRANSCRIPT_LABEL}\n{spoken}\n\n#GTA6 #gaming #ps5" in yt
    assert "uhh" not in yt and "bye" not in yt
    for p in ("tiktok", "instagram"):
        if p in clip.descriptions:
            assert text.TRANSCRIPT_LABEL not in clip.descriptions[p]
            assert clip.descriptions[p].endswith("#GTA6 #gaming #ps5")


def test_non_gta_clip_gets_no_gta6(settings, db, monkeypatch):
    plan = _plan(
        hashtags=["minecraft", "speedrun", "gaming"], tags=["minecraft"],
        titles=PlatformText(youtube="Fastest run ever? 😱", tiktok="fast run", instagram="fast run"),
    )
    _, clip = _run(settings, monkeypatch, plan)
    for d in clip.descriptions.values():
        assert "#GTA6" not in d and d.endswith("#minecraft #speedrun #gaming")


def test_approve_edit_keeps_or_restores_transcript(settings, db, monkeypatch):
    p, clip = _run(settings, monkeypatch, _plan())
    spoken = clip.analysis["transcript_kept"]
    original = clip.descriptions["youtube"]

    # 1) Text oben geändert, Transkript im Anhang korrigiert -> Korrektur bleibt
    edited = original.replace("What a jump", "new body").replace("PlayStation", "PlayStation 5")
    p.approve_clip(clip.id, ["youtube"], descriptions={"youtube": edited})
    with dbmod.session_scope() as s:
        d = s.get(Clip, clip.id).descriptions["youtube"]
    assert d.startswith("new body\n\n") and text.transcript_in(d) == "Is PlayStation 5 better than Xbox?"
    assert d.count(text.FAN_DISCLAIMER) == 1 and d.endswith("#GTA6 #gaming #ps5")

    # 2) ganzer Anhang gelöscht -> Credit, Hinweis, gespeichertes Transkript und Hashtags kommen wieder
    with dbmod.session_scope() as s:
        s.get(Clip, clip.id).status = "pending_review"
    p.approve_clip(clip.id, ["youtube"], descriptions={"youtube": "only my text"})
    with dbmod.session_scope() as s:
        d = s.get(Clip, clip.id).descriptions["youtube"]
    assert d.startswith("only my text\n\n") and text.transcript_in(d) == spoken and "#GTA6" in d

    # 3) nur der Transkript-Block gelöscht -> bleibt weg
    with dbmod.session_scope() as s:
        s.get(Clip, clip.id).status = "pending_review"
    no_tr = d.replace(f"\n\n{text.TRANSCRIPT_LABEL}\n{spoken}", "")
    p.approve_clip(clip.id, ["youtube"], descriptions={"youtube": no_tr + " "})
    with dbmod.session_scope() as s:
        d = s.get(Clip, clip.id).descriptions["youtube"]
    assert text.TRANSCRIPT_LABEL not in d and d.endswith("#GTA6 #gaming #ps5")


def test_post_hashtags_youtube_gets_plan_tags():
    plan = {"hashtags": ["GTA6", "gaming"], "tags": ["gta 6", "ishowspeed"]}
    assert _post_hashtags("youtube", plan) == ["gta 6", "ishowspeed", "GTA6", "gaming"]
    assert _post_hashtags("tiktok", plan) == ["GTA6", "gaming"]
    assert _post_hashtags("instagram", None) == []
    assert _post_hashtags("youtube", {"hashtags": ["GTA6"]}) == ["GTA6"]  # alte Pläne ohne tags


def test_youtube_upload_request_carries_plan_tags(settings, db, monkeypatch):
    settings.config.publish.tiktok.enabled = False
    settings.config.publish.instagram.enabled = False
    settings.config.publish.youtube.native_schedule = False
    yt = FakePublisher("youtube")
    p, clip = _run(settings, monkeypatch, _plan(), publishers={"youtube": yt})
    p.approve_clip(clip.id, ["youtube"])
    assert p.schedule_approved(now=NOW) == 1
    with dbmod.session_scope() as s:
        slot = s.exec(select(Post)).one().scheduled_at
    assert p.publish_due(now=slot.replace(tzinfo=NOW.tzinfo) + timedelta(minutes=1)) == 1
    req = yt.requests[0]
    assert req.hashtags == ["gta 6", "ishowspeed", "ps5 vs xbox", "GTA6", "gaming", "ps5"]
    assert text.TRANSCRIPT_LABEL in req.description
    assert youtube_tags(req.hashtags, gta=True) == ["GTA6", "ishowspeed", "ps5 vs xbox", "gaming", "ps5"]


def test_dashboard_save_texts_keeps_transcript_and_gta_rule(settings, db, monkeypatch):
    """„Nur Texte speichern“ im Dashboard: gleiche Regeln wie die Freigabe (Transkript bleibt bzw. kommt
    zurück, #GTA6 nur bei GTA-Inhalten)."""
    from wdvi.dashboard import _data

    p, clip = _run(settings, monkeypatch, _plan())
    spoken = clip.analysis["transcript_kept"]
    original = clip.descriptions["youtube"]

    # Text oben geändert, Anhang (mit Transkript) unverändert -> Transkript bleibt
    assert _data.save_clip_texts(clip.id, {}, {"youtube": original.replace("What a jump", "new body")},
                                 settings) is None
    with dbmod.session_scope() as s:
        d = s.get(Clip, clip.id).descriptions["youtube"]
    assert d.startswith("new body\n\n") and text.transcript_in(d) == spoken and d.endswith("#GTA6 #gaming #ps5")

    # ganzer Anhang gelöscht -> gespeichertes Transkript kommt zurück
    assert _data.save_clip_texts(clip.id, {}, {"youtube": "only my text"}, settings) is None
    with dbmod.session_scope() as s:
        d = s.get(Clip, clip.id).descriptions["youtube"]
    assert d.startswith("only my text\n\n") and text.transcript_in(d) == spoken and d.count(text.FAN_DISCLAIMER) == 1


def test_dashboard_save_texts_no_gta6_for_non_gta_clip(settings, db, monkeypatch):
    plan = _plan(
        hashtags=["minecraft"], tags=["minecraft", "speedrun"],
        titles=PlatformText(youtube="Fastest run ever? 😱", tiktok="fast run", instagram="fast run"),
    )
    from wdvi.dashboard import _data

    p, clip = _run(settings, monkeypatch, plan)
    assert _data.save_clip_texts(clip.id, {}, {"youtube": "only my text"}, settings) is None
    with dbmod.session_scope() as s:
        d = s.get(Clip, clip.id).descriptions["youtube"]
    assert "#GTA6" not in d and "#minecraft" in d
