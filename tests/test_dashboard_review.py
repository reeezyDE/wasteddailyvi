"""Dashboard: Review-Queue, Freigeben/Ablehnen über die Pipeline, Texte speichern, Escaping."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from tests.test_dashboard_support import (  # noqa: F401  (Fixture)
    FakePipeline,
    add_candidate,
    add_clip,
    dash_fixture,
    get,
    make_client,
)
from wdvi.dashboard.app import create_app
from wdvi.models import Clip


def _flash_text(client, resp) -> str:
    assert resp.status_code == 303, resp.text
    page = client.get(resp.headers["location"])
    m = re.search(r'<div class="flash flash-(ok|error)"[^>]*>.*?<span>(.*?)</span>', page.text, re.S)
    assert m, "keine Flash-Meldung gefunden"
    return f"{m.group(1)}: {m.group(2)}"


def test_review_page_shows_pending_clip_card(dash):
    cid = add_candidate(title="no way bro", creator_name="xQc", creator_handle="xqc",
                        url="https://clips.twitch.tv/AwkwardSalamander", view_count=12345)
    clip_id = add_clip(dash.clips_dir, filename="sub/a.mp4", candidate_id=cid,
                       thumb_path=str(dash.clips_dir / "sub/a.mp4"),
                       titles={"youtube": "Edited YT title"}, virality_score=91)
    add_clip(dash.clips_dir, status="approved")  # nicht in der Queue

    r = dash.client.get("/")
    assert r.status_code == 200
    html = r.text
    assert f'id="clip-{clip_id}"' in html
    assert html.count("data-clip>") == 1
    assert '<video controls playsinline preload="metadata"' in html
    assert 'src="/media/clips/sub/a.mp4"' in html
    assert "HE ACTUALLY DID IT" in html
    assert "Huge reaction, clean payoff." in html
    assert "91" in html and "score-hot" in html
    assert "no way bro" in html
    assert "Credit: xQc (@xqc)" in html
    assert 'href="https://clips.twitch.tv/AwkwardSalamander"' in html
    assert "12.345" in html
    # Titel: Clip.titles hat Vorrang, sonst aus dem Plan
    assert 'value="Edited YT title"' in html
    assert 'value="Plan TT"' in html
    assert "Plan IG desc" in html
    # Plattform-Checkboxen: alle aktiven Publisher, standardmäßig an
    for p in ("youtube", "tiktok", "instagram"):
        assert f'name="platforms" value="{p}" checked' in html
    assert "Freigeben" in html and "Ablehnen" in html
    # TikTok/Instagram sind nicht verbunden -> Hinweis
    assert "nicht verbunden" in html


def test_review_queue_is_newest_first_and_paginated(dash):
    now = datetime.now(UTC)
    ids = [add_clip(created_at=now - timedelta(minutes=i)) for i in range(12)]  # ids[0] = neuester
    page1 = dash.client.get("/").text
    found = [int(x) for x in re.findall(r'id="clip-(\d+)"', page1)]
    assert found == ids[:10]
    assert "Seite 1 von 2" in page1
    page2 = dash.client.get("/?page=2").text
    assert [int(x) for x in re.findall(r'id="clip-(\d+)"', page2)] == ids[10:]


def test_empty_queue_message(dash):
    r = dash.client.get("/")
    assert "Alles erledigt" in r.text


def test_missing_video_file_shows_hint(dash):
    add_clip(None)  # Pfad außerhalb von clips_dir
    html = dash.client.get("/").text
    assert "Video nicht gefunden" in html
    assert "<video" not in html


def test_approve_calls_pipeline_with_edited_texts_and_platforms(dash):
    clip_id = add_clip(dash.clips_dir)
    form = {
        "platforms": ["youtube", "instagram", "myspace"],
        "title_youtube": "  Neuer YouTube-Titel  ",
        "desc_youtube": "Zeile 1\r\nZeile 2",
        "title_tiktok": "TT neu",
        "desc_tiktok": "TT desc",
        "title_instagram": "IG neu",
        "desc_instagram": "IG desc #gta6",
        "next": "/",
    }
    r = dash.client.post(f"/clips/{clip_id}/approve", data=form, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/?flash=")
    assert dash.pipeline.approved == [(
        clip_id,
        ["youtube", "instagram"],
        {"youtube": "Neuer YouTube-Titel", "tiktok": "TT neu", "instagram": "IG neu"},
        {"youtube": "Zeile 1\nZeile 2", "tiktok": "TT desc", "instagram": "IG desc #gta6"},
    )]
    page = dash.client.get(r.headers["location"]).text
    assert "freigegeben für YouTube, Instagram" in page
    # Clip ist weg aus der Queue
    assert f'id="clip-{clip_id}"' not in page


def test_approve_via_ajax_returns_json(dash):
    clip_id = add_clip(dash.clips_dir)
    other = add_clip(dash.clips_dir)
    r = dash.client.post(f"/clips/{clip_id}/approve", data={"platforms": "tiktok"},
                         headers={"X-WDVI-Ajax": "1"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and "TikTok" in body["message"]
    assert body["pending"] == 1
    assert dash.pipeline.approved[0][:2] == (clip_id, ["tiktok"])
    assert get(Clip, other).status == "pending_review"


def test_approve_without_platform_is_refused(dash):
    clip_id = add_clip(dash.clips_dir)
    r = dash.client.post(f"/clips/{clip_id}/approve", data={"title_youtube": "x"}, follow_redirects=False)
    assert "mindestens eine Plattform" in _flash_text(dash.client, r)
    assert dash.pipeline.approved == []
    r = dash.client.post(f"/clips/{clip_id}/approve", data={}, headers={"X-WDVI-Ajax": "1"})
    assert r.status_code == 400 and r.json()["ok"] is False


def test_approve_requires_youtube_title(dash):
    clip_id = add_clip(dash.clips_dir)
    r = dash.client.post(f"/clips/{clip_id}/approve", data={"platforms": "youtube", "title_youtube": "   "},
                         follow_redirects=False)
    assert "YouTube" in _flash_text(dash.client, r)
    assert dash.pipeline.approved == []


def test_approve_twice_or_unknown_clip_is_refused(dash):
    clip_id = add_clip(dash.clips_dir)
    assert dash.client.post(f"/clips/{clip_id}/approve", data={"platforms": "youtube"},
                            headers={"X-WDVI-Ajax": "1"}).json()["ok"]
    r = dash.client.post(f"/clips/{clip_id}/approve", data={"platforms": "youtube"}, headers={"X-WDVI-Ajax": "1"})
    assert r.status_code == 400 and "schon bearbeitet" in r.json()["message"]
    r = dash.client.post("/clips/9999/approve", data={"platforms": "youtube"}, headers={"X-WDVI-Ajax": "1"})
    assert "nicht gefunden" in r.json()["message"]
    assert len(dash.pipeline.approved) == 1


def test_pipeline_error_is_shown_not_500(dash):
    dash.pipeline.fail = RuntimeError("DB gesperrt access_token=SECRET123")
    clip_id = add_clip(dash.clips_dir)
    r = dash.client.post(f"/clips/{clip_id}/approve", data={"platforms": "youtube"}, follow_redirects=False)
    text = _flash_text(dash.client, r)
    assert text.startswith("error:") and "Freigabe fehlgeschlagen" in text
    assert "SECRET123" not in text
    r = dash.client.post(f"/clips/{clip_id}/reject", data={}, follow_redirects=False)
    assert "Ablehnen fehlgeschlagen" in _flash_text(dash.client, r)


def test_without_pipeline_actions_are_refused(settings, db):
    from tests.test_dashboard_support import PASSWORD

    settings.secrets.dashboard_password = PASSWORD
    app = create_app(settings, pipeline=None, publishers={})
    c = make_client(app)
    clip_id = add_clip(None)
    assert "ohne Pipeline" in c.get("/").text
    r = c.post(f"/clips/{clip_id}/approve", data={"platforms": "youtube"}, headers={"X-WDVI-Ajax": "1"})
    assert r.status_code == 400 and "Pipeline" in r.json()["message"]
    assert get(Clip, clip_id).status == "pending_review"


def test_reject_with_and_without_reason(dash):
    a = add_clip(dash.clips_dir)
    b = add_clip(dash.clips_dir)
    r = dash.client.post(f"/clips/{a}/reject", data={"reason": "  Langweilig  "}, follow_redirects=False)
    assert "abgelehnt" in _flash_text(dash.client, r)
    r = dash.client.post(f"/clips/{b}/reject", data={"reason": ""}, headers={"X-WDVI-Ajax": "1"})
    assert r.json()["ok"] is True and r.json()["pending"] == 0
    assert dash.pipeline.rejected == [(a, "Langweilig"), (b, None)]


def test_reject_redirects_back_to_page_but_not_offsite(dash):
    a = add_clip(dash.clips_dir)
    r = dash.client.post(f"/clips/{a}/reject", data={"next": "/?page=2"}, follow_redirects=False)
    assert r.headers["location"].startswith("/?page=2&flash=")
    b = add_clip(dash.clips_dir)
    r = dash.client.post(f"/clips/{b}/reject", data={"next": "//evil.example/x"}, follow_redirects=False)
    assert r.headers["location"].startswith("/?flash=")


def test_save_texts_updates_pending_clip_only(dash):
    # realistischer GTA-Plan (v1-Pläne hatten immer gta6 + "GTA 6" im Titel); #GTA6 nur bei GTA-Inhalten
    plan = {"hook_text": "HE ACTUALLY DID IT", "reasoning": "r", "hashtags": ["gta6", "gaming"],
            "titles": {"youtube": "He did it | GTA 6", "tiktok": "Plan TT", "instagram": "Plan IG"},
            "descriptions": {"youtube": "Plan YT desc", "tiktok": "Plan TT desc", "instagram": "Plan IG desc"}}
    clip_id = add_clip(dash.clips_dir, titles={"youtube": "alt", "tiktok": "bleibt"}, plan=plan)
    r = dash.client.post(f"/clips/{clip_id}/texts",
                         data={"title_youtube": "Neu\nTitel", "desc_youtube": "Neue Beschreibung",
                               "platforms": "youtube"},
                         headers={"X-WDVI-Ajax": "1"})
    assert r.json() == {"ok": True, "message": "Texte gespeichert."}
    clip = get(Clip, clip_id)
    assert clip.titles == {"youtube": "Neu Titel", "tiktok": "bleibt"}
    # Credit + Fan-Hinweis + #GTA6 hängt das Dashboard (wie die Freigabe) immer an
    assert list(clip.descriptions) == ["youtube"]
    assert clip.descriptions["youtube"].startswith("Neue Beschreibung\n\n🎥 Clip: xQc on Twitch")
    assert "Fan channel – not affiliated" in clip.descriptions["youtube"]
    assert clip.descriptions["youtube"].endswith("#GTA6 #gaming")
    assert clip.status == "pending_review"
    assert dash.pipeline.approved == []

    done = add_clip(dash.clips_dir, status="scheduled")
    r = dash.client.post(f"/clips/{done}/texts", data={"title_youtube": "x"}, headers={"X-WDVI-Ajax": "1"})
    assert r.status_code == 400 and "nicht mehr im Review" in r.json()["message"]


def test_everything_is_escaped_and_bad_links_dropped(dash):
    cid = add_candidate(title="<script>alert('t')</script>", creator_name="<img src=x onerror=alert(1)>",
                        url="javascript:alert(1)", creator_url="data:text/html,hi")
    add_clip(dash.clips_dir, candidate_id=cid,
             plan={"hook_text": "<b>hook</b>", "reasoning": "</blockquote><script>x()</script>",
                   "titles": {"youtube": '"><script>y()</script>'}})
    html = dash.client.get("/").text
    assert "<script>alert" not in html and "<script>x()" not in html and "<script>y()" not in html
    assert "&lt;script&gt;alert(&#39;t&#39;)&lt;/script&gt;" in html
    assert "<img src=x" not in html
    assert "javascript:" not in html
    assert 'href="data:' not in html
    assert "&lt;b&gt;hook&lt;/b&gt;" in html


def test_post_with_invalid_id_is_400_and_get_on_post_route_is_405(dash):
    r = dash.client.post("/clips/abc/approve", data={}, headers={"X-WDVI-Ajax": "1"})
    assert r.status_code == 400
    r = dash.client.get("/clips/1/approve")
    assert r.status_code == 405
    assert "Buttons" in r.text


def test_unknown_page_is_german_404(dash):
    r = dash.client.get("/gibtsnicht")
    assert r.status_code == 404
    assert "Diese Seite gibt es nicht" in r.text


def test_pending_badge_in_navigation(dash):
    add_clip(dash.clips_dir)
    add_clip(dash.clips_dir)
    html = dash.client.get("/health").text
    assert re.search(r'data-pending-count>\s*2\s*<', html)


def test_platform_list_follows_config_without_publishers(settings, db):
    from tests.test_dashboard_support import PASSWORD

    settings.secrets.dashboard_password = PASSWORD
    settings.config.publish.tiktok.enabled = True
    app = create_app(settings, pipeline=FakePipeline(), publishers={})
    add_clip(None)
    html = make_client(app).get("/").text
    assert 'value="youtube"' in html and 'value="tiktok"' in html
    assert 'value="instagram"' not in html


def test_previously_chosen_platforms_are_preselected(dash):
    add_clip(dash.clips_dir, platforms=["tiktok"])
    html = dash.client.get("/").text
    assert 'name="platforms" value="tiktok" checked' in html
    assert 'name="platforms" value="youtube">' in html


def test_unexpected_error_gives_german_500_page(dash, monkeypatch, caplog):
    from fastapi.testclient import TestClient

    from wdvi.dashboard import _data

    def boom(*a, **k):
        raise RuntimeError("kaputt https://graph.instagram.com/me?access_token=SECRETTOKEN")

    monkeypatch.setattr(_data, "candidates", boom)
    c = TestClient(dash.app, raise_server_exceptions=False, headers=dict(dash.client.headers))
    r = c.get("/candidates")
    assert r.status_code == 500
    assert "Interner Fehler im Dashboard" in r.text
    assert "SECRETTOKEN" not in r.text
    assert "SECRETTOKEN" not in caplog.text
    assert "access_token=***" in caplog.text
