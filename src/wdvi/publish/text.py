"""Finale Titel/Beschreibungen je Plattform (Credit + Fan-Hinweis werden IMMER vom Code angehängt)."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Protocol

from wdvi.config import Settings
from wdvi.schemas import CandidateIn, SourceKind, TimeMap

FAN_DISCLAIMER = "Fan channel – not affiliated with Rockstar Games or Take-Two."
REQUIRED_HASHTAG = "GTA6"  # nur noch bei GTA-Inhalten (siehe is_gta_content)
MAX_HASHTAGS = 5  # Beschreibung/Caption: 3–5 Hashtags wirken besser als lange Hashtag-Blöcke
MIN_HASHTAGS = 3  # zu wenige Plan-Hashtags -> mit Plan-Tags auffüllen
TRANSCRIPT_LABEL = "Transcript:"
TRANSCRIPT_MAX_CHARS = 600
YOUTUBE_TITLE_MAX = 100
SHORTS_SUFFIX = " #Shorts"
SOCIAL_TITLE_MAX = 150  # TikTok/Instagram: Titel = erste Zeile der Caption, kurz halten
DEFAULT_TITLE = "GTA 6 Clip"

# Beschreibungslimits: YouTube 5000 BYTES (UTF-8), TikTok 2200 UTF-16-Einheiten, Instagram 2200 Zeichen
DESCRIPTION_LIMITS = {"youtube": 5000, "tiktok": 2200, "instagram": 2200}

_HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)
_TAG_CLEAN_RE = re.compile(r"[^\w]", re.UNICODE)
_GTA_RE = re.compile(r"gta|grand\s*theft\s*auto|vice\s*city|leonida", re.IGNORECASE)
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.!?;:…%)])")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


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


def normalize_hashtags(tags: Iterable[str], limit: int = MAX_HASHTAGS, *, gta: bool = True) -> list[str]:
    """Ohne '#', case-insensitiv dedupliziert, max. limit Stück. gta=True: "GTA6" steht zuerst."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in [REQUIRED_HASHTAG, *tags] if gta else list(tags):
        tag = clean_hashtag(str(raw))
        if not tag or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(tag)
    return out[:limit]


def hashtags_in(text: str) -> list[str]:
    return _HASHTAG_RE.findall(text)


def _flatten(parts: Iterable[object]) -> Iterable[str]:
    for p in parts:
        if p is None:
            continue
        if isinstance(p, str):
            yield p
        elif isinstance(p, Iterable):
            yield from _flatten(p)
        else:
            yield str(p)


def is_gta_content(*parts: object) -> bool:
    """True, wenn Titel/Hashtags/Tags/Text GTA erwähnen ("GTA6", "gtavi", "Grand Theft Auto", "Vice City").
    Akzeptiert Strings und (verschachtelte) Listen."""
    return any(_GTA_RE.search(p) for p in _flatten(parts))


def _wants_gta(
    gta: bool | None, hashtags: list[str], tags: list[str] | None, title: str | None, body: str
) -> bool:
    """#GTA6 nur bei GTA-Inhalten. Ohne jede Plan-Information (keine Hashtags/Tags/Titel) bleibt es beim
    Kanal-Standard (GTA-Kanal -> #GTA6), damit alte Aufrufe/gespeicherte Clips sich nicht ändern."""
    if gta is not None:
        return gta
    if is_gta_content(hashtags, tags or [], title or "", body):
        return True
    return not (hashtags or tags or (title or "").strip())


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


class _WordLike(Protocol):
    text: str
    start: float
    end: float


def _word_key(w: str) -> str:
    return re.sub(r"[^\w]", "", w, flags=re.UNICODE).casefold()


def clean_transcript(text: str, max_chars: int = TRANSCRIPT_MAX_CHARS) -> str:
    """Gesprochener Text für die Beschreibung: einzeilig, ohne '#', '<', '>' (würden als Hashtag bzw. von
    YouTube als ungültig gewertet), Whisper-Wiederholungen ("no no no no no") auf max. 2 gekürzt,
    kein Leerzeichen vor Satzzeichen, max. max_chars Zeichen (an einer Wortgrenze mit "…")."""
    t = _CTRL_RE.sub(" ", text or "").replace("#", "").replace("<", "").replace(">", "")
    words: list[str] = []
    run = 0
    for w in t.split():
        k = _word_key(w)
        if words and k and k == _word_key(words[-1]):
            run += 1
            if run >= 2:
                continue
        else:
            run = 0
        words.append(w)
    out = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", " ".join(words)).strip()
    if not _word_key(out):  # nur Satzzeichen/Leerzeichen
        return ""
    return truncate(out, max_chars, len) if max_chars > 0 else out


