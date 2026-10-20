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
from wdvi.schemas import CandidateIn, EditPlan, LayoutHint, MediaAnalysis, RenderResult, TimeMap, TrackPoint

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

    # 3) Layout + Captions
    layout = _resolve_layout(plan, analysis, tm)
    words = analysis.transcript.words() if analysis.transcript else []
    ass_path: Path | None = None
    if words:
        ass_path = captions.build_ass(words, tm, plan.highlight_words, layout.kind, settings, work / "captions.ass")

    # 4) Zooms, Overlays, 5) SFX
    zooms = _map_zooms(plan, tm)
    ovs = _build_overlays(plan, c, settings, tm, layout.kind, work, cut_dur)
    sfx_events = _sfx_events(plan, tm, zooms) if r.sfx else []

    wav = audio.build_audio(cut, sfx_events, settings, work / "audio.wav", music_track=_pick_music(settings, key))

    # 6) Komposition + Encoding (NVENC-Fehler -> einmal mit libx264 wiederholen)
    spec = compositor.CompositionSpec(layout=layout, zooms=zooms, overlays=ovs)
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
    _thumbnail(out_mp4, _thumb_time(zooms, duration), thumb)

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


def _sfx_events(plan: EditPlan, tm: TimeMap, zooms: list[compositor.ZoomOut]) -> list[tuple[float, str]]:
    ev: list[tuple[float, str]] = []
    for e in plan.emphasis:
        if e.sfx:
            t = tm.to_output(e.t)
            if t is not None:
                ev.append((t, e.sfx))
    ev += [(z.start, "whoosh") for z in zooms]
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


def _build_overlays(plan: EditPlan, c: CandidateIn, settings: Settings, tm: TimeMap, layout_kind: str,
                    work: Path, duration: float) -> list[compositor.TimedOverlay]:
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

    hook_end = min(float(r.hook_seconds), duration)
    if plan.hook_text.strip() and hook_end > 0.3:
        p = overlays.render_hook_png(captions.mask_slurs(plan.hook_text), settings, work / "hook.png",
                                     compact=layout_kind == "facecam_split")
        top = _alpha_top(p)
        ovs.append(compositor.TimedOverlay(png=p, start=0.0, end=hook_end, x=0, y=round(H * HOOK_TOP_RATIO) - top,
                                           pop=True, fade_out=0.2))

    # Emojis über der Caption-Zeile
    size = max(32, round(W * EMOJI_SIZE_RATIO))
    cx, cy = _caption_anchor(settings, layout_kind)
    cap_half = round(r.captions.font_size * 0.45 + r.captions.outline)
    ey = cy - cap_half - round(EMOJI_GAP * W / 1080) - size
    times: list[tuple[float, str]] = []
    for e in plan.emphasis:
        if not e.emoji:
            continue
        t = tm.to_output(e.t)
        if t is not None and t < duration - 0.1:
            times.append((t, e.emoji))
    times.sort()
    for i, (t, emo) in enumerate(times):
        end = min(t + EMOJI_SECONDS, duration)
        if i + 1 < len(times):
            end = min(end, times[i + 1][0] - 0.02)
        if end - t < 0.15:
            continue
        p = overlays.emoji_png(emo, size, cfg.paths.cache_dir)
        if p is None:
            continue
        ovs.append(compositor.TimedOverlay(png=p, start=t, end=end, x=cx - size // 2, y=ey, pop=True, fade_out=0.15))
    return ovs


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


def _thumb_time(zooms: list[compositor.ZoomOut], duration: float) -> float:
    if zooms:
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
