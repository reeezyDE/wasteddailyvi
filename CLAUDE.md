# WastedDailyVI – Projektkarte für Claude

Selbst gehostete Pipeline für einen Clip-Kanal (Fan-Kanal „Wasted Daily | GTA 6 Clips“, @WastedDailyVI):
virale Clips finden → Claude plant den Schnitt → Renderer macht 9:16-Shorts im Submagic-/Clip-Kanal-Stil →
Review-Dashboard → Posten auf YouTube Shorts / TikTok / Instagram. Läuft 24/7 lokal oder auf einem VPS.
Der Nutzer ist kein Entwickler: antworte auf **Deutsch**, kurz, mit konkreten Befehlen für Windows-PowerShell.
Claude läuft beim Nutzer über das **Abo** (nicht API): `llm.backend: cli`, Nutzungslimits schonen.

## ➡️ Nächste Aufgaben: `docs/ROADMAP.md`

Dort stehen Strategie (EN- + DE-Kanal, Warm-up, später Kampagnen) und die **Bauschritte 1–7** mit Abnahmekriterien.
Wenn der Nutzer „weiter“, „mach weiter“ o. ä. sagt: nächsten offenen Bauschritt aus der Roadmap umsetzen.

## Schnell orientieren (statt den Code zu durchsuchen)

| Bereich | Dateien | Inhalt |
|---|---|---|
| Verträge | `src/wdvi/schemas.py`, `config.py`, `models.py` | EditPlan (Shots, Annotations, WordColors, Zooms, Emphasis, Outro), Config, DB-Tabellen |
| Quellen | `sources/twitch.py`, `kick.py`, `youtube.py`, `reddit.py`, `scoring.py` | Discovery + Vorbewertung |
| Medien | `media/download.py`, `transcribe.py` (faster-whisper), `faces.py`, `analyze.py` | Download, Transkript, Layout-Erkennung |
| Claude | `brain/prompts.py` (System-Prompts), `editor.py` (Prompt-Bau + `sanitize_plan`), `claude.py` (API/CLI-Backend) | |
| Rendering | `render/renderer.py` (Ablauf), `compositor.py` (Frames/Shots/Effekte), `captions.py` (ASS), `overlays.py` (PNGs), `audio.py` | |
| Studio | `studio.py` + `wdvi studio …` | Claude Code ist selbst der Editor (kein API-Key) |
| Betrieb | `pipeline.py`, `scheduler.py`, `cli.py`, `dashboard/`, `publish/` | |

Details zu Architektur, Entscheidungen und Historie: `docs/ENTWICKLUNG.md` (nur bei Bedarf lesen).

## Befehle

```bash
.venv/bin/pytest -o addopts="" -q          # alle Tests (~3 min); einzelne Datei: tests/test_render_v2_core.py
.venv/bin/ruff check src tests             # Lint (Pflicht vor jedem Commit)
wdvi doctor                                # Systemcheck (ffmpeg, GPU, Schlüssel, Modelle)
wdvi studio prepare <video> -o <dir> [--start s --end s]   # Clip von Hand schneiden: siehe Skill /clip
wdvi render-demo                           # Install-Test ohne Claude
```
Windows ohne WSL: `.venv\Scripts\pytest`, `.venv\Scripts\wdvi`. Ersteinrichtung: `scripts/dev-setup.ps1` bzw. `.sh`.

## Regeln

- **Clip schneiden lassen?** → Skill `/clip` benutzen (`.claude/skills/clip/SKILL.md`). Du bist dann der Editor;
  schreibe `plan.json` selbst, keine API-Aufrufe.
- Plan-Zeiten sind **Quellzeiten** von `source.mp4`; der Renderer mappt über `TimeMap` (Schnitte via `remove_ranges`).
- Leere v2-Felder müssen exakt wie v1 rendern (es gibt byte-/pixelgenaue Tests). Neue Felder immer optional.
- Code-Stil: Type Hints, sparsame **deutsche** Kommentare, deutsche Nutzermeldungen (Logs dürfen englisch sein),
  Zeilenlänge 110, ruff-sauber. Tests ohne Netzwerk (respx/Fakes); ffmpeg-Renders als `@pytest.mark.slow`.
- Keine Secrets loggen/committen (`.env`, `config.yaml`, `data/`, `rohclips/*.mp4` sind ignoriert).
- Rechtliches/Plattform: Credits immer, Opt-out-Liste respektieren, Leaks ablehnen, Musik-Clips meiden,
  Flüche in Captions zensiert (`render.caption_censor`).

## Token sparen (ohne Qualitätsverlust)

- Diese Karte + `docs/ENTWICKLUNG.md` statt Code-Exploration; gezielt mit Grep/Zeilenbereichen lesen.
- Bildmaterial immer als **eine** Übersicht (`sheet.jpg`, `preview.jpg`) ansehen, nicht Frame für Frame.
- Transkript aus `transcript.txt` (kompakt, Wort@Zeit) statt JSON.
- Im Dauerbetrieb: `llm.backend: cli` nutzt das Claude-Abo (`claude -p`), `api` den API-Key; System-Prompt ist
  gecacht, Claude sieht nur Kandidaten, die die billigen Filter bestanden haben.
