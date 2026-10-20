"""Datenbankabfragen fürs Dashboard -> einfache Dicts für die Templates (keine ORM-Objekte im Template)."""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from sqlmodel import col, func, select

from wdvi import db
from wdvi.dashboard._fmt import as_utc
from wdvi.models import Candidate, Clip, CreatorRule, JobRun, Metric, Post
from wdvi.publish import text

log = logging.getLogger(__name__)

PLATFORM_NAMES = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"}
ALL_PLATFORMS = ("youtube", "tiktok", "instagram")
SOURCE_LABELS = ("Twitch", "Kick", "YouTube", "Reddit")
RULES = {
    "block": "Blockieren",
    "allow": "Erlauben",
    "boost": "Bevorzugen",
}

CANDIDATE_STATUS = {
    "new": "Neu",
    "queued": "Wartend",
    "processing": "In Arbeit",
    "rendered": "Gerendert",
    "rejected": "Abgelehnt",
    "skipped": "Übersprungen",
    "failed": "Fehler",
    "mined": "Zerlegt",
}
CLIP_STATUS = {
    "pending_review": "Wartet auf Review",
    "approved": "Freigegeben",
    "scheduled": "Geplant",
    "posted": "Gepostet",
    "rejected": "Abgelehnt",
    "failed": "Fehler",
}
POST_STATUS = {
    "scheduled": "Geplant",
    "uploading": "Lädt hoch",
    "posted": "Gepostet",
    "draft_sent": "Als Entwurf gesendet",
    "failed": "Fehlgeschlagen",
    "canceled": "Abgebrochen",
}

MAX_TEXT = 10_000
# optionale Einträge in .env: "fehlt" ist hier kein Problem
OPTIONAL_SECRETS = {
    "youtube_api_key": "optional (nur für sources.youtube.search_queries)",
    "reddit_client_id": "optional (sonst öffentliche Reddit-Schnittstelle)",
    "reddit_client_secret": "optional (sonst öffentliche Reddit-Schnittstelle)",
    "tiktok_redirect_uri": "optional (Standard: public_base_url mit https)",
    "instagram_redirect_uri": "optional (Standard: public_base_url mit https)",
    "ytdlp_cookies_file": "optional (nur bei YouTube-Bot-Check nötig)",
}


def platform_name(p: str) -> str:
    return PLATFORM_NAMES.get(p, p.capitalize())


# --------------------------------------------------------------------------- Medien-URLs


def media_url(path: str | None, clips_dir: Path) -> str:
    """Datei in clips_dir -> '/media/clips/<rel>'; außerhalb oder fehlend -> ''."""
    if not path:
        return ""
    try:
        p = Path(path)
        if not p.is_absolute():
            p = Path.cwd() / p
        rel = p.resolve().relative_to(clips_dir.resolve())
    except (ValueError, OSError):
        return ""
    if not (clips_dir.resolve() / rel).is_file():
        return ""
    return "/media/clips/" + "/".join(quote(part) for part in rel.parts)


# --------------------------------------------------------------------------- Review


def pending_count() -> int:
    with db.session_scope() as s:
        return int(s.exec(select(func.count()).select_from(Clip).where(Clip.status == "pending_review")).one())


def _text_for(clip: Clip, field: str, platform: str) -> str:
    own = clip.titles if field == "titles" else clip.descriptions
    if isinstance(own, dict) and own.get(platform):
        return str(own[platform])
    plan = clip.plan if isinstance(clip.plan, dict) else {}
    fallback = plan.get(field) if isinstance(plan.get(field), dict) else {}
    return str(fallback.get(platform) or "")


def _candidate_dict(c: Candidate | None) -> dict[str, Any]:
    if c is None:
        return {"title": "(Kandidat fehlt)", "platform_label": "?", "creator_name": "?", "url": "",
                "creator_url": "", "creator_handle": None, "view_count": None, "created_at": None,
                "duration_s": None, "source": "", "engagement": {}, "thumbnail_url": ""}
    return {
        "id": c.id,
        "title": c.title,
        "platform_label": c.platform_label,
        "creator_name": c.creator_name,
        "creator_handle": c.creator_handle,
        "creator_url": c.creator_url or "",
        "url": c.url,
        "view_count": c.view_count,
        "created_at": as_utc(c.created_at),
        "duration_s": c.duration_s,
        "source": c.source,
        "engagement": c.engagement if isinstance(c.engagement, dict) else {},
        "thumbnail_url": c.thumbnail_url or "",
        "start_s": c.start_s,
        "end_s": c.end_s,
    }


