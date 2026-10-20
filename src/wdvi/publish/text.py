"""Finale Titel/Beschreibungen je Plattform (Credit + Fan-Hinweis werden IMMER vom Code angehängt)."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from wdvi.config import Settings
from wdvi.schemas import CandidateIn, SourceKind

FAN_DISCLAIMER = "Fan channel – not affiliated with Rockstar Games or Take-Two."
REQUIRED_HASHTAG = "GTA6"
MAX_HASHTAGS = 8
YOUTUBE_TITLE_MAX = 100
SHORTS_SUFFIX = " #Shorts"
SOCIAL_TITLE_MAX = 150  # TikTok/Instagram: Titel = erste Zeile der Caption, kurz halten
DEFAULT_TITLE = "GTA 6 Clip"

# Beschreibungslimits: YouTube 5000 BYTES (UTF-8), TikTok 2200 UTF-16-Einheiten, Instagram 2200 Zeichen
DESCRIPTION_LIMITS = {"youtube": 5000, "tiktok": 2200, "instagram": 2200}

_HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)
_TAG_CLEAN_RE = re.compile(r"[^\w]", re.UNICODE)


# --------------------------------------------------------------------------- Längenmaße


def utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def measure_for(platform: str) -> Callable[[str], int]:
    if platform == "youtube":
        return utf8_len
    return utf16_len  # TikTok zählt UTF-16-Runes; für Instagram die sichere Variante


def truncate(text: str, max_units: int, measure: Callable[[str], int] = len, ellipsis: str = "…") -> str:
    """Kürzt auf max_units (gemessen mit measure), bevorzugt an einer Wortgrenze, mit "…"."""
    if measure(text) <= max_units:
        return text
    if max_units <= measure(ellipsis):
        return ""
    lo, hi = 0, len(text)
    while lo < hi:  # größtes n mit measure(text[:n] + …) <= max_units
        mid = (lo + hi + 1) // 2
        if measure(text[:mid].rstrip() + ellipsis) <= max_units:
            lo = mid
        else:
            hi = mid - 1
    cut = text[:lo]
    space = cut.rfind(" ")
    if space >= max(1, lo - 30):  # Wortgrenze nur, wenn sie nicht zu viel Text kostet
        cut = cut[:space]
    return cut.rstrip(" \n,;:-–") + ellipsis


def youtube_safe(s: str) -> str:
    """YouTube lehnt '<' und '>' in Titel/Beschreibung ab (invalidTitle/invalidDescription)."""
    return s.replace("<", "‹").replace(">", "›")


# --------------------------------------------------------------------------- Hashtags


def clean_hashtag(raw: str) -> str:
    """'#GTA 6!' -> 'GTA6'. Leere oder reine Ziffern-Tags -> ''."""
    tag = _TAG_CLEAN_RE.sub("", raw.strip().lstrip("#"))
    if not tag or tag.isdigit():
        return ""
    return tag


def normalize_hashtags(tags: Iterable[str], limit: int = MAX_HASHTAGS) -> list[str]:
    """Ohne '#', case-insensitiv dedupliziert, "GTA6" immer zuerst, max. limit Stück."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in [REQUIRED_HASHTAG, *tags]:
        tag = clean_hashtag(str(raw))
        if not tag or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(tag)
    return out[:limit]


def hashtags_in(text: str) -> list[str]:
    return _HASHTAG_RE.findall(text)


# --------------------------------------------------------------------------- Credit / Beschreibung


def _display_url(url: str | None) -> str:
    if not url:
        return ""
    u = re.sub(r"^\s*https?://", "", url.strip(), flags=re.IGNORECASE)
    u = re.sub(r"^www\.", "", u, flags=re.IGNORECASE)
    return u.rstrip("/")


def reddit_via(c: CandidateIn) -> str | None:
    """Reddit-Poster eines Posts, der auf einen Twitch-/Kick-/YouTube-Clip verlinkt ("u/name"), sonst None.
    Nur bei reddit_post: YouTube-Kandidaten nutzen extra.via für ihre Herkunft ("rss"/"search")."""
    if c.source != SourceKind.reddit_post or c.platform_label == "Reddit":
        return None
    via = str((c.extra or {}).get("via") or "").strip()
    if not via:
        return None
    return via if via.lower().startswith("u/") else f"u/{via.lstrip('@/')}"


