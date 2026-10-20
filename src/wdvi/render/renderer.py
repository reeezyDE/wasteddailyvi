"""Einstiegspunkt Rendering: Quellclip + EditPlan -> fertiges 9:16-MP4 + Thumbnail."""

from __future__ import annotations

import logging
import os
import re
import time
import zlib
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from wdvi.config import Settings
from wdvi.render import _ff, audio, captions, compositor, encoder, overlays, timeline
from wdvi.render._ff import RenderError
from wdvi.schemas import (
    Box,
    CandidateIn,
    EditPlan,
    LayoutHint,
    MediaAnalysis,
    RenderResult,
    TimeMap,
    TrackPoint,
)

__all__ = ["RenderError", "render_clip", "safe_key"]

log = logging.getLogger(__name__)

MAX_CUT_SIDE = 1920  # Zwischendatei max. 1920 px lang (4K-Quellen wären zu langsam)
AFADE_S = 0.01  # Mini-Crossfade an jedem Schnitt (kein Knacken)
HOOK_TOP_RATIO = 0.065  # Oberkante Hook-Pille
CREDIT_Y_RATIO = 0.025
SIDE_MARGIN_RATIO = 0.035
EMOJI_SIZE_RATIO = 150 / 1080
EMOJI_SECONDS = 0.7
EMOJI_GAP = 26  # Abstand Emoji-Unterkante -> Caption-Oberkante (px bei 1080 Breite)
SFX_DEDUP_S = 0.3
MUSIC_EXT = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus")
# --- Edit-Engine v2
MIN_SHOT_S = 0.12  # kürzere (nach dem Schnitt übrig gebliebene) Shots fallen weg
OUTRO_Y_RATIO = 0.42  # Mitte der Schlusskarte
OUTRO_POP_FROM = 0.55
OUTRO_POP_S = 0.28
OUTRO_CAPTION_GAP = 36  # Mindestabstand Karte <-> Caption-Zeile (px bei 1080 Breite)
OUTRO_MIN_S = 1.2  # so lange steht die Schlussfrage mindestens (auch wenn bis zum Ende gesprochen wird)
OUTRO_AFTER_SPEECH_S = captions.LINGER_S + 0.05  # erst wenn die letzte Caption-Zeile weg ist (Pointe frei)
OUTRO_PNG = "outro.png"
THUMB_AFTER_PEAK_S = 0.15  # Gesicht kurz nach dem Lautstärke-Peak (Mund offen, nicht verwackelt)
THUMB_AFTER_TRANSITION_S = 0.35  # Übergänge (flash/punch/glitch) sind dann sicher vorbei
EMOJI_MIN_BEFORE_CUT = 0.25  # Emoji endet am nächsten Shot-Wechsel, wenn es bis dahin so lange stand
TRANSITION_SFX = {"punch": "whoosh", "glitch": "whoosh"}
OUTRO_SFX = "ding"  # Schlussfrage poppt mit einem hellen Ton auf (Aufmerksamkeit für die Kommentarfrage)