def review_items(platforms: list[str], clips_dir: Path, offset: int, limit: int) -> tuple[list[dict], int]:
    """Clips mit Status pending_review, neueste zuerst."""
    with db.session_scope() as s:
        total = int(
            s.exec(select(func.count()).select_from(Clip).where(Clip.status == "pending_review")).one()
        )
        clips = s.exec(
            select(Clip)
            .where(Clip.status == "pending_review")
            .order_by(col(Clip.created_at).desc(), col(Clip.id).desc())
            .offset(offset)
            .limit(limit)
        ).all()
        cand_ids = {c.candidate_id for c in clips}
        cands = {c.id: c for c in s.exec(select(Candidate).where(col(Candidate.id).in_(cand_ids))).all()}

    items = []
    for clip in clips:
        plan = clip.plan if isinstance(clip.plan, dict) else {}
        analysis = clip.analysis if isinstance(clip.analysis, dict) else {}
        chosen = [p for p in (clip.platforms or []) if p in platforms]
        items.append(
            {
                "id": clip.id,
                "score": clip.virality_score or plan.get("virality_score") or 0,
                "hook": str(plan.get("hook_text") or ""),
                "reasoning": str(plan.get("reasoning") or ""),
                "is_leak": bool(plan.get("is_leak_or_unreleased")),
                "is_unsafe": bool(plan.get("is_unsafe")),
                "layout": plan.get("layout") or analysis.get("layout") or "",
                "hashtags": [str(h) for h in (plan.get("hashtags") or [])][:12],
                "language": plan.get("content_language") or analysis.get("language") or "",
                "music_prob": analysis.get("music_prob"),
                "duration": clip.duration,
                "created_at": as_utc(clip.created_at),
                "video_url": media_url(clip.video_path, clips_dir),
                "thumb_url": media_url(clip.thumb_path, clips_dir),
                "video_name": Path(clip.video_path).name if clip.video_path else "",
                "note": clip.error or "",  # z. B. Musik-Prüfung ausgefallen, Posts abgebrochen
                "cand": _candidate_dict(cands.get(clip.candidate_id)),
                "texts": [
                    {
                        "platform": p,
                        "name": platform_name(p),
                        "title": _text_for(clip, "titles", p),
                        "description": _text_for(clip, "descriptions", p),
                        "checked": (p in chosen) if chosen else True,
                    }
                    for p in platforms
                ],
            }
        )
    return items, total


def get_clip_status(clip_id: int) -> str | None:
    with db.session_scope() as s:
        clip = s.get(Clip, clip_id)
        return clip.status if clip else None


CREDIT_PREFIX = "🎥 Clip:"  # Anfang von text.credit_line()


def _candidate_in(cand: Candidate | None) -> Any:
    """Candidate-Zeile -> CandidateIn (für die Credit-Zeile); None, wenn das nicht geht."""
    if cand is None:
        return None
    try:
        from wdvi.pipeline import candidate_to_in  # erst hier: zieht Claude-/Render-Module nach

        return candidate_to_in(cand)
    except Exception as e:  # noqa: BLE001 - dann eben ohne neuen Credit
        log.warning("candidate %s: cannot build credit (%s)", cand.id, e)
        return None


