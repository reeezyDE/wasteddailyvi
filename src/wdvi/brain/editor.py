"""Claude als Viral-Redakteur: bewertet einen Clip und plant Schnitt + Verpackung."""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from wdvi.brain import prompts
from wdvi.brain.claude import LLMClient
from wdvi.config import Settings
from wdvi.schemas import (
    Annotation,
    Box,
    CandidateIn,
    EditPlan,
    Emphasis,
    FrameSample,
    MediaAnalysis,
    MomentPick,
    MomentPicks,
    PerformanceExample,
    PlatformText,
    Segment,
    Shot,
    SourceKind,
    Transcript,
    Word,
    WordColor,
    Zoom,
)
from wdvi.sources.youtube import HeatPoint, VideoContext

log = logging.getLogger(__name__)

# Prompt
MAX_TRANSCRIPT_WORDS = 800  # Wort-Ebene im Editor-Prompt; darüber Segment-Ebene
MAX_SEGMENT_CHARS = 12000
MOMENT_WORD_BUDGET = 6000  # Transkript-Wörter im Momente-Prompt (~40 min Sprache)
MAX_HEAT_PEAKS = 12

MAX_RECENT_POSTS = 20  # letzte Hooks/Titel, die Claude nicht wiederverwenden soll
MAX_OUTPUT_TOKENS = 64000  # gestreamt; Thinking bei hohem effort braucht Platz (Abbruch = bezahlter Fehlschlag)

# harte Regeln (sanitize_plan) – dieselben Zahlen stehen als HARD LIMITS im Prompt
MIN_REMOVE_S = 0.25  # kürzere Jump-Cuts werden verworfen
MIN_KEEP_S = 0.2  # kürzere Reststücke zwischen/neben Schnitten werden mit weggeschnitten
MIN_ZOOM_S = 0.3
MAX_ZOOMS = 3
MAX_EMOJIS = 4
MAX_SFX = 4  # inkl. des automatischen whoosh je Zoom (Renderer)
ZOOM_WHOOSH_MERGE_S = 0.3  # so nah am Zoom-Start legt der Renderer zwei whooshs zusammen (SFX_DEDUP_S)
ZOOM_WHOOSH_GAP_S = 0.6  # darüber hinaus bis hierhin: eigener whoosh klänge doppelt -> weg
SLUR_PAD_S = 0.05  # Slur-Schnitt: etwas Rand um das Wort
SLUR_MIN_CUT_S = 0.32
MAX_HASHTAGS = 8  # Plan darf bis zu 8 haben (Rest geht in die YouTube-Tags) ...
PROMPT_HASHTAGS = 5  # ... die Beschreibung zeigt aber nur 3–5 (publish.text.MAX_HASHTAGS)
HOOK_MAX_WORDS = 6
HOOK_MAX_CHARS = 32
MAX_HIGHLIGHT_WORDS = 6
YT_TITLE_MAX = 100
SHORT_TITLE_MAX = 150
DESCRIPTION_MAX = {"youtube": 3000, "tiktok": 1200, "instagram": 1200}  # Platz für Credit + Hashtags
MOMENT_MIN_S = 15.0
MOMENT_MAX_S = 55.0
SNAP_TOLERANCE_S = 2.0

# Edit-Engine v2 (sanitize_plan)
MIN_SHOT_S = 0.6  # kürzere Shots (behaltene Zeit) werden mit dem Nachbarn verschmolzen (Punch-ins ab 0.6 s)
MAX_SHOTS = 48
SHOT_ZOOM_MIN, SHOT_ZOOM_MAX = 1.0, 1.6
MAX_GLITCH = 2  # glitch nur sparsam, Rest wird zu cut
MIN_ANNOTATION_S = 0.4
MAX_ANNOTATIONS = 3
MIN_BOX = 0.03  # Mindestbreite/-höhe normalisierter Boxen
LABEL_MAX_WORDS = 3
LABEL_MAX_CHARS = 24
MAX_WORD_COLORS = 12
WORD_COLOR_MAX_CHARS = 32
OUTRO_MAX_WORDS = 8
OUTRO_MAX_CHARS = 48
MAX_TAGS = 15
TAG_MAX_CHARS = 30
TAGS_TOTAL_MAX = 450  # YouTube erlaubt 500 Zeichen für alle Tags zusammen
MAX_SHAKES = 3
MAX_FLASHES = 2
HOOK_SPOKEN_WINDOW_S = 2.0  # hook_style "none" braucht gesprochene Worte so kurz nach trim_start

# Koordinatengitter auf Kopien der Frames (Claude liest normalisierte Boxen ab)
GRID_DIR = "_grid"
GRID_STEPS = 10

_UPPER_KEEP = {"GTA", "GTA6", "GTAVI", "VI", "NPC", "NPCS", "RP", "LSPD", "FIB", "IRL", "OMG", "WTF", "LOL",
               "LMAO", "POV", "FPS", "PC", "PS5", "XBOX", "AI", "USA", "US", "UK", "TV"}

# Sicherheitsnetz für eingebrannte Untertitel: schwere Slurs (Varianten/Leetspeak), ganze Wörter
_SLUR_RE = re.compile(
    r"(?<![a-z])(?:n+[i1!|]+gg+(?:a+|ah+|az|er+|uh)s?|f+[a@4]+g+(?:s|g?[o0]ts?)?|tr[a@4]nn(?:y|ies)"
    r"|k[iy1]kes?|ch[i1]nks?|sp[i1]cs?|wetbacks?|g[o0]{2}ks?|r+[e3]+t[a@4]+r+d+(?:s|ed)?)(?![a-z])",
    re.IGNORECASE)
_MENTION_RE = re.compile(r"(?<![\w@.])@(?=\w)")  # @handle (keine E-Mail-Adressen)
_GTA6_VARIANT_RE = re.compile(r"(?<![#\w])gta[\s-]*(?:6|vi)(?!\w)", re.IGNORECASE)
_GTA_ANY_RE = re.compile(r"gta|grand\s*theft\s*auto|vice\s*city|leonida", re.IGNORECASE)  # wie publish/text
_GTA6_PRESENT_RE = re.compile(r"\bGTA 6\b|\bgrand theft auto (?:vi|6)\b", re.IGNORECASE)
_URL_RE = re.compile(r"^(?:https?://|www\.)", re.IGNORECASE)
# Zusatzinfos, die nicht unbeschriftet im Prompt landen dürfen (Verwechslung mit dem Creator) oder eine
# eigene Zeile bekommen
_EXTRA_SKIP = {"clip_creator_name", "author", "via", "broadcaster_login", "channel_slug", "game_name",
               "category_name", "category", "category_slug", "subreddit", "flair", "permalink", "domain"}


# =========================================================================== plan_clip


def plan_clip(llm: LLMClient, c: CandidateIn, analysis: MediaAnalysis, examples: list[PerformanceExample],
              settings: Settings, *, now: datetime | None = None,
              recent: list[tuple[str, str]] | None = None) -> EditPlan:
    """Baut den Prompt (Metadaten, Velocity, Transkript mit Zeitstempeln, Lautstärke-Peaks,
    Musik-Wahrscheinlichkeit, Layout-Vorschlag, Frames mit Zeitangabe, Performance-Beispiele TOP/FLOP,
    letzte Hooks/Titel), ruft llm.complete_json(SYSTEM_EDITOR, ..., EditPlan) und gibt sanitize_plan(...)
    zurück. `recent`: (hook, titel) unserer letzten Clips; None = aus der DB lesen (falls initialisiert)."""
    now = _utc(now or datetime.now(UTC))
    if recent is None:
        recent = _recent_posts()
    ctx = _clip_context(c, analysis, examples, settings, now, recent)
    blocks: list[dict[str, Any]] = [{"type": "text", "text": ctx}]

    frames = [f for f in _select_frames(analysis.frames, settings.config.llm.max_frames) if _frame_ok(f)]
    if frames:
        blocks.append({"type": "text", "text": f"STILL FRAMES ({len(frames)}, labeled with source time; the "
                                               "10x10 grid with 0.0-1.0 labels is drawn on top for reading "
                                               "normalized coordinates and is not part of the video):"})
    for f in frames:
        blocks.append({"type": "text", "text": f"Frame t={f.t:.2f}s (faces: {len(f.faces)})"})
        blocks.append({"type": "image", "path": str(grid_frame(Path(f.path)).resolve()), "t": f.t})
    blocks.append({"type": "text", "text": prompts.EDITOR_TASK})

    result = llm.complete_json(prompts.SYSTEM_EDITOR, blocks, EditPlan, max_tokens=MAX_OUTPUT_TOKENS)
    plan = result if isinstance(result, EditPlan) else EditPlan.model_validate(
        result.model_dump() if hasattr(result, "model_dump") else result)
    return sanitize_plan(plan, analysis, settings)