def transcript_text(
    words: Iterable[_WordLike] | None, timemap: TimeMap | None = None, max_chars: int = TRANSCRIPT_MAX_CHARS
) -> str:
    """Gesprochene Wörter des fertigen Clips: nur Wörter, deren Mitte in einem behaltenen Abschnitt der
    TimeMap liegt (Quellzeit), in Reihenfolge der Ausgabe. Ohne TimeMap: alle Wörter."""
    picked: list[tuple[float, str]] = []
    for w in words or []:
        txt = str(getattr(w, "text", "") or "").strip()
        if not txt:
            continue
        start, end = float(w.start), float(w.end)
        mid = (start + max(start, end)) / 2
        if timemap is None:
            picked.append((mid, txt))
            continue
        out_t = timemap.to_output(mid)
        if out_t is not None:
            picked.append((out_t, txt))
    picked.sort(key=lambda x: x[0])
    return clean_transcript(" ".join(t for _, t in picked), max_chars)


def transcript_in(description: str | None) -> str | None:
    """Transkript-Block ("Transcript:" ...) aus einer fertigen Beschreibung (für Bearbeitungen), sonst None."""
    d = (description or "").replace("\r\n", "\n")
    i = d.find(TRANSCRIPT_LABEL)
    if i < 0:
        return None
    block = d[i + len(TRANSCRIPT_LABEL):].strip()
    lines = block.split("\n\n")[0].split("\n")  # bis zur Leerzeile vor den Hashtags
    kept = [ln for ln in lines if not (ln.strip() and all(x.startswith("#") for x in ln.split()))]
    t = " ".join(ln.strip() for ln in kept).strip()
    return t or None


def _clean_body(body: str) -> str:
    body = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    body = re.sub(r"[ \t]+\n", "\n", body)
    return re.sub(r"\n{3,}", "\n\n", body)


def compose_description(
    platform: str,
    body: str,
    hashtags: list[str],
    c: CandidateIn,
    settings: Settings,
    *,
    transcript: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    gta: bool | None = None,
) -> str:
    """body + Leerzeile + credit_line + Fan-Hinweis (+ YouTube: "Transcript:"-Block mit dem gesprochenen Text,
    gut für die Suche) + 3–5 Hashtags. #GTA6 nur bei GTA-Inhalten (gta=None: aus Titel/Hashtags/Tags/Text
    erkannt). TikTok/Instagram bleiben kurz (kein Transkript). Plattformlimits: YouTube-Beschreibung 5000 Bytes,
    TikTok/Instagram 2200 Zeichen."""
    limit = DESCRIPTION_LIMITS.get(platform, 2200)
    measure = measure_for(platform)
    body = _clean_body(body)
    if platform == "youtube":
        body = youtube_safe(body)
    with_gta = _wants_gta(gta, list(hashtags or []), tags, title, body)

    # Hashtags, die schon im Text stehen, nicht wiederholen; insgesamt max. MAX_HASHTAGS
    body_tags = {t.lower() for t in hashtags_in(body)}
    pool = list(hashtags or [])
    if tags and len({clean_hashtag(str(h)).lower() for h in pool} - {""}) < MIN_HASHTAGS:
        pool += [t for t in tags if len(clean_hashtag(str(t))) <= 24]  # zu wenige -> mit Plan-Tags auffüllen
    tags_out = [
        t
        for t in normalize_hashtags(pool, limit=MAX_HASHTAGS + len(body_tags) + 1, gta=with_gta)
        if t.lower() not in body_tags
    ]
    budget = max(0, MAX_HASHTAGS - len(body_tags))
    if with_gta and REQUIRED_HASHTAG.lower() not in body_tags:
        budget = max(1, budget)  # #GTA6 steht immer vorne in tags_out
    tags_out = tags_out[:budget]

    tail = credit_line(c) + "\n" + FAN_DISCLAIMER
    spoken = clean_transcript(transcript or "") if platform == "youtube" else ""
    if spoken:
        tail += f"\n\n{TRANSCRIPT_LABEL}\n{spoken}"
    if tags_out:
        tail += "\n\n" + " ".join("#" + t for t in tags_out)
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