def compose_edited_description(
    platform: str,
    edited: str,
    current: str | None,
    hashtags: list[str],
    cin: Any,
    settings: Any,
    *,
    plan: dict | None = None,
    transcript: str | None = None,
) -> str:
    """Bearbeitete Beschreibung wie bei der Freigabe (Pipeline._edited_description) aufbereiten:
    Unveränderter Text bleibt exakt; sonst wird der alte Anhang (Credit, Fan-Hinweis, Transkript, Hashtag-Zeile)
    abgetrennt und neu angehängt. So gehen Credit, Fan-Hinweis und Transkript (YouTube) auch über
    „Nur Texte speichern“ nicht verloren (die Freigabe übernimmt gespeicherte Texte unverändert).
    Steht im Anhang noch ein "Transcript:"-Block, gilt dessen (evtl. korrigierter) Text; wurde nur der
    Transkript-Block gelöscht, bleibt er weg; fehlt der ganze Anhang, kommt transcript (gespeichert) zurück.
    plan (Clip.plan) liefert Titel und Tags für die GTA-Erkennung (#GTA6 nur bei GTA-Inhalten)."""
    d = (edited or "").replace("\r\n", "\n").strip()
    if current is not None and d == current.strip():
        return current
    if cin is None:
        return d
    try:
        markers = [m for m in (text.credit_line(cin), text.FAN_DISCLAIMER, CREDIT_PREFIX) if m]
        cuts = [d.find(m) for m in markers if m in d]
        body, tail_tags, spoken = d, None, transcript
        if cuts:
            cut = min(cuts)
            body, tail = d[:cut].rstrip(), d[cut:]
            tail_tags, spoken = text.hashtags_in(tail), text.transcript_in(tail)
        pl = plan if isinstance(plan, dict) else {}
        titles = pl.get("titles") if isinstance(pl.get("titles"), dict) else {}
        return text.compose_description(
            platform,
            body,
            tail_tags or hashtags,
            cin,
            settings,
            transcript=spoken,
            title=" ".join(str(t) for t in titles.values() if t),
            tags=[str(t) for t in (pl.get("tags") or [])],
        )
    except Exception as e:  # noqa: BLE001 - lieber Text behalten als Speichern scheitern lassen
        log.warning("compose_description failed for %s: %s", platform, e)
        return d


def save_clip_texts(
    clip_id: int, titles: dict[str, str], descriptions: dict[str, str], settings: Any = None
) -> str | None:
    """Titel/Beschreibungen eines wartenden Clips ändern. Gibt Fehlertext oder None zurück."""
    with db.session_scope() as s:
        clip = s.get(Clip, clip_id)
        if clip is None:
            return f"Clip #{clip_id} nicht gefunden."
        if clip.status != "pending_review":
            return f"Clip #{clip_id} ist nicht mehr im Review (Status: {CLIP_STATUS.get(clip.status, clip.status)})."
        old = dict(clip.descriptions or {})
        new_desc = dict(old)
        if descriptions:
            cin = _candidate_in(s.get(Candidate, clip.candidate_id))
            plan = clip.plan if isinstance(clip.plan, dict) else {}
            hashtags = [str(h) for h in (plan.get("hashtags") or [])]
            spoken = str((clip.analysis or {}).get("transcript_kept") or "") or None
            for p, d in descriptions.items():
                # ohne gespeicherten Text (nur Plan-Rohtext ohne Credit) immer neu zusammensetzen
                new_desc[p] = compose_edited_description(
                    p, d, old.get(p) or None, hashtags, cin, settings, plan=plan, transcript=spoken
                )
        # JSON-Spalten erkennen keine In-place-Änderungen -> neue Dicts zuweisen
        clip.titles = {**(clip.titles or {}), **titles}
        clip.descriptions = new_desc
        s.add(clip)
    return None


# --------------------------------------------------------------------------- Posts


def _clip_infos(s, clip_ids: set[int], clips_dir: Path) -> dict[int, dict]:
    if not clip_ids:
        return {}
    clips = s.exec(select(Clip).where(col(Clip.id).in_(clip_ids))).all()
    cand_ids = {c.candidate_id for c in clips}
    cands = {c.id: c for c in s.exec(select(Candidate).where(col(Candidate.id).in_(cand_ids))).all()}
    out = {}
    for c in clips:
        cand = cands.get(c.candidate_id)
        plan = c.plan if isinstance(c.plan, dict) else {}
        out[c.id] = {
            "thumb_url": media_url(c.thumb_path, clips_dir),
            "video_url": media_url(c.video_path, clips_dir),
            "hook": str(plan.get("hook_text") or ""),
            "creator": cand.creator_name if cand else "",
            "source_label": cand.platform_label if cand else "",
            "source_url": cand.url if cand else "",
        }
    return out


