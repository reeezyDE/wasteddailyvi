# Entwicklung – Architektur, Entscheidungen, Stand

Nur bei Bedarf lesen (CLAUDE.md ist die Kurzfassung).

## Datenfluss

```
sources/*  --CandidateIn-->  pipeline.discover  -> DB Candidate (prescore)
pipeline.process_candidate:
  media.download -> media.analyze (probe, Whisper, Musik, Laut-Peaks, Layout/Gesichter, Frames, pHash)
  -> Filter (Sprache, Musik, Dedup) -> brain.editor.plan_clip (Claude -> EditPlan) -> sanitize_plan
  -> render.renderer.render_clip -> DB Clip (pending_review)
dashboard: Review -> approve -> pipeline.schedule_approved (publish/slots) -> publish_due (publish/*) -> fetch_stats
studio: prepare -> (Claude Code schreibt plan.json) -> render   [ohne DB, ohne API]
```

## Rendering (render/)

1. `timeline.build_timemap`: behaltene Quellabschnitte (trim minus remove_ranges) ↔ Ausgabezeit.
2. `renderer._cut_segments`: ffmpeg schneidet/concat → `cut.mp4` (CFR, 10-ms-Audio-Fades).
3. `captions.build_ass`: Wort-für-Wort-ASS (aktives Wort Pop, Bedeutungsfarben, Glow, Zensur); Position je Layout
   (`caption_y_ratio`: split → Naht, fit → unter dem Bild, shots/sonst → `captions.y_ratio`).
4. `overlays`: Hook-Pill, Credit, Wasserzeichen, Emojis (Twemoji SVG→cairosvg, Fallback 72-px-PNG), Outro-Karte.
5. `audio.build_audio`: synthetische SFX, optional Musik mit Ducking, zweistufiges loudnorm (−14 LUFS).
6. `compositor.compose_video`: Frame-Schleife (numpy/OpenCV) → Layout/Shots, Zooms, Übergänge, Shake/Flash,
   Annotations, Farbkorrektur, Overlays → ffmpeg (ASS einbrennen, NVENC oder libx264).

Shots: `face` (Gesichtsausschnitt aus facecam_box/Gesichtsspur, `renderer._face_box` nimmt die Mitte aus der
gleichmäßig verteilten Gesichtsspur), `split`, `gameplay` (optional focus), `fit`. Ohne Shots: ein Layout (v1).

## Wichtige Entscheidungen (mit Grund)

- **Plan-Zeiten = Quellzeiten.** Claude sieht Quell-Timestamps; Mapping macht der Renderer.
- **v1-Kompatibilität**: leere v2-Felder → byte-/pixelgleiche Ausgabe (Tests vergleichen das).
- **Montagen (schon geschnittene Videos)** → `fit`, weil feste Facecam-Boxen bei Vollbild-Gesichts-Zooms danebenliegen.
- **Reaction-Clips** → Kamera groß (split/face), auch wenn die Quelle weich ist: Emotion zählt mehr als Schärfe.
- **Annotations nur auf statische Elemente**; bewegter Kampftext („Miss“) wanderte aus dem Kreis.
- **Captions-Zensur** gängiger Flüche (F*CKING) standardmäßig an (Reichweite); schwere Beleidigungen immer maskiert.
- **Trailer mit eingebrannten Untertiteln**: nur Streamer-Worte captionen.
- **#GTA6** nur bei GTA-Inhalten; YouTube-Beschreibung enthält das Transkript (Suche).
- **Clip-Länge** Ziel 12–25 s, hart < 60 s (Shorts > 1 min mit Content-ID-Claim werden blockiert).
- **Musik im Quellclip** = größtes Copyright-Risiko → Standard: ablehnen.

## Bekannte Grenzen / Ideen (Roadmap)

- Annotations/Face-Boxen sind statisch je Shot – Tracking bewegter Objekte fehlt.
- Facecam-Erkennung bei freigestellten Streamern (ohne Rahmen) ist heuristisch; `--facecam x,y,w,h` überschreibt.
- Hook-Varianten-A/B und automatischer Loop-Schnitt sind noch nicht gebaut.
- YouTube blockt Rechenzentrums-IPs (Bot-Check/403) – Downloads daher lokal oder mit Cookies.
- Plattform-APIs (TikTok/Instagram/YouTube-Upload) sind nach Doku gebaut, aber noch nie live gelaufen.
- NVENC-Pfad und Docker-Image wurden noch nicht auf echter GPU-Hardware getestet.

## Historie (Kurzfassung)

1. Grundpipeline (Quellen, Analyse, Claude-Editor, Renderer, Publisher, Dashboard, Docker) – Review mit 61 Befunden behoben.
2. Fit-Layout: Captions unter dem Bild, hellerer Hintergrund (PR #1).
3. Edit-Engine v2 nach Analyse eines viralen Referenz-Shorts (IShowSpeed, 866k Views/4 Tage): Shot-Wechsel,
   Annotations, Bedeutungsfarben, gesprochener Hook, Outro-Frage, Farbkorrektur, Zensur (PR #2).
4. Studio-Modus + diese Doku, damit Claude Code lokal weiterarbeiten kann.

## Tests

`tests/` (~950 Tests, ~3 min). Kein Netzwerk; Fakes für Whisper/Claude/HTTP. Gemeinsame Fixtures in
`tests/conftest.py` (synthetisches Testvideo, Settings mit tmp-Datenordner). Visuelle Prüfung: Frames rendern und
als Übersicht ansehen (siehe Skill /clip, Schritt 4).