def _recent_posts(limit: int = MAX_RECENT_POSTS) -> list[tuple[str, str]]:
    """(hook, YouTube-Titel) unserer letzten Clips (ohne abgelehnte), neueste zuerst. Ohne initialisierte
    DB (Tests, Einzelaufruf) leer – der Prompt kommt dann ohne diesen Abschnitt aus."""
    try:
        from sqlmodel import col, select

        from wdvi import db
        from wdvi.models import Clip

        out: list[tuple[str, str]] = []
        with db.session_scope() as s:
            rows = s.exec(select(Clip).where(col(Clip.status) != "rejected")
                          .order_by(col(Clip.created_at).desc()).limit(limit)).all()
            for clip in rows:
                plan = clip.plan if isinstance(clip.plan, dict) else {}
                titles = clip.titles if isinstance(clip.titles, dict) else {}
                plan_titles = plan.get("titles") if isinstance(plan.get("titles"), dict) else {}
                hook = str(plan.get("hook_text") or "").strip()
                title = str(titles.get("youtube") or plan_titles.get("youtube") or "").strip()
                if hook or title:
                    out.append((hook, title))
        return out
    except Exception as e:  # optionaler Kontext – darf den Plan nie verhindern
        log.debug("editor: recent posts unavailable (%s)", e)
        return []


def _frame_ok(f: FrameSample) -> bool:
    if Path(f.path).is_file():
        return True
    log.warning("editor: frame %s missing – not sent to Claude", f.path)
    return False


def grid_frame(path: Path) -> Path:
    """Kopie des Frames mit dezentem 10x10-Koordinatengitter (Beschriftung 0.0–1.0 an den Rändern), damit
    Claude normalisierte Boxen (focus/annotations) genau ablesen kann. Das Original bleibt unverändert;
    die Kopie liegt in <frame-ordner>/_grid/. Bei Fehlern wird das Original zurückgegeben."""
    out = path.parent / GRID_DIR / (path.stem + ".jpg")
    try:
        if out.is_file() and out.stat().st_mtime >= path.stat().st_mtime:
            return out
        from PIL import Image, ImageDraw, ImageFont

        with Image.open(path) as im:
            base = im.convert("RGB")
        w, h = base.size
        if w < 32 or h < 32:
            return path
        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        line_w = max(1, round(min(w, h) / 400))
        for i in range(1, GRID_STEPS):
            x = round(w * i / GRID_STEPS)
            y = round(h * i / GRID_STEPS)
            strong = i == GRID_STEPS // 2
            color = (255, 255, 255, 120 if strong else 70)
            draw.line([(x, 0), (x, h)], fill=color, width=line_w)
            draw.line([(0, y), (w, y)], fill=color, width=line_w)
        size = max(9, round(min(w, h) / 32))
        try:
            font: Any = ImageFont.load_default(size=size)
        except TypeError:  # ältere Pillow-Versionen ohne skalierbare Standardschrift
            font = ImageFont.load_default()
        pad = max(1, size // 6)
        for i in range(GRID_STEPS + 1):
            label = f"{i / GRID_STEPS:.1f}"
            tw, th = _text_size(draw, label, font)
            # x-Beschriftung oben, y-Beschriftung links (Ecke 0.0 nur einmal)
            x = min(max(round(w * i / GRID_STEPS - tw / 2), 0), w - tw - 1)
            _label(draw, (x, pad), label, font, tw, th, pad)
            if i > 0:
                y = min(max(round(h * i / GRID_STEPS - th / 2), 0), h - th - 1 - pad)
                _label(draw, (pad, y), label, font, tw, th, pad)
        merged = Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.stem + ".tmp.jpg")
        merged.save(tmp, "JPEG", quality=90)
        tmp.replace(out)
        return out
    except Exception as e:  # Gitter ist nur eine Hilfe – Frame dann ohne Gitter schicken
        log.warning("editor: grid for frame %s failed (%s) – sending original", path, e)
        return path


def _text_size(draw: Any, text: str, font: Any) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return int(right - left), int(bottom - top)


def _label(draw: Any, xy: tuple[int, int], text: str, font: Any, tw: int, th: int, pad: int) -> None:
    x, y = xy
    draw.rectangle([x - pad, y - pad, x + tw + pad, y + th + pad], fill=(0, 0, 0, 140))
    left, top, _, _ = draw.textbbox((0, 0), text, font=font)
    draw.text((x - left, y - top), text, font=font, fill=(255, 255, 255, 230))