def caption_for(p: Post) -> str:
    """TikTok/Instagram: komplette Caption (Titel + Beschreibung mit Credit, Fan-Hinweis, Hashtags) –
    genau wie beim Direktpost. Im TikTok-Inbox-Modus muss sie von Hand in der App eingefügt werden."""
    if p.platform not in ("tiktok", "instagram"):
        return ""
    try:
        return text.build_caption(p.platform, p.title or "", p.description or "")
    except Exception:  # noqa: BLE001 - Anzeige darf nie crashen
        return "\n\n".join(x for x in (p.title, p.description) if x)


def note_kind(note: str | None) -> str:
    """Hinweis zu einem erfolgreichen Upload einordnen: 'private' (nur privat sichtbar), 'check' oder ''."""
    n = (note or "").lower()
    if not n:
        return ""
    if "self_only" in n or "privat" in n:
        return "private"
    return "check"


def _post_dict(p: Post, info: dict | None) -> dict[str, Any]:
    return {
        "id": p.id,
        "clip_id": p.clip_id,
        "platform": p.platform,
        "platform_name": platform_name(p.platform),
        "status": p.status,
        "status_label": POST_STATUS.get(p.status, p.status),
        "scheduled_at": as_utc(p.scheduled_at),
        "posted_at": as_utc(p.posted_at),
        "title": p.title,
        "caption": caption_for(p),
        "remote_id": p.remote_id,
        "remote_url": p.remote_url or "",
        "attempts": p.attempts,
        "error": p.error or "",
        # bei erfolgreichen Uploads ist "error" nur ein Hinweis (privat gepostet, Status-Timeout …)
        "note_kind": note_kind(p.error) if p.status in ("posted", "draft_sent") else "",
        # bei der Plattform geplant und Termin vorbei: die Pipeline prüft gerade, ob es wirklich öffentlich ist
        "awaiting_check": bool(
            p.remote_id and p.status == "scheduled" and p.scheduled_at
            and as_utc(p.scheduled_at) <= datetime.now(UTC)
        ),
        "clip": info or {},
    }


def scheduled_posts(clips_dir: Path) -> list[dict]:
    with db.session_scope() as s:
        posts = s.exec(
            select(Post)
            .where(col(Post.status).in_(["scheduled", "uploading"]))
            .order_by(col(Post.scheduled_at).asc(), col(Post.id).asc())
            .limit(300)
        ).all()
        infos = _clip_infos(s, {p.clip_id for p in posts}, clips_dir)
    return [_post_dict(p, infos.get(p.clip_id)) for p in posts]


def approved_waiting(clips_dir: Path, limit: int = 50) -> list[dict]:
    """Freigegebene Clips ohne (vollständige) Einplanung – z. B. kein freier Slot oder Plattform aus."""
    with db.session_scope() as s:
        clips = s.exec(
            select(Clip)
            .where(Clip.status == "approved")
            .order_by(col(Clip.reviewed_at).asc(), col(Clip.id).asc())
            .limit(limit)
        ).all()
        infos = _clip_infos(s, {c.id for c in clips}, clips_dir)
    out = []
    for c in clips:
        titles = c.titles if isinstance(c.titles, dict) else {}
        title = next((str(titles[p]) for p in ALL_PLATFORMS if titles.get(p)), "")
        out.append(
            {
                "id": c.id,
                "title": title or str((c.plan or {}).get("hook_text") or f"Clip #{c.id}"),
                "platforms": [{"platform": p, "name": platform_name(p)} for p in (c.platforms or [])],
                "error": c.error or "",
                "reviewed_at": as_utc(c.reviewed_at),
                "clip": infos.get(c.id, {}),
            }
        )
    return out


def posted_posts(clips_dir: Path, limit: int = 100) -> list[dict]:
    with db.session_scope() as s:
        posts = s.exec(
            select(Post)
            .where(col(Post.status).in_(["posted", "draft_sent"]))
            .order_by(col(Post.posted_at).desc(), col(Post.id).desc())
            .limit(limit)
        ).all()
        infos = _clip_infos(s, {p.clip_id for p in posts}, clips_dir)
        metrics: dict[int, Metric] = {}
        ids = [p.id for p in posts]
        if ids:
            for m in s.exec(
                select(Metric).where(col(Metric.post_id).in_(ids)).order_by(col(Metric.fetched_at), col(Metric.id))
            ).all():
                metrics[m.post_id] = m  # letzter gewinnt
    out = []
    for p in posts:
        d = _post_dict(p, infos.get(p.clip_id))
        m = metrics.get(p.id)
        d["metrics"] = (
            {
                "views": m.views,
                "likes": m.likes,
                "comments": m.comments,
                "shares": m.shares,
                "hours_after": m.hours_after,
                "fetched_at": as_utc(m.fetched_at),
            }
            if m
            else None
        )
        out.append(d)
    return out