def credit_line(c: CandidateIn) -> str:
    """z. B. "🎥 Clip: xQc on Twitch – twitch.tv/xqc" (creator_url ohne https://, falls vorhanden). Reddit-Post
    mit Original-Clip: "🎥 Clip: shroud on Twitch – twitch.tv/shroud (via u/clipposter on Reddit)"."""
    name = (c.creator_name or "").strip() or (c.creator_handle or "").strip() or "unknown"
    line = f"🎥 Clip: {name} on {c.platform_label}"
    url = _display_url(c.creator_url)
    if url:
        line += f" – {url}"
    via = reddit_via(c)
    if via:
        line += f" (via {via} on Reddit)"
    return line


def _clean_body(body: str) -> str:
    body = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    body = re.sub(r"[ \t]+\n", "\n", body)
    return re.sub(r"\n{3,}", "\n\n", body)


def compose_description(
    platform: str, body: str, hashtags: list[str], c: CandidateIn, settings: Settings
) -> str:
    """body + Leerzeile + credit_line + Fan-Hinweis ("Fan channel – not affiliated with Rockstar Games.")
    + Hashtags (#GTA6 immer dabei). Plattformlimits: YouTube-Beschreibung 5000, TikTok 2200,
    Instagram 2200 Zeichen / max 30 Hashtags (wir nutzen <= 8)."""
    limit = DESCRIPTION_LIMITS.get(platform, 2200)
    measure = measure_for(platform)
    body = _clean_body(body)
    if platform == "youtube":
        body = youtube_safe(body)

    # Hashtags, die schon im Text stehen, nicht wiederholen; insgesamt max. MAX_HASHTAGS
    body_tags = {t.lower() for t in hashtags_in(body)}
    tags = [
        t
        for t in normalize_hashtags(hashtags, limit=MAX_HASHTAGS + len(body_tags))
        if t.lower() not in body_tags
    ]
    budget = max(0, MAX_HASHTAGS - len(body_tags))
    if REQUIRED_HASHTAG.lower() not in body_tags:
        budget = max(1, budget)  # #GTA6 steht immer vorne in tags
    tags = tags[:budget]

    tail = credit_line(c) + "\n" + FAN_DISCLAIMER
    if tags:
        tail += "\n\n" + " ".join("#" + t for t in tags)
    if platform == "youtube":
        tail = youtube_safe(tail)
    if not body:
        return truncate(tail, limit, measure)
    room = limit - measure(tail) - measure("\n\n")
    body = truncate(body, room, measure) if room > 0 else ""
    return f"{body}\n\n{tail}" if body else truncate(tail, limit, measure)


# --------------------------------------------------------------------------- Titel / Caption


def _cut_youtube_title(t: str) -> str:
    """> 100 Zeichen -> 93..100 Zeichen mit "…" (so lang, dass " #Shorts" danach nicht mehr passt ->
    compose_title bleibt idempotent)."""
    head = t[: YOUTUBE_TITLE_MAX - 1]
    space = head.rfind(" ")
    min_len = YOUTUBE_TITLE_MAX - len(SHORTS_SUFFIX)
    if space >= min_len:
        return head[:space] + "…"
    return head + "…"


def compose_title(platform: str, title: str) -> str:
    """YouTube: <= 100 Zeichen. TikTok/Instagram: Titel ist Teil der Caption (wird vorangestellt)."""
    t = " ".join((title or "").split())  # einzeilig, Mehrfach-Leerzeichen weg
    if platform == "youtube":
        t = youtube_safe(t) or DEFAULT_TITLE
        if len(t) > YOUTUBE_TITLE_MAX:
            return _cut_youtube_title(t)
        if "#shorts" not in t.lower() and len(t) + len(SHORTS_SUFFIX) <= YOUTUBE_TITLE_MAX:
            t += SHORTS_SUFFIX
        return t
    return truncate(t, SOCIAL_TITLE_MAX, len)


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def build_caption(platform: str, title: str, description: str) -> str:
    """TikTok/Instagram-Caption = Titel + Leerzeile + Beschreibung (Titel entfällt, wenn die Beschreibung
    schon damit beginnt oder das Limit sonst überschritten würde)."""
    limit = DESCRIPTION_LIMITS.get(platform, 2200)
    measure = measure_for(platform)
    t = compose_title(platform, title) if (title or "").strip() else ""
    desc = (description or "").strip()
    if t and desc and _norm(desc).startswith(_norm(t).rstrip("…")):
        t = ""
    caption = f"{t}\n\n{desc}" if t and desc else (t or desc)
    if measure(caption) > limit and desc:
        caption = desc
    return truncate(caption, limit, measure)
