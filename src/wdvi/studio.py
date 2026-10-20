"""Studio-Modus: Claude Code (lokal, im Chat) ist selbst der Editor – ohne API-Aufrufe.

Ablauf (siehe .claude/skills/clip/SKILL.md):
  1. prepare()  Quelle (Ausschnitt) -> <dir>/source.mp4 + analysis.json + transcript.txt + sheet.jpg
  2. grid_still() Einzelbild mit Koordinatengitter, um Boxen (focus/annotations) abzulesen
  3. Claude schreibt <dir>/plan.json (EditPlan, Quellzeiten von source.mp4)
  4. render()   sanitize_plan + render_clip -> <dir>/out/*.mp4 + preview.jpg (Frames an jedem Shot-Wechsel)

Alles bewusst token-sparsam: Transkript als kompakte Textdatei, Bildmaterial als EINE Übersicht.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from wdvi.config import Settings
from wdvi.schemas import Box, CandidateIn, EditPlan, MediaAnalysis, SourceKind, Transcript

log = logging.getLogger(__name__)

SHEET_TILES = 24
TILE_W = 320


@dataclass
class Prepared:
    dir: Path
    source: Path
    analysis: MediaAnalysis
    transcript_txt: Path
    sheet: Path


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def _cut_source(src: Path, out: Path, start: float | None, end: float | None) -> Path:
    """Ausschnitt als H.264/AAC (genauer Schnitt, kompatibel mit dem Renderer, auch bei AV1/VP9-Quellen)."""
    args: list[str] = []
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(src)]
    if end is not None:
        args += ["-t", f"{end - (start or 0.0):.3f}"]
    _ffmpeg(*args, "-c:v", "libx264", "-crf", "14", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", str(out))
    return out


def transcript_text(tr: Transcript | None) -> str:
    """Kompakt: eine Zeile je Segment, jedes Wort mit Startzeit – genau genug für Schnitte."""
    if tr is None or not tr.segments:
        return "(kein Transkript – keine Sprache erkannt)\n"
    lines = [f"Sprache: {tr.language}"]
    for seg in tr.segments:
        words = " ".join(f"{w.text.strip()}@{w.start:.2f}" for w in seg.words) or seg.text.strip()
        lines.append(f"[{seg.start:.2f}-{seg.end:.2f}] {words}")
    return "\n".join(lines) + "\n"


def contact_sheet(video: Path, out: Path, times: list[float], cols: int = 6, labels: list[str] | None = None) -> Path:
    """Frames zu den Zeiten als ein Bild (mit Zeitstempel/Label) – eine Bilddatei statt vieler."""
    from PIL import Image, ImageDraw

    tmp = out.parent / f".{out.stem}_tiles"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    tiles = []
    for i, t in enumerate(times):
        p = tmp / f"{i:03d}.jpg"
        _ffmpeg("-ss", f"{max(t, 0):.3f}", "-i", str(video), "-frames:v", "1", "-vf", f"scale={TILE_W}:-2", str(p))
        if p.exists():
            tiles.append((p, labels[i] if labels else f"{t:.1f}s"))
    if not tiles:
        raise RuntimeError(f"Keine Frames aus {video} lesbar")
    w, h = Image.open(tiles[0][0]).size
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * w, rows * h), "black")
    draw = ImageDraw.Draw(sheet)
    for k, (p, label) in enumerate(tiles):
        x, y = (k % cols) * w, (k // cols) * h
        sheet.paste(Image.open(p), (x, y))
        draw.rectangle((x, y, x + 8 + 7 * len(label), y + 15), fill="black")
        draw.text((x + 4, y + 2), label, fill="yellow")
    sheet.save(out, "JPEG", quality=85)
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def prepare(src: Path, out_dir: Path, settings: Settings, start: float | None = None,
            end: float | None = None, transcriber=None, detector=None) -> Prepared:
    """Quelle schneiden/normalisieren, transkribieren, analysieren; Textdatei + Übersichtsbild schreiben."""
    from wdvi.media import analyze as media_analyze
    from wdvi.media.faces import FaceDetector
    from wdvi.media.transcribe import Transcriber

    out_dir.mkdir(parents=True, exist_ok=True)
    source = _cut_source(Path(src), out_dir / "source.mp4", start, end)
    a = media_analyze.analyze(source, out_dir / "work", settings, transcriber or Transcriber(settings),
                              detector or FaceDetector(settings))
    (out_dir / "analysis.json").write_text(a.model_dump_json(indent=1), encoding="utf-8")
    tpath = out_dir / "transcript.txt"
    tpath.write_text(transcript_text(a.transcript), encoding="utf-8")
    dur = a.info.duration
    n = max(6, min(SHEET_TILES, int(dur / 1.5)))
    times = [dur * (i + 0.5) / n for i in range(n)]
    sheet = contact_sheet(source, out_dir / "sheet.jpg", times)
    return Prepared(out_dir, source, a, tpath, sheet)


def summary(p: Prepared) -> str:
    from wdvi.brain.editor import available_shot_kinds

    a = p.analysis
    lay = a.layout
    lines = [
        f"Quelle:      {p.source}  ({a.info.width}x{a.info.height}, {a.info.duration:.1f} s, {a.info.fps:.0f} fps)",
        f"Layout:      {lay.kind} (Konfidenz {lay.confidence:.2f})",
        f"Facecam-Box: {lay.facecam_box.model_dump() if lay.facecam_box else '-'}",
        f"Gameplay:    {lay.gameplay_box.model_dump() if lay.gameplay_box else '-'}",
        f"Shot-Arten:  {', '.join(available_shot_kinds(a))}",
        f"Laut-Peaks:  {', '.join(f'{t:.1f}s' for t in a.loudness_peaks)}",
        f"Musik:       {'nicht gemessen' if a.music_prob is None else f'{a.music_prob:.2f}'}  |  "
        f"Sprache: {a.transcript.language if a.transcript else '-'} ({a.speech_ratio:.0%} Sprachanteil)",
        f"Transkript:  {p.transcript_txt}",
        f"Übersicht:   {p.sheet}",
    ]
    if a.info.height < 720 and a.info.width < 1280:
        lines.append("⚠️  Quelle unter 720p – Ergebnis wird weich. Besser mit -S \"res:1080\" laden.")
    return "\n".join(lines)


def grid_still(source: Path, t: float, out: Path) -> Path:
    """Ein Frame in voller Breite (max 1280) mit 10x10-Gitter – zum Ablesen normalisierter Boxen."""
    from wdvi.brain.editor import grid_frame

    raw = out.with_name(out.stem + "_raw.jpg")
    _ffmpeg("-ss", f"{t:.3f}", "-i", str(source), "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", str(raw))
    g = grid_frame(raw)
    shutil.move(str(g), out)
    raw.unlink(missing_ok=True)
    return out


def _parse_box(text: str | None) -> Box | None:
    if not text:
        return None
    x, y, w, h = (float(v) for v in text.split(","))
    return Box(x=x, y=y, w=w, h=h)


def render(work_dir: Path, plan_path: Path, settings: Settings, creator: str, platform: str = "Twitch",
           creator_url: str | None = None, facecam: str | None = None, gameplay: str | None = None,
           ) -> tuple[Path, Path, EditPlan]:
    """plan.json prüfen (sanitize_plan) und rendern. Gibt (mp4, preview.jpg, bereinigter Plan) zurück."""
    from wdvi.brain.editor import sanitize_plan
    from wdvi.render.renderer import render_clip

    a = MediaAnalysis.model_validate_json((work_dir / "analysis.json").read_text(encoding="utf-8"))
    if box := _parse_box(facecam):
        a.layout.facecam_box = box
    if box := _parse_box(gameplay):
        a.layout.gameplay_box = box
    plan = EditPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    settings.config.filters.min_virality_score = 0  # im Studio entscheidest du, nicht der Score
    clean = sanitize_plan(plan, a, settings)
    if not clean.keep:
        raise ValueError(f"Plan verworfen: {clean.reject_reason or clean.reasoning}")
    (work_dir / "plan.sanitized.json").write_text(clean.model_dump_json(indent=1), encoding="utf-8")
    c = CandidateIn(source=SourceKind.youtube_moment, source_id=work_dir.name, url=str(work_dir),
                    title=clean.titles.youtube, creator_name=creator, creator_handle=creator.lower(),
                    creator_url=creator_url, platform_label=platform, created_at=datetime.now(UTC),
                    duration_s=a.info.duration)
    r = render_clip(work_dir / "source.mp4", a, clean, c, settings, work_dir / "render", work_dir / "out")
    preview = render_preview(Path(r.video_path), clean, work_dir / "preview.jpg", a.info.duration)
    return Path(r.video_path), preview, clean


def render_preview(video: Path, plan: EditPlan, out: Path, source_duration: float) -> Path:
    """Frames kurz nach jedem Shot-Wechsel (bzw. alle 2 s) + am Ende – zur Kontrolle in EINEM Bild."""
    from wdvi.render.timeline import build_timemap

    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                            str(video)], capture_output=True, text=True, check=True)
    dur = float(probe.stdout.strip() or 0)
    tm = build_timemap(plan, source_duration)
    times: list[float] = [0.05]
    for s in plan.shots:
        t = tm.to_output(s.start)
        if t is not None:
            times.append(t + 0.4)
    if len(times) < 6:
        times = [dur * (i + 0.5) / 12 for i in range(12)]
    times.append(max(dur - 0.6, 0))
    times = sorted({round(min(t, max(dur - 0.05, 0)), 2) for t in times})[:30]
    return contact_sheet(video, out, times, cols=min(8, len(times)), labels=[f"{t:.1f}s" for t in times])


def loudness(video: Path) -> str:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(video), "-af", "loudnorm=print_format=json",
                        "-f", "null", "-"], capture_output=True, text=True)
    try:
        js = json.loads(r.stderr[r.stderr.rindex("{"):])
        return f"{float(js['input_i']):.1f} LUFS, True Peak {float(js['input_tp']):.1f} dB"
    except (ValueError, KeyError):
        return "unbekannt"