def failed_posts(clips_dir: Path, limit: int = 20) -> list[dict]:
    with db.session_scope() as s:
        posts = s.exec(
            select(Post).where(Post.status == "failed").order_by(col(Post.id).desc()).limit(limit)
        ).all()
        infos = _clip_infos(s, {p.clip_id for p in posts}, clips_dir)
    return [_post_dict(p, infos.get(p.clip_id)) for p in posts]


def failed_by_clip(clips_dir: Path, limit: int = 20) -> list[dict]:
    """Fehlgeschlagene Posts je Clip (neu einplanen geht nur für den ganzen Clip: wdvi requeue)."""
    groups: dict[int, dict] = {}
    for p in failed_posts(clips_dir, limit):
        g = groups.setdefault(p["clip_id"], {"clip_id": p["clip_id"], "clip": p["clip"], "posts": []})
        g["posts"].append(p)
    return list(groups.values())


# --------------------------------------------------------------------------- Kandidaten


def candidate_status_counts() -> dict[str, int]:
    with db.session_scope() as s:
        rows = s.exec(select(Candidate.status, func.count()).group_by(Candidate.status)).all()
    return {str(k): int(v) for k, v in rows}


def candidates(status: str | None, limit: int = 150) -> list[dict]:
    with db.session_scope() as s:
        q = select(Candidate)
        if status:
            q = q.where(Candidate.status == status)
        rows = s.exec(q.order_by(col(Candidate.discovered_at).desc(), col(Candidate.id).desc()).limit(limit)).all()
    out = []
    for c in rows:
        d = _candidate_dict(c)
        d.update(
            {
                "status": c.status,
                "status_label": CANDIDATE_STATUS.get(c.status, c.status),
                "status_reason": c.status_reason or "",
                "prescore": c.prescore,
                "discovered_at": as_utc(c.discovered_at),
                "updated_at": as_utc(c.updated_at),
            }
        )
        out.append(d)
    return out


# --------------------------------------------------------------------------- Creator-Regeln

def normalize_handle(raw: str) -> str:
    """'@XQC', 'https://twitch.tv/xqc', ' xqc ' -> 'xqc' (lowercase, ohne @)."""
    text = (raw or "").strip()
    if "://" in text or text.lower().startswith(("www.", "twitch.tv/", "kick.com/", "youtube.com/", "reddit.com/")):
        try:
            parts = [p for p in urlsplit(text if "://" in text else "https://" + text).path.split("/") if p]
        except ValueError:
            parts = []
        if parts and parts[0].lower() in ("user", "u", "c", "channel") and len(parts) > 1:
            text = parts[1]
        elif parts:
            text = parts[0]
        else:
            text = ""
    text = text.strip().lstrip("@").strip().lower()
    return re.sub(r"\s+", " ", text)[:100]


def creator_rules() -> list[dict]:
    with db.session_scope() as s:
        rows = s.exec(
            select(CreatorRule).order_by(col(CreatorRule.rule), col(CreatorRule.platform_label), col(CreatorRule.handle))
        ).all()
    return [
        {
            "id": r.id,
            "platform_label": r.platform_label,
            "handle": r.handle,
            "rule": r.rule,
            "rule_label": RULES.get(r.rule, r.rule),
            "note": r.note or "",
            "created_at": as_utc(r.created_at),
        }
        for r in rows
    ]


def upsert_creator_rule(platform_label: str, handle: str, rule: str, note: str | None) -> tuple[bool, str]:
    if platform_label not in SOURCE_LABELS:
        return False, "Unbekannte Plattform."
    if rule not in RULES:
        return False, "Unbekannte Regel (erlaubt: block, allow, boost)."
    h = normalize_handle(handle)
    if not h:
        return False, "Bitte einen Creator-Namen bzw. Handle eingeben."
    note = (note or "").strip()[:300] or None
    with db.session_scope() as s:
        existing = s.exec(
            select(CreatorRule).where(CreatorRule.platform_label == platform_label, CreatorRule.handle == h)
        ).first()
        if existing:
            existing.rule = rule
            existing.note = note
            s.add(existing)
            return True, f"Regel für {h} ({platform_label}) aktualisiert: {RULES[rule]}."
        s.add(CreatorRule(platform_label=platform_label, handle=h, rule=rule, note=note))
    return True, f"Regel für {h} ({platform_label}) angelegt: {RULES[rule]}."


