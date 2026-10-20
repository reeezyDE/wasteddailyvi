"""Dashboard rund ums Posten: TikTok-Inbox-Caption, Hinweise nach erfolgreichen Uploads, fehlgeschlagene
Posts neu einplanen, freigegebene Clips ohne Slot, Abbrechen von Posts, Texte speichern (Credit bleibt)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from markupsafe import escape

from tests.test_dashboard_support import (  # noqa: F401  (Fixture)
    PASSWORD,
    FakePipeline,
    FakePublisher,
    add_candidate,
    add_clip,
    add_post,
    dash_fixture,
    get,
    make_client,
)
from wdvi.dashboard import _data
from wdvi.dashboard.app import NO_PIPELINE, create_app
from wdvi.models import Clip, Post
from wdvi.publish import text

CREDIT = "🎥 Clip: xQc on Twitch – twitch.tv/xqc"
TT_DESC = f"Bro really did that\n\n{CREDIT}\n{text.FAN_DISCLAIMER}\n\n#GTA6 #GTAVI"


def _flash(client, resp) -> str:
    assert resp.status_code == 303, resp.text
    page = client.get(resp.headers["location"]).text
    m = re.search(r'<div class="flash flash-(ok|error)"[^>]*>.*?<span>(.*?)</span>', page, re.S)
    assert m, "keine Flash-Meldung gefunden"
    return f"{m.group(1)}: {m.group(2)}"


# --------------------------------------------------------------------------- TikTok-Inbox: Caption kopieren


def test_posted_tiktok_draft_offers_full_caption_with_credit(dash):
    clip_id = add_clip(None)
    pid = add_post(clip_id, platform="tiktok", status="draft_sent", posted_at=datetime.now(UTC),
                   title="Wild Vice City jump", description=TT_DESC, remote_id="v_inbox_1")
    yt = add_post(clip_id, platform="youtube", status="posted", posted_at=datetime.now(UTC),
                  title="YT title #Shorts", description="yt desc")

    html = dash.client.get("/posted").text
    caption = text.build_caption("tiktok", "Wild Vice City jump", TT_DESC)
    assert caption.startswith("Wild Vice City jump\n\nBro really did that")
    assert f'<pre class="caption" id="caption-{pid}">{escape(caption)}</pre>' in html
    assert f'data-copy-target="caption-{pid}"' in html
    assert str(escape(CREDIT)) in html and "Fan channel" in html
    assert "OHNE Text" in html
    assert f'id="caption-{yt}"' not in html  # YouTube braucht das nicht


def test_scheduled_tiktok_shows_caption_only_in_inbox_mode(dash):
    clip_id = add_clip(None)
    pid = add_post(clip_id, platform="tiktok", title="Wild jump", description=TT_DESC)
    yt = add_post(clip_id, platform="youtube", title="YT", description="d")
    html = dash.client.get("/scheduled").text
    assert f'data-copy-target="caption-{pid}"' in html and "Inbox-Modus" in html
    assert f'id="caption-{yt}"' not in html

    dash.settings.config.publish.tiktok.mode = "direct"  # Direktpost: Caption geht automatisch mit
    html = dash.client.get("/scheduled").text
    assert f'data-copy-target="caption-{pid}"' not in html


def test_review_explains_inbox_mode_for_tiktok_text(dash):
    add_clip(dash.clips_dir)
    html = dash.client.get("/").text
    assert "TikTok-Inbox-Modus" in html
    assert "Credit-Zeile und Fan-Hinweis am Ende setzt WastedDailyVI beim Speichern und Freigeben" in html
    assert "beim Posten automatisch an" not in html  # alter, irreführender Hinweis
    dash.settings.config.publish.tiktok.mode = "direct"
    assert "TikTok-Inbox-Modus" not in dash.client.get("/").text


def test_caption_for_handles_missing_texts():
    assert _data.caption_for(Post(clip_id=1, platform="youtube", scheduled_at=datetime.now(UTC),
                                  title="t", description="d")) == ""
    p = Post(clip_id=1, platform="tiktok", scheduled_at=datetime.now(UTC), title="", description="")
    assert _data.caption_for(p) == ""


# --------------------------------------------------------------------------- Hinweise bei erfolgreichen Uploads


def test_posted_shows_notes_of_successful_uploads(dash):
    clip_id = add_clip(None)
    now = datetime.now(UTC)
    add_post(clip_id, platform="tiktok", status="posted", posted_at=now, title="Privat gepostet",
             error="Hinweis: als SELF_ONLY (nur für dich sichtbar) gepostet. Öffentliche Direktposts …")
    add_post(clip_id, platform="youtube", status="posted", posted_at=now, title="YT privat",
             error="Hinweis: Uploads aus nicht geprüften Google-API-Projekten werden von YouTube auf 'privat' "
                   "gesperrt …")
    add_post(clip_id, platform="tiktok", status="draft_sent", posted_at=now, title="Timeout",
             error="Hinweis: TikTok-Status nach 300 s noch 'PROCESSING_UPLOAD' – Entwurf bitte prüfen.")
    add_post(clip_id, platform="instagram", status="posted", posted_at=now, title="Alles gut")

    html = dash.client.get("/posted").text
    assert "SELF_ONLY (nur für dich sichtbar)" in html
    assert "nicht geprüften Google-API-Projekten" in html
    assert "300 s" in html
    assert html.count(">Privat</span>") == 2
    assert html.count(">Prüfen</span>") == 1
    assert _data.note_kind(None) == "" and _data.note_kind("Hinweis: bitte prüfen") == "check"


def test_scheduled_note_after_early_upload_is_not_shown_as_error(dash):
    clip_id = add_clip(None)
    add_post(clip_id, platform="youtube", remote_id="yt1", error="Hinweis: läuft")
    add_post(clip_id, platform="instagram", error="Netzwerkfehler – neuer Versuch 21.10. 13:00 UTC")
    html = dash.client.get("/scheduled").text
    assert '<p class="small hint">Hinweis: läuft</p>' in html
    assert '<p class="small error-text">Netzwerkfehler' in html


# --------------------------------------------------------------------------- Fehlgeschlagene Posts neu einplanen


def test_failed_posts_are_grouped_per_clip_with_requeue_button(dash):
    a = add_clip(None)
    b = add_clip(None)
    add_post(a, platform="youtube", status="failed", error="YouTube-Token abgelaufen")
    add_post(a, platform="tiktok", status="failed", error="TikTok-Token abgelaufen")
    add_post(b, platform="instagram", status="failed", error="kaputt")
    html = dash.client.get("/posted").text
    assert html.count(f'action="/clips/{a}/requeue"') == 1
    assert html.count(f'action="/clips/{b}/requeue"') == 1
    assert "YouTube-Token abgelaufen" in html and "TikTok-Token abgelaufen" in html
    assert "Erneut einplanen" in html and "Doppelpost" in html


def test_requeue_via_dashboard_calls_pipeline(dash):
    clip_id = add_clip(None, status="failed")
    pid = add_post(clip_id, platform="tiktok", status="failed", attempts=4, error="kaputt")
    r = dash.client.post(f"/clips/{clip_id}/requeue", follow_redirects=False)
    assert r.headers["location"].startswith("/scheduled?flash=")
    msg = _flash(dash.client, r)
    assert msg == f"ok: Clip #{clip_id} neu eingeplant: TikTok Mi 21.10. 09:00."
    assert dash.pipeline.requeued == [clip_id]
    assert get(Post, pid).status == "scheduled"
    assert "Fehlgeschlagene Uploads" not in dash.client.get("/posted").text


def test_requeue_errors_are_shown(settings, db):
    settings.secrets.dashboard_password = PASSWORD
    app = create_app(settings, pipeline=FakePipeline(fail=ValueError("Das Video von Clip 3 existiert nicht mehr")),
                     publishers={})
    c = make_client(app)
    r = c.post("/clips/3/requeue", headers={"X-WDVI-Ajax": "1"})
    assert r.status_code == 400 and "existiert nicht mehr" in r.json()["message"]
    r = c.post("/clips/3/requeue", follow_redirects=False)
    assert r.headers["location"].startswith("/posted?flash=")

    app = create_app(settings, pipeline=None, publishers={})
    r = make_client(app).post("/clips/3/requeue", headers={"X-WDVI-Ajax": "1"})
    assert r.json() == {"ok": False, "message": NO_PIPELINE}


# --------------------------------------------------------------------------- Freigegeben, aber ohne Slot


def test_approved_clips_without_slot_are_listed_on_scheduled(dash):
    stuck = add_clip(None, status="approved", platforms=["tiktok"], titles={"tiktok": "Hängt fest"},
                     error="Kein freier Slot für tiktok: keine Slots in 14 Tagen")
    fresh = add_clip(None, status="approved", platforms=["youtube"], titles={"youtube": "Gleich dran"})
    add_clip(None, status="pending_review", titles={"youtube": "Noch im Review"})
    html = dash.client.get("/scheduled").text
    assert "Freigegeben, noch nicht eingeplant" in html
    assert f'id="waiting-{stuck}"' in html and f'id="waiting-{fresh}"' in html
    assert "Noch nicht eingeplant: Kein freier Slot für tiktok" in html
    assert "nächsten Durchlauf" in html
    assert "Noch im Review" not in html
    assert "Nichts geplant" not in html
    assert "2 Clips noch ohne Termin" in html


# --------------------------------------------------------------------------- Posts abbrechen -> Clip-Status


def test_cancel_last_post_puts_clip_back_into_review(dash):
    clip_id = add_clip(dash.clips_dir, status="scheduled", platforms=["tiktok"])
    pid = add_post(clip_id, platform="tiktok")
    r = dash.client.post(f"/posts/{pid}/cancel", follow_redirects=False)
    assert "wieder in der Review" in _flash(dash.client, r)
    clip = get(Clip, clip_id)
    assert clip.status == "pending_review" and clip.platforms == []
    html = dash.client.get("/").text
    assert f'id="clip-{clip_id}"' in html and "Alle Posts im Dashboard abgebrochen" in html


def test_cancel_remaining_post_marks_clip_posted(dash):
    clip_id = add_clip(None, status="scheduled", platforms=["youtube", "tiktok"])
    add_post(clip_id, platform="youtube", status="posted", posted_at=datetime.now(UTC))
    pid = add_post(clip_id, platform="tiktok")
    assert dash.client.post(f"/posts/{pid}/cancel", headers={"X-WDVI-Ajax": "1"}).json()["ok"]
    clip = get(Clip, clip_id)
    assert clip.status == "posted" and clip.platforms == ["youtube"]


def test_cancel_keeps_rejected_clip_rejected(dash):
    clip_id = add_clip(None, status="rejected", platforms=["youtube"])
    pid = add_post(clip_id, platform="youtube", remote_id="yt9")  # schon bei YouTube geplant
    r = dash.client.post(f"/posts/{pid}/cancel", headers={"X-WDVI-Ajax": "1"})
    assert r.json()["ok"] and "zusätzlich" in r.json()["message"]
    assert get(Clip, clip_id).status == "rejected"


def test_cancel_with_other_posts_pending_keeps_clip_scheduled(dash):
    clip_id = add_clip(None, status="scheduled", platforms=["youtube", "tiktok"])
    add_post(clip_id, platform="youtube")
    pid = add_post(clip_id, platform="tiktok")
    dash.client.post(f"/posts/{pid}/cancel", headers={"X-WDVI-Ajax": "1"})
    clip = get(Clip, clip_id)
    assert clip.status == "scheduled" and clip.platforms == ["youtube"]


# --------------------------------------------------------------------------- Texte speichern


def test_saved_description_keeps_credit_and_disclaimer(dash):
    cid = add_candidate()
    clip_id = add_clip(dash.clips_dir, candidate_id=cid, descriptions={"tiktok": TT_DESC},
                       plan={"hashtags": ["GTA6", "GTAVI"]})

    def save(desc: str) -> str:
        r = dash.client.post(f"/clips/{clip_id}/texts", data={"desc_tiktok": desc}, headers={"X-WDVI-Ajax": "1"})
        assert r.json()["ok"], r.json()
        return get(Clip, clip_id).descriptions["tiktok"]

    # unverändert (Browser schickt CRLF) -> exakt gleich
    assert save(TT_DESC.replace("\n", "\r\n")) == TT_DESC
    # Anhang gelöscht -> Credit, Fan-Hinweis und Plan-Hashtags kommen zurück
    d = save("Nur noch mein Text")
    assert d.startswith("Nur noch mein Text\n\n" + CREDIT + "\n" + text.FAN_DISCLAIMER)
    assert d.endswith("#GTA6 #GTAVI")
    # Credit-Zeile verändert -> wird ersetzt, nicht doppelt angehängt; eigene Hashtags bleiben
    d = save("Neu\n\n🎥 Clip: falscher Name on Twitch\n#GTA6 #ViceCity")
    assert d.count("🎥 Clip:") == 1 and CREDIT in d and "falscher Name" not in d
    assert d.endswith("#GTA6 #ViceCity")
    assert d.count(text.FAN_DISCLAIMER) == 1


# --------------------------------------------------------------------------- mit der echten Pipeline


def _real(settings, tmp_clip_status="pending_review"):
    from wdvi.pipeline import Pipeline

    settings.secrets.dashboard_password = PASSWORD
    pubs = {"youtube": FakePublisher("youtube", connected=True), "tiktok": FakePublisher("tiktok", connected=True)}
    pipe = Pipeline(settings, publishers=pubs)
    return pipe, make_client(create_app(settings, pipeline=pipe, publishers=pubs))


def test_real_pipeline_requeue_from_dashboard(settings, db):
    pipe, c = _real(settings)
    clips_dir = settings.config.paths.clips_dir
    clip_id = add_clip(clips_dir, status="failed", platforms=["tiktok"])
    pid = add_post(clip_id, platform="tiktok", status="failed", attempts=4, error="Netzwerk weg",
                   scheduled_at=datetime.now(UTC) - timedelta(hours=5))
    html = c.get("/posted").text
    assert f'action="/clips/{clip_id}/requeue"' in html
    r = c.post(f"/clips/{clip_id}/requeue", headers={"X-WDVI-Ajax": "1"})
    assert r.json()["ok"] and "TikTok" in r.json()["message"]
    post = get(Post, pid)
    assert post.status == "scheduled" and post.attempts == 0 and post.error is None
    assert get(Clip, clip_id).status == "scheduled"
    assert "Fehlgeschlagene Uploads" not in c.get("/posted").text
    # nochmal: nichts mehr fehlgeschlagen -> verständliche Meldung statt 500
    r = c.post(f"/clips/{clip_id}/requeue", headers={"X-WDVI-Ajax": "1"})
    assert r.status_code == 400 and "keine fehlgeschlagenen" in r.json()["message"]


def test_real_pipeline_cancel_then_schedule_does_not_recreate_post(settings, db):
    pipe, c = _real(settings)
    now = datetime.now(UTC)
    clip_id = add_clip(None, status="approved", platforms=["youtube", "tiktok"])
    yt = add_post(clip_id, platform="youtube", scheduled_at=now + timedelta(hours=2))
    assert c.post(f"/posts/{yt}/cancel", headers={"X-WDVI-Ajax": "1"}).json()["ok"]
    assert get(Clip, clip_id).status == "approved"  # TikTok wartet noch auf einen Slot
    pipe.schedule_approved(now=now)
    from sqlmodel import select

    from wdvi import db as dbmod

    with dbmod.session_scope() as s:
        posts = s.exec(select(Post).where(Post.clip_id == clip_id)).all()
        live = sorted(p.platform for p in posts if p.status != "canceled")
    assert live == ["tiktok"]  # YouTube bleibt abgebrochen
    assert get(Clip, clip_id).status == "scheduled"


def test_real_pipeline_reapprove_after_all_posts_canceled(settings, db):
    pipe, c = _real(settings)
    clip_id = add_clip(settings.config.paths.clips_dir, status="scheduled", platforms=["tiktok"])
    pid = add_post(clip_id, platform="tiktok")
    c.post(f"/posts/{pid}/cancel", headers={"X-WDVI-Ajax": "1"})
    r = c.post(f"/clips/{clip_id}/approve", data={"platforms": "youtube"}, headers={"X-WDVI-Ajax": "1"})
    assert r.json()["ok"], r.json()
    clip = get(Clip, clip_id)
    assert clip.status == "approved" and clip.platforms == ["youtube"] and clip.error is None


def test_real_pipeline_save_then_approve_keeps_credit(settings, db):
    pipe, c = _real(settings)
    cid = add_candidate()
    clip_id = add_clip(settings.config.paths.clips_dir, candidate_id=cid,
                       descriptions={"tiktok": TT_DESC}, plan={"hashtags": ["GTA6"]})
    edited = "Nur Text ohne Credit"
    r = c.post(f"/clips/{clip_id}/texts", data={"desc_tiktok": edited}, headers={"X-WDVI-Ajax": "1"})
    assert r.json()["ok"]
    saved = get(Clip, clip_id).descriptions["tiktok"]
    # Formular schickt den gespeicherten Text bei der Freigabe unverändert zurück
    r = c.post(f"/clips/{clip_id}/approve", data={"platforms": "tiktok", "desc_tiktok": saved.replace("\n", "\r\n")},
               headers={"X-WDVI-Ajax": "1"})
    assert r.json()["ok"], r.json()
    final = get(Clip, clip_id).descriptions["tiktok"]
    assert final.startswith(edited) and final.count(CREDIT) == 1 and text.FAN_DISCLAIMER in final


# --------------------------------------------------------------------------- Abbrechen über die Pipeline


class _UnschedulingYouTube(FakePublisher):
    def __init__(self) -> None:
        super().__init__("youtube", connected=True)
        self.unscheduled: list[str] = []

    def unschedule(self, remote_id: str) -> str:
        self.unscheduled.append(remote_id)
        return "Bei YouTube zurückgezogen: Das Video ist jetzt privat ohne Veröffentlichungstermin (nicht gelöscht)."


def test_dashboard_cancel_goes_through_pipeline_and_unschedules(settings, db):
    from wdvi.pipeline import Pipeline

    settings.secrets.dashboard_password = PASSWORD
    yt = _UnschedulingYouTube()
    pipe = Pipeline(settings, publishers={"youtube": yt})
    c = make_client(create_app(settings, pipeline=pipe, publishers={"youtube": yt}))
    clip_id = add_clip(None, status="posted", platforms=["youtube"])  # nur bei YouTube geplant
    pid = add_post(clip_id, platform="youtube", remote_id="yt42")
    r = c.post(f"/posts/{pid}/cancel", headers={"X-WDVI-Ajax": "1"})
    assert r.json()["ok"] and "zurückgezogen" in r.json()["message"] and "zusätzlich" not in r.json()["message"]
    assert yt.unscheduled == ["yt42"]
    assert get(Post, pid).status == "canceled"
    assert get(Clip, clip_id).status == "pending_review"


def test_dashboard_cancel_without_pipeline_cancel_uses_dashboard_publishers(dash):
    """FakePipeline ohne cancel_post: das Dashboard baut eine Pipeline mit seinen eigenen Publishern – auch
    dann läuft das Abbrechen über denselben Weg (inkl. unschedule)."""
    unscheduled: list[str] = []
    dash.publishers["youtube"].unschedule = lambda remote_id: unscheduled.append(remote_id) or "zurückgezogen"
    clip_id = add_clip(None, status="scheduled", platforms=["youtube"])
    pid = add_post(clip_id, platform="youtube", remote_id="yt7")
    r = dash.client.post(f"/posts/{pid}/cancel", headers={"X-WDVI-Ajax": "1"})
    assert r.json()["ok"] and unscheduled == ["yt7"] and "zusätzlich" not in r.json()["message"]


def test_scheduled_page_shows_pending_visibility_check(dash):
    clip_id = add_clip(None, status="posted", platforms=["youtube"])
    add_post(clip_id, platform="youtube", remote_id="yt9", scheduled_at=datetime.now(UTC) - timedelta(minutes=5))
    html = dash.client.get("/scheduled").text
    assert "Termin erreicht" in html and "Bereits bei YouTube hochgeladen" not in html


def test_posted_page_flags_youtube_video_locked_private(dash):
    from wdvi.publish.youtube import LOCKED_PRIVATE_HINT

    clip_id = add_clip(None, status="posted", platforms=["youtube"])
    add_post(clip_id, platform="youtube", status="posted", remote_id="yt5", error=LOCKED_PRIVATE_HINT,
             posted_at=datetime.now(UTC) - timedelta(hours=1), remote_url="https://youtube.com/shorts/yt5")
    html = dash.client.get("/posted").text
    assert ">Privat<" in html and str(escape("API-Audit")) in html