def _select_frames(frames: list[FrameSample], max_frames: int) -> list[FrameSample]:
    frames = sorted(frames, key=lambda f: f.t)
    if max_frames <= 0:
        return []
    if len(frames) <= max_frames:
        return frames
    # gleichmäßig verteilt, erster und letzter Frame bleiben
    if max_frames == 1:
        return [frames[len(frames) // 2]]
    step = (len(frames) - 1) / (max_frames - 1)
    idx = sorted({round(i * step) for i in range(max_frames)})
    return [frames[i] for i in idx]


def _clip_context(c: CandidateIn, a: MediaAnalysis, examples: list[PerformanceExample], settings: Settings,
                  now: datetime, recent: list[tuple[str, str]] | None = None) -> str:
    cfg = settings.config
    f = cfg.filters
    dur = a.info.duration
    lines: list[str] = ["CLIP CANDIDATE"]
    lines.append(f"- Source: {c.source.value} on {c.platform_label}")
    handle = f" (@{c.creator_handle})" if c.creator_handle else ""
    lines.append(f"- Creator: {c.creator_name}{handle}")
    category = _category_line(c, settings)
    if category:
        lines.append(category)
    via = (c.extra or {}).get("via")
    if c.source == SourceKind.reddit_post and isinstance(via, str) and via.strip():
        lines.append(f"- Shared on Reddit by: {_one_line(via, 60)} (not the creator)")
    lines.append(f'- Original title: "{_one_line(c.title, 300)}"')
    lines.append(_popularity_line(c, now))
    eng = {k: v for k, v in (c.engagement or {}).items() if isinstance(v, int | float)}
    if eng:
        lines.append("- Engagement: " + ", ".join(f"{k}={_metric(v)}" for k, v in sorted(eng.items())))
    extras = _scalar_extras(c.extra)
    if extras:
        lines.append("- Extra: " + ", ".join(f"{k}={v}" for k, v in extras))
    if c.source == SourceKind.youtube_moment and c.start_s is not None:
        lines.append(f"- This clip is a moment cut from a longer video (original position {c.start_s:.1f}-"
                     f"{(c.end_s or 0):.1f} s). All timestamps below are relative to this clip.")
    lines.append(f"- Current date (UTC): {now.date().isoformat()} ({_release_note(now, _release_date(settings))})")

    lines += ["", "HARD LIMITS (enforced by code - anything outside is clipped or rejected)"]
    lines.append(f"- Source clip duration: {dur:.2f} s -> every timestamp must be within 0.00-{dur:.2f}")
    lo, hi = _target_range(settings)
    lines.append(f"- Final length after trim and cuts: min {f.min_output_duration_s:.1f} s, max "
                 f"{f.max_output_duration_s:.1f} s (target {lo:g}-{hi:g} s)")
    lines.append(f"- Shots: each at least {MIN_SHOT_S:.1f} s of kept time, gap-free from trim_start to trim_end, "
                 f"zoom {SHOT_ZOOM_MIN:.1f}-{SHOT_ZOOM_MAX:.1f}, glitch at most {MAX_GLITCH}x, max {MAX_SHOTS} shots")
    lines.append(f"- Zooms: max {MAX_ZOOMS}, no overlaps, scale 1.15-1.35 (hard range 1.0-1.5)")
    lines.append(f"- Emojis: 1-{MAX_EMOJIS}, at most one per word; sound effects: 0-{MAX_SFX} in total, the "
                 f"automatic whoosh of each zoom included; shake max {MAX_SHAKES}, flash max {MAX_FLASHES}")
    lines.append(f"- Annotations: max {MAX_ANNOTATIONS}, each at least {MIN_ANNOTATION_S:.1f} s, boxes within "
                 f"0-1, labels max {LABEL_MAX_WORDS} words")
    lines.append(f"- hook_text: max {HOOK_MAX_WORDS} words / {HOOK_MAX_CHARS} characters (may be empty with "
                 "hook_style none); highlight_words: 2-6")
    lines.append(f"- word_colors: max {MAX_WORD_COLORS}; outro_text: max {OUTRO_MAX_WORDS} words / "
                 f"{OUTRO_MAX_CHARS} characters")
    lines.append(f"- Hashtags: 3-{PROMPT_HASHTAGS}, without '#', always include gta6; tags: 8-{MAX_TAGS}, without '#'")
    lines.append(f"- YouTube title: max {YT_TITLE_MAX} characters (ideal <= 70), contains \"GTA 6\"")
    if f.leak_policy == "reject":
        lines.append("- Leaked/unreleased GTA 6 footage: always rejected")
    else:
        lines.append("- Leaked/unreleased GTA 6 footage: allowed by channel policy, but flag it honestly")
    if f.music_policy == "reject":
        lines.append(f"- Background music: clips with music_prob >= {f.music_threshold:.2f} are rejected by policy")
    else:
        lines.append("- Background music: allowed by policy, but mention the copyright risk")

    lines += ["", "ANALYSIS"]
    info = a.info
    lines.append(f"- Video: {info.width}x{info.height} @ {info.fps:.1f} fps, duration {info.duration:.2f} s, "
                 f"audio: {'yes' if info.has_audio else 'no'}")
    lines.append("- Suggested layout: " + _layout_text(a))
    lines.append("- Shot kinds available: " + _shot_kinds_text(a))
    lines.append("- Music probability: " + ("not measured" if a.music_prob is None else f"{a.music_prob:.2f}"))
    lines.append(f"- Speech ratio: {a.speech_ratio * 100:.0f} %")
    peaks = sorted(p for p in a.loudness_peaks if _finite(p) is not None)
    lines.append("- Loudness peaks (source s; likely action, impact or reaction moments - never cut right "
                 "before them): " + (", ".join(f"{p:.2f}" for p in peaks[:20]) or "none"))
    if a.transcript is not None:
        lines.append(f"- Transcript language: {a.transcript.language} (p={a.transcript.language_prob:.2f})")

    lines += ["", _transcript_text(a.transcript)]

    if examples:
        ranked = sorted(examples, key=lambda e: e.views, reverse=True)
        n_top = (len(ranked) + 1) // 2
        lines += ["", "OUR PAST POSTS (views after N hours; TOP = our best performers, FLOP = our weakest - "
                      "learn what works for this audience)"]
        for i, ex in enumerate(ranked):
            lines.append(f'- {"TOP" if i < n_top else "FLOP"}: {_num(ex.views)} views ({ex.platform}, '
                         f'{ex.hours_after} h): title "{_one_line(ex.title, 120)}" | hook '
                         f'"{_one_line(ex.hook_text, 60)}"')
    if recent:
        lines += ["", "RECENT POSTS (our latest hooks and titles - never reuse or closely imitate them)"]
        for hook, title in recent[:MAX_RECENT_POSTS]:
            lines.append(f'- hook "{_one_line(hook, 60)}" | title "{_one_line(title, 120)}"')
    return "\n".join(lines)


def _category_line(c: CandidateIn, settings: Settings) -> str | None:
    """Plattform-Kategorie (stärkster Hinweis GTA 6 vs. GTA V) als eigene, beschriftete Zeile."""
    extra = c.extra or {}
    for key in ("game_name", "category_name"):
        v = extra.get(key)
        if isinstance(v, str) and v.strip():
            return f"- Platform category: {_one_line(v, 80)}"
    slug = extra.get("category_slug")
    if isinstance(slug, str) and slug.strip():
        return f"- Platform category: {_one_line(slug, 80)} (Kick category slug)"
    if c.source == SourceKind.twitch_clip and extra.get("game_id") and not extra.get("manual"):
        # die Twitch-Quelle holt Clips nur aus den konfigurierten Spielkategorien (manuelle URLs: unbekannt)
        names = [n.strip() for n in settings.config.sources.twitch.game_names
                 if n and n.strip() and not n.strip().isdigit()]
        if len(names) == 1:
            return f"- Platform category: {names[0]} (Twitch game category)"
        if names:
            return f"- Platform category: one of {', '.join(names)} (Twitch game categories)"
    if c.source == SourceKind.reddit_post:
        sub = extra.get("subreddit")
        if isinstance(sub, str) and sub.strip():
            flair = extra.get("flair")
            tail = f" (flair: {_one_line(flair, 40)})" if isinstance(flair, str) and flair.strip() else ""
            return f"- Subreddit: r/{sub.strip()}{tail}"
    return None


def _popularity_line(c: CandidateIn, now: datetime) -> str:
    hours = _age_hours(c.created_at, now)
    if c.source == SourceKind.reddit_post:
        score = _finite((c.engagement or {}).get("score"))
        if score is not None:
            return f"- Reddit score: {_num(score)} | Age: {hours:.1f} h | Velocity: {_num(score / hours)} upvotes/h"
    if c.view_count is not None:
        return (f"- Views: {_num(c.view_count)} | Age: {hours:.1f} h | Velocity: "
                f"{_num(c.view_count / hours)} views/h")
    return f"- Views: unknown | Age: {hours:.1f} h"


def _release_date(settings: Settings) -> date:
    """Release-Termin: config `filters.gta6_release_date` (falls gesetzt), sonst prompts-Standard."""
    v = getattr(settings.config.filters, "gta6_release_date", None)
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return date.fromisoformat(v.strip())
        except ValueError:
            log.warning("editor: invalid filters.gta6_release_date %r – using %s", v, prompts.GTA6_RELEASE_DATE)
    return prompts.GTA6_RELEASE_DATE


def _release_note(now: datetime, release: date = prompts.GTA6_RELEASE_DATE) -> str:
    days = (release - now.date()).days
    if days > 0:
        return f"GTA VI is planned to release in {days} days, on {release.isoformat()}"
    if days == 0:
        return f"GTA VI releases today ({release.isoformat()})"
    return f"GTA VI was released {-days} days ago, on {release.isoformat()}"


def _layout_text(a: MediaAnalysis) -> str:
    lay = a.layout
    s = f"{lay.kind} (confidence {lay.confidence:.2f})"
    if lay.facecam_box is not None:
        b = lay.facecam_box
        s += f"; facecam box x={b.x:.2f} y={b.y:.2f} w={b.w:.2f} h={b.h:.2f} (normalized)"
    if lay.gameplay_box is not None:
        b = lay.gameplay_box
        s += f"; gameplay box x={b.x:.2f} y={b.y:.2f} w={b.w:.2f} h={b.h:.2f}"
    if lay.track:
        s += f"; face track with {len(lay.track)} points"
    return s


def _target_range(settings: Settings) -> tuple[float, float]:
    """Ziel-Länge (filters.target_output_min_s/max_s), innerhalb der harten Grenzen."""
    f = settings.config.filters
    lo = _finite(getattr(f, "target_output_min_s", None), 12.0) or 12.0
    hi = _finite(getattr(f, "target_output_max_s", None), 25.0) or 25.0
    lo = max(lo, f.min_output_duration_s)
    hi = min(max(hi, lo), f.max_output_duration_s)
    return lo, hi


def available_shot_kinds(a: MediaAnalysis) -> list[str]:
    """face braucht eine Facecam-Box oder Gesichtserkennungen, split eine Facecam-Box (ohne sie rendert der
    Renderer split als fit); gameplay/fit gehen immer."""
    kinds = ["gameplay", "fit"]
    if a.layout.facecam_box is not None:
        kinds = ["split"] + kinds
    if _has_face_info(a):
        kinds = ["face"] + kinds
    return kinds


def _has_face_info(a: MediaAnalysis) -> bool:
    return a.layout.facecam_box is not None or bool(a.layout.track) or any(fr.faces for fr in a.frames)


def _shot_kinds_text(a: MediaAnalysis) -> str:
    kinds = available_shot_kinds(a)
    s = ", ".join(kinds)
    if "face" not in kinds:
        return s + " (no facecam or face detected - face and split are not possible)"
    if a.layout.facecam_box is not None:
        return s + " (face and split use the facecam box)"
    return s + " (face uses the detected face; no facecam overlay - split is not possible)"


def _transcript_text(tr: Transcript | None) -> str:
    if tr is None or not tr.segments:
        return "TRANSCRIPT: none (no speech detected or transcription unavailable)"
    words = tr.words()
    if words and len(words) <= MAX_TRANSCRIPT_WORDS:
        out = ["TRANSCRIPT (word level, source seconds; '?' marks uncertain words)"]
        for w in words:
            mark = "?" if w.prob < 0.5 else ""
            out.append(f"[{w.start:.2f}-{w.end:.2f}] {w.text.strip()}{mark}")
        return "\n".join(out)
    # zu lang oder ohne Wortzeiten: Segment-Ebene
    out = ["TRANSCRIPT (segment level, source seconds)"]
    used = 0
    for seg in tr.segments:
        line = f"[{seg.start:.2f}-{seg.end:.2f}] {_one_line(seg.text, 1000)}"
        used += len(line)
        if used > MAX_SEGMENT_CHARS:
            out.append("[... transcript truncated ...]")
            break
        out.append(line)
    return "\n".join(out)


# =========================================================================== sanitize_plan


def sanitize_plan(plan: EditPlan, analysis: MediaAnalysis, settings: Settings) -> EditPlan:
    """Erzwingt harte Regeln, egal was Claude sagt:
    - trim in [0, duration], trim_end > trim_start; remove_ranges innerhalb trim, sortiert, ohne Überlappung,
      jede Lücke >= 0.25 s; behaltene Gesamtdauer <= max_output_duration_s (sonst trim_end kürzen)
    - zu kurz (< min_output_duration_s) -> keep=False
    - zooms/emphasis auf trim begrenzen, scale in [1.0, 1.5], max 1 Emoji pro Wort
    - zooms max 3; emojis max 4; SFX max 4 inkl. des automatischen Zoom-whoosh, eigener whoosh kurz nach
      einem Zoom-Start fällt weg
    - gesprochene Slurs im behaltenen Teil werden herausgeschnitten (Untertitel zeigen jedes Wort);
      klappt das nicht oder steht ein Slur in Hook/Titel/Beschreibung -> keep=False
    - is_leak_or_unreleased & leak_policy=reject -> keep=False; is_unsafe -> keep=False
    - virality_score < min_virality_score -> keep=False
    - Titel: YouTube <= 100 Zeichen und (bei GTA-Inhalten) mit "GTA 6", keine "#shorts"-Duplikate, keine @-Erwähnungen;
      Hashtags ohne '#', dedupliziert, max 8
    - hook_text max 6 Wörter / 32 Zeichen (bei hook_style "none" darf er leer sein; ohne Sprache am Anfang
      wird "none" zu "banner")
    - v2: shots sortiert, auf den Trim begrenzt, lückenlos ab trim_start, ohne Überlappung, min 0.6 s
      behaltene Zeit, face/split ohne Gesichtsinfo bzw. split ohne Facecam-Box -> fit, zoom 1.0–1.6;
      annotations auf den Trim begrenzt, Boxen in [0, 1] mit Mindestgröße; word_colors dedupliziert (max 12); outro_text max 8 Wörter /
      48 Zeichen; debate_score 0–100; tags dedupliziert (max 15, ohne '#')"""
    f = settings.config.filters
    p = plan.model_copy(deep=True)
    words = analysis.transcript.words() if analysis.transcript is not None else []

    # ---- Slurs im Ton -> Wort herausschneiden (vor der Zeit-Normalisierung, damit es mitgeprüft wird)
    slurs = [w for w in words if _SLUR_RE.search(w.text or "")
             and _finite(w.start) is not None and _finite(w.end) is not None]
    if slurs:
        cuts = [_slur_cut(w) for w in slurs]
        ts0, te0 = _finite(p.trim_start), _finite(p.trim_end)
        if ts0 is not None and te0 is not None and ts0 < te0:
            for a, b in cuts:  # Wort auf der Trim-Kante: Kante verschieben statt (zu kurzem) Schnitt
                if a < te0 < b:
                    te0 = a
                if a < ts0 < b:
                    ts0 = b
            p.trim_start, p.trim_end = ts0, te0
        p.remove_ranges = list(p.remove_ranges or []) + cuts

    # ---- Zeiten
    dur = _finite(analysis.info.duration)
    if dur is None or dur <= 0:
        dur = max(_finite(p.trim_end) or 0.0, 0.0)
        _reject(p, "Quelldauer unbekannt – Clip kann nicht sicher geschnitten werden")
    ts, te, ranges = _sanitize_times(p, dur, f.max_output_duration_s, words)
    p.trim_start, p.trim_end = round(ts, 3), round(te, 3)
    p.remove_ranges = [(round(a, 3), round(b, 3)) for a, b in ranges]
    kept = _kept_duration(ts, te, ranges)
    slur_left = [w for w in slurs if _kept_overlap(max(w.start, ts), min(w.end, te), ranges) > 0.02]
    if slurs:
        log.info("editor: %d slur word(s) in transcript, %d left in the kept part", len(slurs), len(slur_left))

    p.zooms = _sanitize_zooms(p.zooms, ts, te, ranges, _has_face(analysis))
    # ---- Edit-Engine v2: Shots/Annotationen (leer bleibt leer = v1)
    p.shots = _sanitize_shots(p.shots, ts, te, ranges, _has_face_info(analysis),
                              split_ok=analysis.layout.facecam_box is not None)
    p.annotations = _sanitize_annotations(p.annotations, ts, te, ranges)
    whoosh_starts = [sh.start for sh in p.shots if sh.transition in ("punch", "glitch")]
    p.emphasis = _sanitize_emphasis(p.emphasis, ts, te, ranges, words, p.zooms, whoosh_starts)

    # ---- Layout nur übernehmen, wenn die Analyse es trägt
    p.layout = _sanitize_layout(p.layout, analysis)

    # ---- Texte
    p.virality_score = int(round(min(max(_finite(p.virality_score) or 0.0, 0.0), 100.0)))
    p.reasoning = _one_line(p.reasoning, 800)
    lang = (p.content_language or "").strip().lower()
    if not re.fullmatch(r"[a-z]{2,3}", lang):
        lang = analysis.transcript.language if analysis.transcript is not None else "en"
    p.content_language = lang or "en"

    moved_tags: list[str] = []
    yt_title = _clean_title(p.titles.youtube, YT_TITLE_MAX)
    if not yt_title:
        yt_title = _clean_title(p.hook_text, YT_TITLE_MAX) or "Wild GTA 6 moment"
    tiktok = _clean_title(p.titles.tiktok, SHORT_TITLE_MAX) or _clean_title(yt_title, SHORT_TITLE_MAX)
    instagram = _clean_title(p.titles.instagram, SHORT_TITLE_MAX) or _clean_title(yt_title, SHORT_TITLE_MAX)
    # YouTube-Suche läuft über den Titel -> "GTA 6" muss drinstehen (TikTok/IG: Hashtag gta6 reicht)
    # Suchwort "GTA 6" nur bei GTA-Inhalten erzwingen (gleiche Regel wie #GTA6 in publish/text.py)
    gta = _is_gta_plan(p, yt_title)
    p.titles = PlatformText(youtube=_with_gta6(yt_title, YT_TITLE_MAX) if gta else yt_title, tiktok=tiktok,
                            instagram=instagram)
    descs = {}
    for platform in ("youtube", "tiktok", "instagram"):
        body, tags = _clean_description(getattr(p.descriptions, platform), DESCRIPTION_MAX[platform])
        descs[platform] = body
        moved_tags += tags
    p.descriptions = PlatformText(**descs)
    p.hashtags = [h for h in _clean_hashtags(list(p.hashtags) + moved_tags, gta=gta) if not _SLUR_RE.search(h)]
    p.hook_style, p.hook_text = _sanitize_hook(p.hook_style, p.hook_text, yt_title, words, ts, ranges)
    p.highlight_words = [w for w in _clean_highlights(p.highlight_words) if not _SLUR_RE.search(w)]
    p.word_colors = _clean_word_colors(p.word_colors)
    p.outro_text = _clean_outro(p.outro_text)
    p.debate_score = int(round(_clamp(_finite(p.debate_score, 0.0), 0.0, 100.0)))
    p.tags = _clean_tags(p.tags, gta=gta)

    # ---- harte Ablehnungen (erster Grund gewinnt; Claudes eigener Grund bleibt erhalten)
    texts = [p.hook_text, p.titles.youtube, p.titles.tiktok, p.titles.instagram, p.descriptions.youtube,
             p.descriptions.tiktok, p.descriptions.instagram]
    if any(_SLUR_RE.search(t or "") for t in texts):
        _reject(p, "Beleidigendes Wort (Slur) in Hook, Titel oder Beschreibung")
    if _SLUR_RE.search(p.outro_text or ""):
        _reject(p, "Beleidigendes Wort (Slur) in der Schlussfrage (outro_text)")
    if slur_left:
        _reject(p, "Beleidigendes Wort (Slur) im behaltenen Teil – die Untertitel würden es zeigen")
    if p.is_unsafe:
        _reject(p, "Unsicherer Inhalt (NSFW/Hass/Gewalt/Selbstverletzung/Doxxing/Minderjährige)")
    if p.is_leak_or_unreleased and f.leak_policy == "reject":
        _reject(p, "Geleaktes bzw. unveröffentlichtes GTA-6-Material")
    if p.virality_score < f.min_virality_score:
        _reject(p, f"Virality-Score {p.virality_score} unter Minimum {f.min_virality_score}")
    if kept < f.min_output_duration_s:
        _reject(p, f"Zu kurz nach dem Schnitt ({kept:.1f} s < {f.min_output_duration_s:.1f} s)")

    if p.keep:
        p.reject_reason = None
    else:
        p.reject_reason = _one_line(p.reject_reason or "Von Claude abgelehnt", 300)
    return p


def _slur_cut(w: Word) -> tuple[float, float]:
    """Schnittbereich, der das ganze Wort sicher abdeckt (mind. SLUR_MIN_CUT_S, sonst verwirft
    _sanitize_times ihn als zu kurz)."""
    a = float(w.start) - SLUR_PAD_S
    b = max(float(w.end) + SLUR_PAD_S, a + SLUR_MIN_CUT_S)
    return (a, b)


def _reject(p: EditPlan, reason: str) -> None:
    if p.keep:
        p.keep = False
        p.reject_reason = reason
    elif not (p.reject_reason or "").strip():
        p.reject_reason = reason


def _sanitize_times(p: EditPlan, dur: float, max_out: float,
                    words: list[Word]) -> tuple[float, float, list[tuple[float, float]]]:
    ts = _clamp(_finite(p.trim_start, 0.0), 0.0, dur)
    te = _clamp(_finite(p.trim_end, dur), 0.0, dur)
    if te < ts:
        ts, te = te, ts
    if te - ts < 0.1:  # unbrauchbarer Trim -> ganzer Clip (Länge wird unten geprüft)
        ts, te = 0.0, dur

    ranges: list[tuple[float, float]] = []
    for r in p.remove_ranges or []:
        try:
            a, b = _finite(r[0]), _finite(r[1])
        except (TypeError, IndexError):
            continue
        if a is None or b is None:
            continue
        if b < a:
            a, b = b, a
        a, b = max(a, ts), min(b, te)
        if b - a >= MIN_REMOVE_S:
            ranges.append((a, b))
    ranges = _merge_ranges(ranges)
    ts, te, ranges = _absorb_edges(ts, te, ranges)

    if _kept_duration(ts, te, ranges) > max_out:
        te = _cut_point(ts, te, ranges, max_out)
        # nicht mitten im Wort enden
        for w in words:
            if w.start < te < w.end and te - w.start < 1.0 and w.start > ts:
                te = w.start
                break
        ranges = [(a, min(b, te)) for a, b in ranges if a < te]
        ranges = [(a, b) for a, b in ranges if b - a >= MIN_REMOVE_S]
        ts, te, ranges = _absorb_edges(ts, te, ranges)
    return ts, te, ranges


def _merge_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for a, b in sorted(ranges):
        if merged and a - merged[-1][1] < MIN_KEEP_S:  # überlappend oder winziges Reststück dazwischen
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _absorb_edges(ts: float, te: float,
                  ranges: list[tuple[float, float]]) -> tuple[float, float, list[tuple[float, float]]]:
    """Schnitte am Anfang/Ende des Trims werden zum Trim (keine Mini-Stücke am Rand)."""
    ranges = list(ranges)
    while ranges and ranges[0][0] - ts < MIN_KEEP_S:
        ts = max(ts, ranges.pop(0)[1])
    while ranges and te - ranges[-1][1] < MIN_KEEP_S:
        te = min(te, ranges.pop()[0])
    if te < ts:
        te = ts
    return ts, te, ranges


def _kept_duration(ts: float, te: float, ranges: list[tuple[float, float]]) -> float:
    return max(0.0, (te - ts) - sum(b - a for a, b in ranges))


def _cut_point(ts: float, te: float, ranges: list[tuple[float, float]], max_out: float) -> float:
    """Quellzeit, an der die behaltene Dauer max_out erreicht."""
    acc, s = 0.0, ts
    for a, b in list(ranges) + [(te, te)]:
        seg = a - s
        if acc + seg >= max_out:
            return s + (max_out - acc)
        acc += seg
        s = b
    return te


def _in_ranges(t: float, ranges: list[tuple[float, float]]) -> bool:
    return any(a < t < b for a, b in ranges)


def _kept_overlap(s: float, e: float, ranges: list[tuple[float, float]]) -> float:
    cut = sum(max(0.0, min(e, b) - max(s, a)) for a, b in ranges)
    return max(0.0, (e - s) - cut)


def _has_face(a: MediaAnalysis) -> bool:
    return (a.layout.kind in ("facecam_split", "face_track") or a.layout.facecam_box is not None
            or bool(a.layout.track) or any(fr.faces for fr in a.frames))


def _sanitize_layout(layout: str | None, a: MediaAnalysis) -> Any:
    if layout is None:
        return None
    if layout == "facecam_split" and a.layout.facecam_box is None:
        return None
    if layout == "face_track" and not a.layout.track and not any(fr.faces for fr in a.frames):
        return None
    is_vertical = a.info.height > a.info.width
    if layout == "vertical_native" and not is_vertical:
        return None
    if layout != "vertical_native" and is_vertical and a.layout.kind == "vertical_native":
        return None
    return layout


def _sanitize_zooms(zooms: list[Zoom], ts: float, te: float, ranges: list[tuple[float, float]],
                    has_face: bool) -> list[Zoom]:
    out: list[Zoom] = []
    for z in sorted(zooms, key=lambda z: _finite(z.start, 0.0)):
        s, e = _finite(z.start), _finite(z.end)
        if s is None or e is None:
            continue
        if e < s:
            s, e = e, s
        s, e = max(s, ts), min(e, te)
        if e - s < MIN_ZOOM_S or _kept_overlap(s, e, ranges) < MIN_ZOOM_S:
            continue
        if out and s < out[-1].end:  # überlappende Zooms verwerfen
            continue
        scale = _clamp(_finite(z.scale, 1.2), 1.0, 1.5)
        focus = z.focus if (z.focus != "face" or has_face) else "center"
        out.append(Zoom(start=round(s, 3), end=round(e, 3), scale=round(scale, 3), focus=focus))
        if len(out) >= MAX_ZOOMS:
            break
    return out


def _word_index(t: float, words: list[Word]) -> int | None:
    for i, w in enumerate(words):
        if w.start - 0.05 <= t <= w.end + 0.05:
            return i
    return None


def _sanitize_emphasis(items: list[Emphasis], ts: float, te: float, ranges: list[tuple[float, float]],
                       words: list[Word], zooms: list[Zoom] | None = None,
                       transition_whooshes: list[float] | None = None) -> list[Emphasis]:
    """transition_whooshes: Quellzeiten von punch/glitch-Übergängen (der Renderer spielt dort automatisch
    einen whoosh; zählt nicht ins SFX-Budget, eigener whoosh direkt danach fällt aber weg)."""
    out: list[Emphasis] = []
    used_words: set[int] = set()
    emoji_times: list[float] = []
    zoom_starts = [z.start for z in zooms or []]
    n_sfx = len(zoom_starts)  # jeder Zoom bekommt im Renderer automatisch einen whoosh
    auto_whooshes = zoom_starts + list(transition_whooshes or [])
    n_shake = n_flash = 0
    for e in sorted(items, key=lambda e: _finite(e.t, -1.0)):
        t = _finite(e.t)
        if t is None or t < ts or t > te or _in_ranges(t, ranges):
            continue
        emoji = _first_emoji(e.emoji)
        if emoji:
            wi = _word_index(t, words)
            clash = (wi is not None and wi in used_words) or any(abs(t - x) < 0.25 for x in emoji_times)
            if clash or len(emoji_times) >= MAX_EMOJIS:
                emoji = None
            else:
                emoji_times.append(t)
                if wi is not None:
                    used_words.add(wi)
        sfx = e.sfx
        merged = False
        if sfx == "whoosh" and auto_whooshes:
            gap = min(abs(t - zs) for zs in auto_whooshes)
            if gap < ZOOM_WHOOSH_MERGE_S:
                merged = True  # verschmilzt im Renderer mit dem Zoom-whoosh, zählt nicht extra
            elif gap < ZOOM_WHOOSH_GAP_S:
                sfx = None  # zweiter whoosh kurz nach dem automatischen
        if sfx is not None and not merged:
            if n_sfx >= MAX_SFX:
                sfx = None
            else:
                n_sfx += 1
        shake = bool(e.shake) and n_shake < MAX_SHAKES
        flash = bool(e.flash) and n_flash < MAX_FLASHES
        n_shake += shake
        n_flash += flash
        if emoji is None and sfx is None and not shake and not flash:
            continue
        out.append(Emphasis(t=round(t, 3), emoji=emoji, sfx=sfx, shake=shake, flash=flash))
    return out


# ---- Edit-Engine v2


def _clean_box(b: Box | None, min_size: float = MIN_BOX) -> Box | None:
    """Box auf [0, 1] begrenzen (Schnittmenge mit dem Bild), negative Breite/Höhe umdrehen, zu kleine Boxen
    um ihre Mitte auf min_size vergrößern. Box komplett außerhalb oder ungültig -> None."""
    if b is None:
        return None
    x, y, w, h = _finite(b.x), _finite(b.y), _finite(b.w), _finite(b.h)
    if x is None or y is None or w is None or h is None:
        return None
    if w < 0:
        x, w = x + w, -w
    if h < 0:
        y, h = y + h, -h
    x0, y0, x1, y1 = max(x, 0.0), max(y, 0.0), min(x + w, 1.0), min(y + h, 1.0)
    if x1 < x0 or y1 < y0 or (x1 - x0 == 0 and w > 0) or (y1 - y0 == 0 and h > 0):
        return None  # liegt komplett außerhalb des Bildes
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    w2, h2 = max(x1 - x0, min_size), max(y1 - y0, min_size)
    nx = _clamp(cx - w2 / 2, 0.0, 1.0 - w2)
    ny = _clamp(cy - h2 / 2, 0.0, 1.0 - h2)
    return Box(x=round(nx, 4), y=round(ny, 4), w=round(w2, 4), h=round(h2, 4))


def _same_framing(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return a["kind"] == b["kind"] and a["zoom"] == b["zoom"] and a["focus"] == b["focus"]


def _sanitize_shots(shots: list[Shot], ts: float, te: float, ranges: list[tuple[float, float]],
                    face_ok: bool, split_ok: bool | None = None) -> list[Shot]:
    """Shots lückenlos über [ts, te]: sortiert, Überlappungen entfernt, Lücken geschlossen (der vorige Shot
    läuft bis zum nächsten), zu kurze Shots (behaltene Zeit < MIN_SHOT_S) gehen im Nachbarn auf."""
    if not shots or te - ts <= 0:
        return []
    if split_ok is None:
        split_ok = face_ok
    items: list[dict[str, Any]] = []
    for sh in shots:
        s, e = _finite(sh.start), _finite(sh.end)
        if s is None or e is None:
            continue
        if e < s:
            s, e = e, s
        s, e = max(s, ts), min(e, te)
        if e - s <= 1e-6:
            continue
        kind = sh.kind if (face_ok or sh.kind not in ("face", "split")) else "fit"
        if kind == "split" and not split_ok:
            kind = "fit"  # split braucht eine Facecam-Box (Renderer zeigt sonst ohnehin fit)
        zoom = round(_clamp(_finite(sh.zoom, 1.0), SHOT_ZOOM_MIN, SHOT_ZOOM_MAX), 3)
        items.append({"start": s, "end": e, "kind": kind, "focus": _clean_box(sh.focus), "zoom": zoom,
                      "transition": sh.transition})
    items.sort(key=lambda d: (d["start"], d["end"]))
    seq: list[dict[str, Any]] = []
    for it in items:  # Überlappung: späterer Shot beginnt erst am Ende des vorigen
        if seq and it["start"] < seq[-1]["end"]:
            it["start"] = seq[-1]["end"]
        if it["end"] - it["start"] > 1e-6:
            seq.append(it)
    if not seq:
        return []
    seq[0]["start"] = ts
    for a, b in zip(seq, seq[1:], strict=False):
        a["end"] = b["start"]
    seq[-1]["end"] = te

    merged: list[dict[str, Any]] = []
    for it in seq:  # zu kurz oder gleiche Einstellung per harter Schnitt -> im vorigen Shot aufgehen
        if merged and (_kept_overlap(it["start"], it["end"], ranges) < MIN_SHOT_S
                       or (_same_framing(merged[-1], it) and it["transition"] == "cut")):
            merged[-1]["end"] = it["end"]
            continue
        merged.append(it)
    if len(merged) > 1 and _kept_overlap(merged[0]["start"], merged[0]["end"], ranges) < MIN_SHOT_S:
        # erster Shot = stärkstes Bild (meist das Gesicht): lieber verlängern als im Nachfolger aufgehen
        t = _cut_point(merged[0]["start"], te, ranges, MIN_SHOT_S)
        if _kept_overlap(t, merged[1]["end"], ranges) >= MIN_SHOT_S:
            merged[0]["end"] = merged[1]["start"] = t
    if len(merged) > 1 and _kept_overlap(merged[0]["start"], merged[0]["end"], ranges) < MIN_SHOT_S:
        merged[1]["start"] = merged[0]["start"]
        merged.pop(0)
    if len(merged) > MAX_SHOTS:
        merged = merged[:MAX_SHOTS]
        merged[-1]["end"] = te

    out: list[Shot] = []
    n_glitch = 0
    bounds = [round(m["start"], 3) for m in merged] + [round(te, 3)]
    for i, m in enumerate(merged):
        tr = m["transition"] if i > 0 else "cut"  # erster Frame ohne Übergang (stärkstes Bild sofort)
        if tr == "glitch":
            n_glitch += 1
            if n_glitch > MAX_GLITCH:
                tr = "cut"
        out.append(Shot(start=bounds[i], end=bounds[i + 1], kind=m["kind"], focus=m["focus"], zoom=m["zoom"],
                        transition=tr))
    return out


def _sanitize_annotations(items: list[Annotation], ts: float, te: float,
                          ranges: list[tuple[float, float]]) -> list[Annotation]:
    out: list[Annotation] = []
    for a in sorted(items, key=lambda a: _finite(a.start, 0.0)):
        s, e = _finite(a.start), _finite(a.end)
        if s is None or e is None:
            continue
        if e < s:
            s, e = e, s
        s, e = max(s, ts), min(e, te)
        if e - s < MIN_ANNOTATION_S or _kept_overlap(s, e, ranges) < MIN_ANNOTATION_S:
            continue
        box = _clean_box(a.box)
        if box is None:
            continue
        out.append(Annotation(start=round(s, 3), end=round(e, 3), kind=a.kind, box=box, color=a.color,
                              label=_clean_label(a.label)))
        if len(out) >= MAX_ANNOTATIONS:
            break
    return out


def _clean_label(text: str | None) -> str | None:
    t = _strip_symbols(_MENTION_RE.sub("", text or ""))
    t = re.sub(r"\s+", " ", t).strip().strip('"“”\'')
    if not t or _SLUR_RE.search(t):
        return None
    words = t.split()[:LABEL_MAX_WORDS]
    while len(words) > 1 and len(" ".join(words)) > LABEL_MAX_CHARS:
        words.pop()
    return " ".join(words)[:LABEL_MAX_CHARS].rstrip(" ,;:-–—") or None


def _color_key(word: str) -> str:
    """Vergleichsform wie im Caption-Renderer: klein, ohne Leer-/Satzzeichen ("GTA 6" == "gta6")."""
    return re.sub(r"[\W_]", "", word.lower())


def _clean_word_colors(items: list[WordColor]) -> list[WordColor]:
    out: list[WordColor] = []
    seen: set[str] = set()
    for wc in items:
        word = re.sub(r"\s+", " ", _strip_symbols(str(wc.word or ""))).strip().strip(".,!?;:\"'()[]{}…“”‘’*")
        if not word or len(word) > WORD_COLOR_MAX_CHARS or len(word.split()) > 4 or _SLUR_RE.search(word):
            continue
        key = _color_key(word)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(WordColor(word=word, color=wc.color))
        if len(out) >= MAX_WORD_COLORS:
            break
    return out


def _clean_outro(text: str | None) -> str | None:
    """Schlussfrage: eine Zeile, ohne Emojis/Hashtags/@, max OUTRO_MAX_WORDS Wörter / OUTRO_MAX_CHARS Zeichen."""
    raw = text or ""
    t = re.sub(r"(?<![\w&])#\w+", "", _MENTION_RE.sub("", raw))
    t = _GTA6_VARIANT_RE.sub("GTA 6", _strip_symbols(t))
    t = re.sub(r"\s+", " ", t).strip().strip('"“”\'')
    words = t.split()
    cut = words[:OUTRO_MAX_WORDS]
    while len(cut) > 1 and len(" ".join(cut)) > OUTRO_MAX_CHARS:
        cut.pop()
    out = " ".join(cut)[:OUTRO_MAX_CHARS].rstrip(" ,;:-–—")
    if out and out != t and "?" in t and not out.endswith("?"):  # gekürzte Frage bleibt eine Frage
        out = (out if len(out) < OUTRO_MAX_CHARS else out[:-1].rstrip()) + "?"
    return out or None


def _clean_tags(tags: list[str], gta: bool = True) -> list[str]:
    """YouTube-Tags: ohne '#', ohne Zeichen, die YouTube ablehnt (<, >, Komma, Anführungszeichen),
    dedupliziert, max MAX_TAGS und TAGS_TOTAL_MAX Zeichen insgesamt."""
    out: list[str] = []
    seen: set[str] = set()
    total = 0
    for tag in tags:
        t = _MENTION_RE.sub("", str(tag or "")).replace("#", " ")
        t = _strip_symbols(re.sub(r"[<>\",;|]", " ", t))
        t = re.sub(r"\s+", " ", t).strip(" -_.'")
        if not t or len(t) > TAG_MAX_CHARS or _URL_RE.match(t) or _SLUR_RE.search(t):
            continue
        key = t.lower()
        if key in seen:
            continue
        if total + len(t) > TAGS_TOTAL_MAX:
            break
        seen.add(key)
        total += len(t) + 1
        out.append(t)
        if len(out) >= MAX_TAGS:
            break
    if gta and out and not any(_GTA6_VARIANT_RE.search(t) or t.lower() in ("gta6", "gtavi") for t in out):
        out = ["gta 6"] + out[:MAX_TAGS - 1]  # wichtigstes Suchwort fehlt
    return out


def _speech_at_start(words: list[Word], ts: float, ranges: list[tuple[float, float]]) -> bool:
    """Wird kurz nach trim_start (behaltene Zeit) gesprochen?"""
    for w in words:
        s, e = _finite(w.start), _finite(w.end)
        if s is None or e is None or not (w.text or "").strip():
            continue
        if e > ts and s < ts + HOOK_SPOKEN_WINDOW_S and _kept_overlap(max(s, ts), e, ranges) > 0.05:
            return True
    return False


def _sanitize_hook(style: str, hook_text: str | None, title: str, words: list[Word], ts: float,
                   ranges: list[tuple[float, float]]) -> tuple[Any, str]:
    """hook_style "none" nur, wenn am Anfang wirklich gesprochen wird (sonst hätte der Clip gar keinen Hook);
    bei "banner" braucht es einen Hook-Text (Fallback: Titel)."""
    hook = _clean_hook(hook_text)
    if style == "none" and _speech_at_start(words, ts, ranges):
        return "none", hook
    return "banner", hook or _clean_hook(title)


# ---- Emoji / Text


def _is_emoji_char(ch: str) -> bool:
    o = ord(ch)
    return (o >= 0x1F000 and o <= 0x1FAFF) or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF \
        or 0x2190 <= o <= 0x21FF or 0x2300 <= o <= 0x23FF or o in (0x203C, 0x2049, 0x3030, 0x303D)


def _is_regional(ch: str) -> bool:
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def _first_emoji(s: str | None) -> str | None:
    """Erstes Emoji als Graphem (inkl. VS16, Hautton, ZWJ-Folgen, Flaggen) – sonst None."""
    if not s:
        return None
    chars = list(s.strip())
    i = next((k for k, ch in enumerate(chars) if _is_emoji_char(ch)), None)
    if i is None:
        return None
    out = [chars[i]]
    j = i + 1
    if _is_regional(chars[i]):
        if j < len(chars) and _is_regional(chars[j]):
            out.append(chars[j])
        return "".join(out)
    while j < len(chars):
        ch, o = chars[j], ord(chars[j])
        if ch in ("️", "⃣") or 0x1F3FB <= o <= 0x1F3FF or 0xE0020 <= o <= 0xE007F:
            out.append(ch)
            j += 1
        elif ch == "‍" and j + 1 < len(chars) and _is_emoji_char(chars[j + 1]):
            out += [ch, chars[j + 1]]
            j += 2
        else:
            break
    return "".join(out)


def _strip_symbols(text: str) -> str:
    """Entfernt Emojis/Symbole/Steuerzeichen (die Hook-Schrift hat dafür keine Glyphen)."""
    return "".join(ch for ch in text if not _is_emoji_char(ch)
                   and unicodedata.category(ch) not in ("So", "Sk", "Cc", "Cf", "Co", "Cs", "Mn")
                   or ch in " \t")


def _one_line(text: str | None, max_len: int) -> str:
    s = re.sub(r"\s+", " ", (text or "")).strip()
    return s if len(s) <= max_len else s[:max_len - 1].rstrip() + "…"


def _cut_words(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    space = cut.rfind(" ")
    if space >= max_len * 0.6:
        cut = cut[:space]
    return cut.rstrip(" -–—|,:;/")


def _fix_all_caps(title: str) -> str:
    letters = [ch for ch in title if ch.isalpha()]
    if len(letters) < 10 or sum(ch.isupper() for ch in letters) / len(letters) < 0.8:
        return title
    words = []
    for w in title.split(" "):
        core = re.sub(r"[^\w]", "", w).upper()
        words.append(w if core in _UPPER_KEEP else w.lower())
    out = " ".join(words)
    for i, ch in enumerate(out):  # ersten Buchstaben groß
        if ch.isalpha():
            return out[:i] + ch.upper() + out[i + 1:]
    return out


def _clean_title(title: str | None, max_len: int) -> str:
    t = re.sub(r"(?i)#shorts?\b", "", title or "")
    t = _MENTION_RE.sub("", t)
    t = _GTA6_VARIANT_RE.sub("GTA 6", t)
    t = re.sub(r"\s+", " ", t).strip().strip('"“”')
    t = _fix_all_caps(t)
    return _cut_words(t, max_len)


def _is_gta_plan(p: EditPlan, yt_title: str) -> bool:
    """Erwähnen Titel, Hook, Beschreibungen, Hashtags oder Tags GTA? (Kanal ist GTA-6-only, andere Spiele
    lehnt der Prompt ab – fremde Inhalte bekommen aber nie ein irreführendes "GTA 6" angehängt.)"""
    parts = [yt_title, p.titles.tiktok, p.titles.instagram, p.hook_text, p.descriptions.youtube,
             p.descriptions.tiktok, p.descriptions.instagram, *p.hashtags, *p.tags]
    return any(_GTA_ANY_RE.search(t or "") for t in parts)


def _with_gta6(title: str, max_len: int) -> str:
    """Hängt " | GTA 6" an, wenn der Titel das Suchwort nicht enthält (kürzt dafür notfalls)."""
    if not title or _GTA6_PRESENT_RE.search(title):
        return title
    suffix = " | GTA 6"
    return _cut_words(title, max_len - len(suffix)) + suffix


_TRAILING_TAGS = re.compile(r"(?:\s*#[\w]+)+\s*$")


def _clean_description(body: str | None, max_len: int) -> tuple[str, list[str]]:
    """Body ohne angehängte Hashtag-Blöcke (die wandern in hashtags) und ohne #shorts."""
    text = (body or "").replace("\r\n", "\n").strip()
    tags: list[str] = []
    m = _TRAILING_TAGS.search(text)
    if m:
        tags = re.findall(r"#([\w]+)", m.group(0))
        text = text[:m.start()].rstrip()
    text = re.sub(r"(?i)#shorts?\b", "", text)
    text = _MENTION_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return _cut_words(text, max_len), tags


def _clean_hashtags(tags: list[str], gta: bool = True) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        t = re.sub(r"[^\w]", "", str(tag or "").strip().lstrip("#"))
        if not t or len(t) > 40:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    if gta and "gta6" not in seen:
        out.insert(0, "gta6")
    return out[:MAX_HASHTAGS]


def _clean_hook(text: str | None) -> str:
    t = _strip_symbols(_MENTION_RE.sub("", text or ""))
    t = re.sub(r"\s+", " ", t).strip().strip('"“”\'')
    words = t.split()[:HOOK_MAX_WORDS]
    while len(words) > 1 and len(" ".join(words)) > HOOK_MAX_CHARS:
        words.pop()
    hook = " ".join(words)
    if len(hook) > HOOK_MAX_CHARS:
        hook = hook[:HOOK_MAX_CHARS]
    return hook.rstrip(" ,;:-–—")


def _clean_highlights(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        for part in str(item or "").split():
            w = part.strip(".,!?;:\"'()[]{}…“”‘’*")
            if not w or len(w) > 24 or w.lower() in seen:
                continue
            seen.add(w.lower())
            out.append(w)
    return out[:MAX_HIGHLIGHT_WORDS]


# =========================================================================== mine_moments


def mine_moments(llm: LLMClient, video: CandidateIn, ctx: VideoContext, settings: Settings, *,
                 now: datetime | None = None) -> list[MomentPick]:
    """Aus Transkript + Heatmap + Kapiteln die besten Ausschnitte (je 15–55 s) wählen
    (max sources.youtube.moments_per_video), Grenzen an Satzgrenzen."""
    max_n = settings.config.sources.youtube.moments_per_video
    if max_n <= 0:
        return []
    segs = list(ctx.transcript.segments) if ctx.transcript is not None else []
    if not segs and not ctx.heatmap:
        log.info("mine_moments: %s has neither transcript nor heatmap – skipped", ctx.video_id)
        return []
    now = _utc(now or datetime.now(UTC))
    duration = _video_duration(video, ctx)
    blocks = [
        {"type": "text", "text": _moments_context(video, ctx, duration, max_n, now, settings)},
        {"type": "text", "text": prompts.MOMENTS_TASK.format(max_moments=max_n)},
    ]
    result = llm.complete_json(prompts.SYSTEM_MOMENTS, blocks, MomentPicks, max_tokens=MAX_OUTPUT_TOKENS)
    picks = result if isinstance(result, MomentPicks) else MomentPicks.model_validate(
        result.model_dump() if hasattr(result, "model_dump") else result)
    return _sanitize_moments(picks.moments, segs, duration, max_n)


def _video_duration(video: CandidateIn, ctx: VideoContext) -> float:
    for d in (ctx.duration, video.duration_s):
        v = _finite(d)
        if v is not None and v > 0:
            return v
    ends = [s.end for s in (ctx.transcript.segments if ctx.transcript else [])] + [h.end for h in ctx.heatmap]
    return max(ends, default=0.0)


def _moments_context(video: CandidateIn, ctx: VideoContext, duration: float, max_n: int, now: datetime,
                     settings: Settings) -> str:
    lines = ["LONG VIDEO"]
    lines.append(f'- Title: "{_one_line(ctx.title or video.title, 300)}"')
    lines.append(f"- Channel: {ctx.channel or video.creator_name}")
    lines.append(f"- Duration: {_hms(duration)} ({duration:.0f} s)")
    views = ctx.view_count if ctx.view_count is not None else video.view_count
    hours = _age_hours(video.created_at, now)
    if views is not None:
        lines.append(f"- Views: {_num(views)} | Age: {hours:.1f} h | Velocity: {_num(views / hours)} views/h")
    else:
        lines.append(f"- Age: {hours:.1f} h")
    lines.append(f"- Current date (UTC): {now.date().isoformat()} ({_release_note(now, _release_date(settings))})")
    lines += ["", "LIMITS",
              f"- At most {max_n} moments, each {MOMENT_MIN_S:.0f}-{MOMENT_MAX_S:.0f} s (ideal 20-40 s), "
              "no overlaps",
              f"- Timestamps in seconds from the start of the video, within 0-{duration:.0f}"]
    desc = _one_line(ctx.description, 1500)
    if desc:
        lines += ["", "DESCRIPTION (truncated)", desc]
    if ctx.chapters:
        lines += ["", "CHAPTERS"]
        for ch in ctx.chapters[:60]:
            st = _finite(ch.get("start_time"))
            if st is None:
                continue
            lines.append(f"- {_hms(st)} ({st:.0f} s) {_one_line(str(ch.get('title') or ''), 120)}")
    peaks = _heat_peaks(ctx.heatmap)
    if peaks:
        lines += ["", "MOST REPLAYED PEAKS (heatmap 0-1, higher = rewatched more)"]
        for h in peaks:
            lines.append(f"- {h.start:.0f}-{h.end:.0f} s ({_hms(h.start)}): {h.value:.2f}")
    lines += ["", _moments_transcript(ctx, peaks)]
    return "\n".join(lines)


def _heat_peaks(heat: list[HeatPoint]) -> list[HeatPoint]:
    """Lokale Maxima der Heatmap, stärkste zuerst."""
    pts = sorted(heat, key=lambda h: h.start)
    peaks = []
    for i, h in enumerate(pts):
        left = pts[i - 1].value if i > 0 else -1.0
        right = pts[i + 1].value if i + 1 < len(pts) else -1.0
        if h.value >= left and h.value >= right and h.value > 0:
            peaks.append(h)
    peaks.sort(key=lambda h: h.value, reverse=True)
    return peaks[:MAX_HEAT_PEAKS]


def _moments_transcript(ctx: VideoContext, peaks: list[HeatPoint]) -> str:
    segs = list(ctx.transcript.segments) if ctx.transcript is not None else []
    if not segs:
        return "TRANSCRIPT: none available - rely on heatmap and chapters"
    n_words = [len(s.text.split()) for s in segs]
    if sum(n_words) <= MOMENT_WORD_BUDGET:
        chosen = set(range(len(segs)))
        header = "TRANSCRIPT (segment level, seconds)"
    else:
        chosen = _budget_segments(segs, n_words, peaks, ctx)
        header = ("TRANSCRIPT (segment level, seconds; long video - only the parts around heatmap peaks and "
                  "chapter starts are shown, '...' marks omitted parts)")
    out = [header]
    prev = -1
    for i in sorted(chosen):
        if prev >= 0 and i != prev + 1:
            out.append(f"[... {segs[prev].end:.1f}-{segs[i].start:.1f} omitted ...]")
        out.append(f"[{segs[i].start:.1f}-{segs[i].end:.1f}] {_one_line(segs[i].text, 400)}")
        prev = i
    return "\n".join(out)


def _budget_segments(segs: list[Segment], n_words: list[int], peaks: list[HeatPoint],
                     ctx: VideoContext) -> set[int]:
    windows: list[tuple[float, float]] = [(h.start - 60.0, h.end + 60.0) for h in peaks]
    for ch in ctx.chapters:
        st = _finite(ch.get("start_time"))
        if st is not None:
            windows.append((st, st + 90.0))
    if not windows:  # weder Heatmap noch Kapitel: gleichmäßig verteilte Fenster
        total = segs[-1].end
        k = 16
        windows = [(total * (i + 0.5) / k - 75.0, total * (i + 0.5) / k + 75.0) for i in range(k)]
    chosen: set[int] = set()
    used = 0
    for a, b in windows:
        for i, s in enumerate(segs):
            if s.end < a or s.start > b or i in chosen:
                continue
            if used + n_words[i] > MOMENT_WORD_BUDGET:
                return chosen
            chosen.add(i)
            used += n_words[i]
    return chosen


def _snap(t: float, edges: list[float], tol: float) -> float:
    best = min(edges, key=lambda x: abs(x - t), default=None)
    return best if best is not None and abs(best - t) <= tol else t


def _sanitize_moments(picks: list[MomentPick], segs: list[Segment], duration: float,
                      max_n: int) -> list[MomentPick]:
    starts = [s.start for s in segs]
    ends = [s.end for s in segs]
    upper = duration if duration > 0 else math.inf
    cleaned: list[MomentPick] = []
    for m in picks:
        s, e = _finite(m.start), _finite(m.end)
        if s is None or e is None:
            continue
        if e < s:
            s, e = e, s
        s, e = max(0.0, s), min(e, upper)
        # an Satz-/Segmentgrenzen ausrichten
        s = _snap(s, starts, SNAP_TOLERANCE_S)
        e = min(_snap(e, ends, SNAP_TOLERANCE_S), upper)
        if e <= s:
            continue
        if e - s > MOMENT_MAX_S:
            inside = [x for x in ends if s + MOMENT_MIN_S <= x <= s + MOMENT_MAX_S]
            e = max(inside) if inside else s + MOMENT_MAX_S
        elif e - s < MOMENT_MIN_S:
            pad = (MOMENT_MIN_S - (e - s)) / 2.0
            s, e = s - pad, e + pad
            if s < 0:
                e, s = e - s, 0.0
            if e > upper:
                s, e = max(0.0, s - (e - upper)), upper
            if e - s < MOMENT_MIN_S - 1e-6:  # Video zu kurz
                continue
        score = int(round(_clamp(_finite(m.score, 0.0), 0.0, 100.0)))
        cleaned.append(MomentPick(start=round(s, 2), end=round(e, 2), score=score, why=_one_line(m.why, 300)))
    cleaned.sort(key=lambda m: m.score, reverse=True)
    out: list[MomentPick] = []
    for m in cleaned:
        if any(m.start < o.end and o.start < m.end for o in out):
            continue
        out.append(m)
        if len(out) >= max_n:
            break
    return out


# =========================================================================== Helfer


def _finite(v: Any, default: float | None = None) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _clamp(v: float | None, lo: float, hi: float) -> float:
    return min(max(lo if v is None else v, lo), hi)


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _age_hours(created: datetime, now: datetime) -> float:
    hours = (_utc(now) - _utc(created)).total_seconds() / 3600.0
    return max(hours, 0.5) if math.isfinite(hours) else 0.5


def _num(v: float | int) -> str:
    return f"{round(float(v)):,}"


def _metric(v: float | int) -> str:
    """Zähler mit Tausenderpunkten, kleine Brüche (z. B. upvote_ratio) mit 2 Nachkommastellen."""
    f = float(v)
    return f"{f:.2f}" if abs(f) < 10 and not f.is_integer() else _num(f)


def _hms(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _scalar_extras(extra: dict | None) -> list[tuple[str, Any]]:
    """Kurze, skalare Zusatzinfos der Quelle (keine IDs/Heatmaps/URLs, keine Personen außer dem Creator –
    z. B. clip_creator_name ist der Zuschauer, der den Clip geschnitten hat)."""
    out = []
    for k, v in sorted((extra or {}).items()):
        if k in _EXTRA_SKIP or any(x in k.lower() for x in ("id", "url", "heatmap", "token", "offset", "thumb")):
            continue
        if isinstance(v, str) and _URL_RE.match(v.strip()):
            continue
        if isinstance(v, bool | int | float) or (isinstance(v, str) and 0 < len(v) <= 120):
            out.append((k, v))
    return out[:8]