def delete_creator_rule(rule_id: int) -> tuple[bool, str]:
    with db.session_scope() as s:
        r = s.get(CreatorRule, rule_id)
        if r is None:
            return False, "Regel nicht gefunden."
        label = f"{r.handle} ({r.platform_label})"
        s.delete(r)
    return True, f"Regel für {label} gelöscht."


# --------------------------------------------------------------------------- Health


def _stale_after_s(job: str, settings) -> float:
    """Ab wann ein Job als 'überfällig' gilt (grob: ~3 verpasste Läufe)."""
    src = settings.config.sources
    if job.startswith("discover:"):
        name = job.split(":", 1)[1]
        poll = getattr(getattr(src, name, None), "poll_minutes", 30)
        return max(30, poll * 3) * 60
    table = {"process": 30 * 60, "publish": 30 * 60, "schedule": 30 * 60, "stats": 7 * 3600,
             "cleanup": 50 * 3600}
    for prefix, secs in table.items():
        if job.startswith(prefix):
            return secs
    return 26 * 3600


def latest_jobs(settings, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(UTC)
    with db.session_scope() as s:
        latest_ids = select(func.max(JobRun.id)).group_by(JobRun.job)
        runs = s.exec(select(JobRun).where(col(JobRun.id).in_(latest_ids)).order_by(col(JobRun.job))).all()
        since = now - timedelta(hours=24)
        fail_rows = s.exec(
            select(JobRun.job, func.count())
            .where(JobRun.ok == False, JobRun.started_at >= since)  # noqa: E712
            .group_by(JobRun.job)
        ).all()
    fails = {str(j): int(n) for j, n in fail_rows}
    out = []
    for r in runs:
        started = as_utc(r.started_at)
        finished = as_utc(r.finished_at)
        age = (now - (finished or started)).total_seconds() if (finished or started) else None
        running = finished is None
        stale = age is not None and age > _stale_after_s(r.job, settings)
        out.append(
            {
                "job": r.job,
                "started_at": started,
                "finished_at": finished,
                "duration_s": (finished - started).total_seconds() if finished and started else None,
                "ok": bool(r.ok),
                "running": running,
                "stale": stale,
                "message": (r.message or "")[:500],
                "fails_24h": fails.get(r.job, 0),
                "state": "running" if running and not stale else ("error" if not r.ok else ("stale" if stale else "ok")),
            }
        )
    return out


def status_counts() -> dict[str, dict[str, int]]:
    with db.session_scope() as s:
        out = {}
        for name, model in (("candidates", Candidate), ("clips", Clip), ("posts", Post)):
            rows = s.exec(select(model.status, func.count()).group_by(model.status)).all()
            out[name] = {str(k): int(v) for k, v in rows}
    return out


def secrets_present(settings) -> list[dict]:
    """Welche Secrets gesetzt sind – NUR Booleans, niemals Werte."""
    sec = settings.secrets
    out = []
    for name, field in type(sec).model_fields.items():
        env = field.validation_alias if isinstance(field.validation_alias, str) else f"WDVI_{name.upper()}"
        value = getattr(sec, name, None)
        present = bool(value and str(value).strip())
        item = {"env": env, "present": present, "note": ""}
        if name in ("google_client_secrets_file", "ytdlp_cookies_file") and present:
            exists = Path(os.path.expanduser(str(value))).is_file()
            item["note"] = "Datei gefunden" if exists else "Datei NICHT gefunden"
            item["present"] = exists
        elif name in OPTIONAL_SECRETS and not present:
            item["note"] = OPTIONAL_SECRETS[name]
        if name == "anthropic_api_key" and settings.config.llm.backend == "cli":
            item["note"] = "nicht nötig (CLI-Backend)"
        out.append(item)
    return out


def text_limit(s: str) -> str:
    return (s or "").replace("\r\n", "\n").strip()[:MAX_TEXT]