def safe_key(key: str) -> str:
    """Candidate-Key -> sicherer Dateiname (gleiches Schema wie die Downloads)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", key).strip("_")[:150] or "clip"


def render_clip(source: Path, analysis: MediaAnalysis, plan: EditPlan, c: CandidateIn, settings: Settings,
                work_dir: Path, out_dir: Path) -> RenderResult:
    """1. timemap = build_timemap(plan)
    2. ffmpeg: Segmente schneiden + concat (Video CFR render.fps, Audio mit 10-ms-Crossfades) -> cut.mp4
    3. Captions-ASS aus analysis.transcript (Wörter) + plan.highlight_words
    4. Overlays: Hook (0..hook_seconds, oben), Credit (ganze Dauer, oben links, wenn show_credit),
       Wasserzeichen (unten rechts klein), Emojis an emphasis-Zeitpunkten (0.7 s, über den Captions)
    5. Audio: SFX aus emphasis.sfx + automatisch "whoosh" am Start jedes Zooms (wenn render.sfx)
    v2: plan.shots/annotations/emphasis.shake|flash/outro_text/hook_style + render.color_grade -> CompositionSpec
    (alle Planzeiten sind Quellzeit und werden über die TimeMap umgerechnet)
    6. compose_video -> out_dir/<candidate-key-sicher>-<UTC-Zeitstempel>.mp4 (je Render eindeutig),
       Thumbnail (JPEG, gleicher Name) aus dem stärksten Moment
    Wirft RenderError mit ffmpeg-stderr-Auszug."""
    t0 = time.monotonic()
    source = Path(source)
    if not source.is_file():
        raise RenderError(f"Quellvideo fehlt: {source}")
    key = safe_key(c.key)
    work = Path(work_dir) / f"render_{key}"
    out_dir = Path(out_dir)
    out_mp4: Path | None = None
    try:
        work.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_mp4 = _reserve_output(out_dir, key)
        result = _render(source, analysis, plan, c, settings, work, out_mp4, key)
    except BaseException as e:
        if out_mp4 is not None:  # gescheitert -> keine verwaisten (Teil-)Dateien in clips_dir liegen lassen
            for p in (out_mp4, out_mp4.with_suffix(".jpg"), out_mp4.with_name(out_mp4.stem + ".part.jpg")):
                with suppress(OSError):
                    p.unlink(missing_ok=True)
        if isinstance(e, RenderError) or not isinstance(e, Exception):
            raise
        log.exception("Render of %s failed", c.key)
        raise RenderError(f"Rendern von {c.key} fehlgeschlagen: {type(e).__name__}: {e}") from e
    log.info("Rendered %s -> %s (%.1f s video, %s, %.1f s)", c.key, Path(result.video_path).name, result.duration,
             result.encoder, time.monotonic() - t0)
    return result


# --------------------------------------------------------------------------- Ablauf


def _reserve_output(out_dir: Path, key: str) -> Path:
    """Eindeutigen Zielnamen <key>-<UTC-Zeitstempel>[-n].mp4 atomar (O_EXCL) reservieren.
    Ein erneutes Rendern desselben Kandidaten (z. B. `wdvi process-url`) darf das Video eines bestehenden,
    evtl. schon eingeplanten Clips nie überschreiben – und ein vom Aufräumen gelöschter Name kommt nie wieder."""
    base = f"{key}-{datetime.now(UTC):%Y%m%d-%H%M%S}"
    for n in range(1, 1000):
        mp4 = out_dir / (f"{base}.mp4" if n == 1 else f"{base}-{n}.mp4")
        if mp4.with_suffix(".jpg").exists():
            continue
        try:
            fd = os.open(mp4, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        os.close(fd)  # leerer Platzhalter, compose_video ersetzt ihn per os.replace
        return mp4
    raise RenderError(f"Kein freier Dateiname für {key} in {out_dir}")


def _render(source: Path, analysis: MediaAnalysis, plan: EditPlan, c: CandidateIn, settings: Settings,
            work: Path, out_mp4: Path, key: str) -> RenderResult:
    cfg = settings.config
    r = cfg.render
    W, H = int(r.width), int(r.height)

    # 1) Schnittliste
    src_info = _ff.probe(source)
    src_dur = _ff.duration(src_info) or analysis.info.duration
    tm = timeline.build_timemap(plan, min(src_dur, analysis.info.duration) if analysis.info.duration else src_dur)
    if tm.duration > cfg.filters.max_output_duration_s:
        log.warning("Cut list %.1f s > max %.1f s, truncating", tm.duration, cfg.filters.max_output_duration_s)
        tm = timeline.cap_duration(tm, cfg.filters.max_output_duration_s)
    if not tm.segments:
        raise RenderError(f"Leere Schnittliste für {c.key} (trim {plan.trim_start:.1f}–{plan.trim_end:.1f} s, "
                          f"Quelle {src_dur:.1f} s)")

    # 2) Schneiden
    cut = work / "cut.mkv"
    _cut_segments(source, src_info, tm, int(r.fps), cut)
    cut_dur = _ff.duration(_ff.probe(cut)) or tm.duration

    # 3) Layout (+ Shots) + Captions
    layout = _resolve_layout(plan, analysis, tm)
    shots = _map_shots(plan, tm, cut_dur)
    cap_layout = "shots" if shots else layout.kind
    words = analysis.transcript.words() if analysis.transcript else []
    ass_path: Path | None = None
    if words:
        ass_path = captions.build_ass(words, tm, plan.highlight_words, cap_layout, settings, work / "captions.ass",
                                      word_colors=plan.word_colors or None)

    # 4) Zooms, Overlays, 5) SFX
    zooms = _map_zooms(plan, tm)
    first_kind = shots[0].kind if shots else None
    ovs = _build_overlays(plan, c, settings, tm, cap_layout, work, cut_dur, first_shot=first_kind,
                          speech=_speech_intervals(words, tm) if words else [], cuts=[s.start for s in shots[1:]])
    sfx_events = _sfx_events(plan, tm, zooms, shots) if r.sfx else []
    outro_ov = next((o for o in ovs if o.png.name == OUTRO_PNG), None)
    if r.sfx and outro_ov is not None and not any(n == OUTRO_SFX and abs(t - outro_ov.start) < SFX_DEDUP_S
                                                  for t, n in sfx_events):
        sfx_events = sorted([*sfx_events, (round(outro_ov.start, 3), OUTRO_SFX)])

    wav = audio.build_audio(cut, sfx_events, settings, work / "audio.wav", music_track=_pick_music(settings, key))

    # 6) Komposition + Encoding (NVENC-Fehler -> einmal mit libx264 wiederholen)
    spec = compositor.CompositionSpec(
        layout=layout, zooms=zooms, overlays=ovs, shots=shots, annotations=_map_annotations(plan, tm, cut_dur, cfg.brand),
        effects=_map_effects(plan, tm), grade=compositor.GradeSpec.from_render(r) if r.color_grade else None,
        face_box=_face_box(analysis, plan) if shots else None)
    enc = encoder.detect_encoder(r.encoder)
    try:
        compositor.compose_video(cut, spec, settings, ass_path, wav, out_mp4, enc)
    except RenderError as e:
        if enc != encoder.NVENC:
            raise
        log.warning("NVENC encode failed (%s) – retrying with libx264", e)
        enc = encoder.X264
        compositor.compose_video(cut, spec, settings, ass_path, wav, out_mp4, enc)

    info = _ff.probe(out_mp4)
    vs = _ff.video_stream(info) or {}
    duration = _ff.duration(info)
    thumb = out_mp4.with_suffix(".jpg")
    peaks = [t for t in (tm.to_output(x) for x in analysis.loudness_peaks) if t is not None]
    _thumbnail(out_mp4, _thumb_time(zooms, duration, shots, peaks=peaks), thumb)

    for p in (cut, wav):  # große Zwischendateien sofort weg (ASS/PNGs bleiben zum Debuggen)
        p.unlink(missing_ok=True)
    return RenderResult(video_path=str(out_mp4), thumb_path=str(thumb), duration=round(duration, 3),
                        width=int(vs.get("width") or W), height=int(vs.get("height") or H), encoder=enc)


# --------------------------------------------------------------------------- Schnitt


def _cut_segments(source: Path, info: dict, tm: TimeMap, fps: int, out: Path) -> Path:
    """trim/atrim je Segment + concat; Video CFR fps, gerade Maße (max. MAX_CUT_SIDE), Audio 48 kHz Stereo
    mit 10-ms-Fades an den Schnitten. Quellen ohne Ton bekommen Stille (anullsrc)."""
    has_audio = _ff.has_audio(info)
    n = len(tm.segments)
    end_all = max(e for _, e in tm.segments)
    sw, sh = _ff.display_size(info)
    if sw <= 0 or sh <= 0:
        raise RenderError("Quellvideo hat keine lesbare Videospur/Auflösung")
    sw = round(sw * _sar(info))  # anamorphe Quellen (SAR != 1) auf quadratische Pixel bringen
    k = min(1.0, MAX_CUT_SIDE / max(sw, sh))
    cw, ch = max(2, int(sw * k) // 2 * 2), max(2, int(sh * k) // 2 * 2)
    scale = f"scale={cw}:{ch}:flags=bicubic,setsar=1"
    parts: list[str] = []
    vlabels = [f"[vs{i}]" for i in range(n)]
    alabels = [f"[as{i}]" for i in range(n)]
    parts.append(f"[0:v:0]fps={fps}" + (f",split={n}" + "".join(vlabels) if n > 1 else vlabels[0]))
    a_in = "[0:a:0]" if has_audio else "[1:a]"
    parts.append(f"{a_in}aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
                 + (f",asplit={n}" + "".join(alabels) if n > 1 else alabels[0]))
    concat_in = ""
    for i, (s, e) in enumerate(tm.segments):
        d = e - s
        parts.append(f"{vlabels[i]}trim=start={s:.4f}:end={e:.4f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"{alabels[i]}atrim=start={s:.4f}:end={e:.4f},asetpts=PTS-STARTPTS,apad=whole_dur={d:.4f},"
                     f"atrim=end={d:.4f},afade=t=in:st=0:d={AFADE_S},afade=t=out:st={max(0.0, d - AFADE_S):.4f}"
                     f":d={AFADE_S}[a{i}]")
        concat_in += f"[v{i}][a{i}]"
    parts.append(f"{concat_in}concat=n={n}:v=1:a=1[vc][ac]")
    parts.append(f"[vc]fps={fps},{scale},format=yuv420p[vout]")  # nochmals fps: Lücken (A/V-Versatz) füllen

    cmd = [_ff.FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-t", f"{end_all + 0.5:.3f}",
           "-i", str(source)]
    if not has_audio:
        cmd += ["-f", "lavfi", "-t", f"{end_all + 0.5:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]", "-map", "[ac]",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "14", "-g", str(fps),
            "-c:a", "pcm_s16le", "-ar", "48000", str(out)]
    timeout = max(300.0, tm.duration * 30)
    _ff.run(cmd, timeout=timeout, what="Schneiden (Segmente + concat)")
    if not out.is_file() or out.stat().st_size == 0:
        raise RenderError("Schneiden lieferte keine Datei")
    return out


def _sar(info: dict) -> float:
    sar = str((_ff.video_stream(info) or {}).get("sample_aspect_ratio") or "1:1")
    try:
        num, den = (float(x) for x in sar.split(":", 1))
        v = num / den if num > 0 and den > 0 else 1.0
    except ValueError:
        v = 1.0
    return v if 0.25 <= v <= 4.0 else 1.0


# --------------------------------------------------------------------------- Plan -> Ausgabezeit


def _resolve_layout(plan: EditPlan, analysis: MediaAnalysis, tm: TimeMap) -> LayoutHint:
    hint = analysis.layout
    kind = plan.layout or hint.kind
    if kind == "facecam_split" and hint.facecam_box is None:
        log.info("Plan wants facecam_split but no facecam was detected -> gameplay_fit")
        kind = "gameplay_fit"
    track: list[TrackPoint] = []
    for p in hint.track:
        t = tm.to_output(p.t)
        if t is not None:
            track.append(TrackPoint(t=round(t, 3), cx=p.cx, cy=p.cy))
    if hint.track and not track:  # alle Punkte in Schnitten -> Mittelwert halten
        track = [TrackPoint(t=0.0, cx=sum(p.cx for p in hint.track) / len(hint.track),
                            cy=sum(p.cy for p in hint.track) / len(hint.track))]
    return LayoutHint(kind=kind, facecam_box=hint.facecam_box, gameplay_box=hint.gameplay_box, track=track,
                      confidence=hint.confidence)


def _map_zooms(plan: EditPlan, tm: TimeMap) -> list[compositor.ZoomOut]:
    out: list[compositor.ZoomOut] = []
    for z in plan.zooms:
        iv = timeline.map_interval(tm, z.start, z.end)
        if iv is None or iv[1] - iv[0] < 0.2:
            continue
        out.append(compositor.ZoomOut(start=iv[0], end=iv[1], scale=min(1.6, max(1.0, z.scale)), focus=z.focus))
    return sorted(out, key=lambda z: z.start)


def _map_shots(plan: EditPlan, tm: TimeMap, duration: float) -> list[compositor.ShotOut]:
    """plan.shots (Quellzeit) -> lückenlose Shots in Ausgabezeit (herausgeschnittene fallen weg)."""
    out: list[compositor.ShotOut] = []
    for s in plan.shots:
        iv = timeline.map_interval(tm, s.start, s.end)
        if iv is None or iv[1] - iv[0] < MIN_SHOT_S:
            continue
        out.append(compositor.ShotOut(start=iv[0], end=iv[1], kind=s.kind, focus=s.focus,
                                      zoom=min(1.6, max(1.0, float(s.zoom))), transition=s.transition))
    return compositor.normalize_shots(out, duration)


def _map_annotations(plan: EditPlan, tm: TimeMap, duration: float, brand=None) -> list[compositor.AnnotationOut]:
    """plan.annotations (Quellzeit) -> Ausgabezeit; Farben wie in den Captions (pink/orange = Markenfarben)."""
    out: list[compositor.AnnotationOut] = []
    for a in plan.annotations:
        iv = timeline.map_interval(tm, a.start, a.end)
        if iv is None:
            continue
        start, end = iv[0], min(iv[1], duration)
        if end - start < 0.2:
            continue
        label = captions.mask_slurs(a.label.strip()) if a.label and a.label.strip() else None
        out.append(compositor.AnnotationOut(start=start, end=end, kind=a.kind, box=a.box,
                                            color=captions.resolve_color(a.color, brand),
                                            label=label))
    return out


def _map_effects(plan: EditPlan, tm: TimeMap) -> list[compositor.FxEvent]:
    ev: list[compositor.FxEvent] = []
    for e in plan.emphasis:
        if not (e.shake or e.flash):
            continue
        t = tm.to_output(e.t)
        if t is None:
            continue
        if e.shake:
            ev.append(compositor.FxEvent(t=t, kind="shake"))
        if e.flash:
            ev.append(compositor.FxEvent(t=t, kind="flash"))
    return ev


def _face_box(analysis: MediaAnalysis, plan: EditPlan) -> Box | None:
    """Typische Gesichtsposition (Median über die Standbilder, bevorzugt im behaltenen Bereich); bei einer
    Facecam nur Gesichter innerhalb der Facecam."""
    fc = analysis.layout.facecam_box
    found: list[tuple[bool, Box]] = []
    for f in analysis.frames:
        faces = f.faces
        if fc is not None:
            faces = [d for d in faces if fc.x <= d.box.cx <= fc.x + fc.w and fc.y <= d.box.cy <= fc.y + fc.h]
        if not faces:
            continue
        best = max(faces, key=lambda d: d.score * (0.5 + d.box.w * d.box.h))
        found.append((plan.trim_start - 1.0 <= f.t <= plan.trim_end + 1.0, best.box))
    boxes = [b for ok, b in found if ok] or [b for _, b in found]
    if not boxes:
        return None

    def med(vals: list[float]) -> float:
        v = sorted(vals)
        n = len(v)
        return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2

    w, h = med([b.w for b in boxes]), med([b.h for b in boxes])
    # Mitte bevorzugt aus der Gesichtsspur: gleichmäßig über den Clip verteilt, während die Standbilder sich
    # an lauten Momenten häufen (dort lehnt der Streamer oft zur Seite -> Ausschnitt säße schief)
    track = [p for p in analysis.layout.track
             if fc is None or (fc.x <= p.cx <= fc.x + fc.w and fc.y <= p.cy <= fc.y + fc.h)]
    if len(track) >= 3:
        cx, cy = med([p.cx for p in track]), med([p.cy for p in track])
    else:
        cx, cy = med([b.cx for b in boxes]), med([b.cy for b in boxes])
    return Box(x=cx - w / 2, y=cy - h / 2, w=w, h=h)


def _sfx_events(plan: EditPlan, tm: TimeMap, zooms: list[compositor.ZoomOut],
                shots: list[compositor.ShotOut] | None = None) -> list[tuple[float, str]]:
    ev: list[tuple[float, str]] = []
    for e in plan.emphasis:
        if e.sfx:
            t = tm.to_output(e.t)
            if t is not None:
                ev.append((t, e.sfx))
    ev += [(z.start, "whoosh") for z in zooms]
    ev += [(s.start, TRANSITION_SFX[s.transition]) for s in shots or []
           if s.transition in TRANSITION_SFX and s.start > compositor.FIRST_FRAME_CLEAN_S]
    ev.sort()
    kept: list[tuple[float, str]] = []
    for t, name in ev:
        if any(n == name and abs(t - kt) < SFX_DEDUP_S for kt, n in kept):
            continue
        kept.append((round(t, 3), name))
    return kept


def _caption_anchor(settings: Settings, layout_kind: str) -> tuple[int, int]:
    r = settings.config.render
    W, H = int(r.width), int(r.height)
    ratio = captions.caption_y_ratio(settings.config, layout_kind)
    return round(W * captions.CAPTION_X_RATIO), round(ratio * H)


def _speech_intervals(words: list, tm: TimeMap) -> list[tuple[float, float]]:
    """Behaltene gesprochene Wörter als Ausgabe-Intervalle (für die Platzierung der Schlussfrage)."""
    out: list[tuple[float, float]] = []
    for w in words:
        if not (w.text or "").strip():
            continue
        iv = timeline.map_interval(tm, float(w.start), float(w.end))
        if iv is not None and iv[1] - iv[0] > 0.02:
            out.append((iv[0], iv[1]))
    return out


def _build_overlays(plan: EditPlan, c: CandidateIn, settings: Settings, tm: TimeMap, layout_kind: str,
                    work: Path, duration: float, first_shot: str | None = None, *,
                    speech: list[tuple[float, float]] | None = None,
                    cuts: list[float] | None = None) -> list[compositor.TimedOverlay]:
    """speech: gesprochene Wörter (Ausgabezeit) – die Schlussfrage verdeckt dann nicht die Pointe und rückt bei
    stillem Ende auf die Caption-Zeile; cuts: Shot-Wechsel (Ausgabezeit), an denen Emojis enden."""
    cfg = settings.config
    r, brand = cfg.render, cfg.brand
    W, H = int(r.width), int(r.height)
    margin = round(W * SIDE_MARGIN_RATIO)
    ovs: list[compositor.TimedOverlay] = []
    end_all = duration + 1.0

    credit_y = round(H * CREDIT_Y_RATIO)
    credit_h = 0
    if brand.show_credit:
        p = overlays.render_credit_png(c.platform_label, c.creator_name, settings, work / "credit.png")
        credit_h = _png_size(p)[1]
        ovs.append(compositor.TimedOverlay(png=p, start=0.0, end=end_all, x=margin, y=credit_y, pop=False,
                                           fade_out=0.0))
    if brand.show_watermark:
        p = overlays.render_watermark_png(settings, work / "watermark.png")
        w, h = _png_size(p)
        y = credit_y + (credit_h - h) // 2 if credit_h else credit_y
        ovs.append(compositor.TimedOverlay(png=p, start=0.0, end=end_all, x=W - margin - w, y=y, pop=False,
                                           fade_out=0.0))

    cx, cy = _caption_anchor(settings, layout_kind)
    cap_half = round(r.captions.font_size * 0.45 + r.captions.outline)
    outro, outro_start = _outro_overlay(plan, settings, work, duration, cy, cap_half, speech=speech)

    hook_end = min(float(r.hook_seconds), duration, outro_start)
    if plan.hook_style != "none" and plan.hook_text.strip() and hook_end > 0.3:
        p = overlays.render_hook_png(captions.mask_slurs(plan.hook_text), settings, work / "hook.png",
                                     compact=layout_kind == "facecam_split" or first_shot == "split")
        top = _alpha_top(p)
        ovs.append(compositor.TimedOverlay(png=p, start=0.0, end=hook_end, x=0, y=round(H * HOOK_TOP_RATIO) - top,
                                           pop=True, fade_out=0.2))

    # Emojis über der Caption-Zeile
    size = max(32, round(W * EMOJI_SIZE_RATIO))
    ey = cy - cap_half - round(EMOJI_GAP * W / 1080) - size
    times: list[tuple[float, str]] = []
    for e in plan.emphasis:
        if not e.emoji:
            continue
        t = tm.to_output(e.t)
        if t is not None and t < min(duration - 0.1, outro_start):
            times.append((t, e.emoji))
    times.sort()
    for i, (t, emo) in enumerate(times):
        end = min(t + EMOJI_SECONDS, duration)
        if i + 1 < len(times):
            end = min(end, times[i + 1][0] - 0.02)
        nxt = min((x for x in cuts or [] if x > t), default=None)
        if nxt is not None and nxt - t >= EMOJI_MIN_BEFORE_CUT:
            end = min(end, nxt - 0.02)  # nicht in den nächsten Shot hineinragen
        if end - t < 0.15:
            continue
        p = overlays.emoji_png(emo, size, cfg.paths.cache_dir)
        if p is None:
            continue
        ovs.append(compositor.TimedOverlay(png=p, start=t, end=end, x=cx - size // 2, y=ey, pop=True, fade_out=0.15))
    if outro is not None:
        ovs.append(outro)  # zuoberst
    return ovs


def _outro_overlay(plan: EditPlan, settings: Settings, work: Path, duration: float, cap_y: int, cap_half: int,
                   speech: list[tuple[float, float]] | None = None) -> tuple[compositor.TimedOverlay | None, float]:
    """Schlussfrage (plan.outro_text) für die letzten render.outro_seconds, Mitte bei y = 0.42 H (weicht der
    Caption-Zeile aus), mit kräftigem Pop-in. Gibt (Overlay, Startzeit) zurück; ohne Outro (None, duration).
    Mit speech (Ausgabe-Intervalle der Wörter): Start frühestens nach dem letzten Wort (mind. OUTRO_MIN_S
    bleiben), und ist während der Karte niemand mehr zu hören, sitzt sie auf der Caption-Zeile (Gesichter frei)."""
    r = settings.config.render
    text = (plan.outro_text or "").strip()
    if not text or r.outro_seconds <= 0:
        return None, duration
    start = max(0.0, duration - float(r.outro_seconds))
    silent_end = False
    if speech:
        last = max(e for _, e in speech)
        start = max(start, min(last + OUTRO_AFTER_SPEECH_S, duration - min(OUTRO_MIN_S, float(r.outro_seconds))))
        silent_end = not any(e > start + 0.05 and s < duration for s, e in speech)
    if duration - start < 0.6:
        return None, duration
    W, H = int(r.width), int(r.height)
    # gleiche Bedeutungsfarben wie in den Captions (PLAYSTATION blau, XBOX grün)
    p = overlays.render_outro_png(captions.mask_slurs(text), settings, work / OUTRO_PNG,
                                  word_colors=plan.word_colors or None)
    with Image.open(p) as im:
        bbox = im.getchannel("A").getbbox() if im.mode == "RGBA" else (0, 0, im.width, im.height)
    if not bbox:
        return None, duration
    top, vh = bbox[1], bbox[3] - bbox[1]
    gap = round(OUTRO_CAPTION_GAP * W / 1080)
    cy = OUTRO_Y_RATIO * H
    if silent_end:  # keine Captions mehr -> untere Bildhälfte, Reaktion oben bleibt sichtbar
        cy = min(float(cap_y), H * 0.9 - vh / 2)
    elif cy + vh / 2 > cap_y - cap_half - gap and cy - vh / 2 < cap_y + cap_half + gap:  # kollidiert mit Captions
        above = cap_y - cap_half - gap - vh / 2
        cy = above if above - vh / 2 >= H * 0.1 else cap_y + cap_half + gap + vh / 2
    ov = compositor.TimedOverlay(png=p, start=start, end=duration + 1.0, x=0, y=round(cy - vh / 2) - top, pop=True,
                                 fade_out=0.0, pop_from=OUTRO_POP_FROM, pop_s=OUTRO_POP_S)
    return ov, start


def _png_size(p: Path) -> tuple[int, int]:
    with Image.open(p) as im:
        return im.size


def _alpha_top(p: Path) -> int:
    with Image.open(p) as im:
        bbox = im.getchannel("A").getbbox() if im.mode == "RGBA" else None
    return bbox[1] if bbox else 0


def _pick_music(settings: Settings, key: str) -> Path | None:
    if not settings.config.render.music_bed:
        return None
    d = settings.config.paths.assets_dir / "music"
    try:
        tracks = sorted(p for p in d.iterdir() if p.suffix.lower() in MUSIC_EXT and p.is_file())
    except OSError:
        tracks = []
    if not tracks:
        log.info("music_bed is on, but %s has no tracks – rendering without music", d)
        return None
    return tracks[zlib.crc32(key.encode()) % len(tracks)]


# --------------------------------------------------------------------------- Thumbnail


def _thumb_time(zooms: list[compositor.ZoomOut], duration: float,
                shots: list[compositor.ShotOut] | None = None, *, peaks: list[float] | None = None) -> float:
    """Vorschaubild: lautester Moment (peaks, Ausgabezeit, stärkster zuerst), in dem das Gesicht zu sehen ist
    (face-/split-Shot) – Schreie/Lacher sind die stärksten Reaktionen; sonst erster face-Shot, erster Zoom, 40 %."""
    for pk in peaks or []:
        s = next((s for s in shots or [] if s.start <= pk < s.end), None)
        if s is not None and s.kind in ("face", "split") and 0.3 < pk < duration - 0.3:
            t = min(pk + THUMB_AFTER_PEAK_S, min(s.end, duration) - 0.05)
            if s.transition != "cut":  # nicht im Weißblitz/Punch/Glitch des Shot-Anfangs
                t = max(t, min(s.start + THUMB_AFTER_TRANSITION_S, min(s.end, duration) - 0.05))
            return t
    face = next((s for s in shots or [] if s.kind == "face" and min(s.end, duration) - s.start > 0.3), None)
    if face is not None:  # Gesicht bildfüllend = stärkstes Vorschaubild (nach dem Übergang)
        t = face.start + min(0.6, (min(face.end, duration) - face.start) / 2)
    elif zooms:
        z = zooms[0]
        t = z.start + min(0.35, (z.end - z.start) / 2)
    else:
        t = 0.4 * duration
    return max(0.0, min(t, duration - 0.1))


def _thumbnail(video: Path, t: float, out: Path) -> Path:
    tmp = out.with_name(out.stem + ".part.jpg")
    for ts in (t, 0.0):
        try:
            _ff.run([_ff.FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{ts:.3f}",
                     "-i", str(video), "-frames:v", "1", "-q:v", "2", str(tmp)], timeout=60, what="Thumbnail")
            if tmp.is_file() and tmp.stat().st_size > 0:
                os.replace(tmp, out)
                return out
        except RenderError as e:
            log.warning("Thumbnail at %.2fs failed: %s", ts, e)
    raise RenderError(f"Thumbnail konnte nicht erzeugt werden ({video.name})")
