---
name: clip
description: Einen viralen Short (9:16) aus einem Video/Link schneiden – Claude Code ist selbst der Editor (Studio-Modus, keine API-Kosten). Benutzen, wenn der Nutzer einen Clip, Short, YouTube-/Twitch-Link oder eine Videodatei schneiden lassen will.
---

# /clip – Short schneiden im Studio-Modus

Du bist Head-Editor eines Clip-Kanals. Ziel: maximale Reichweite & Retention (Niveau großer Clip-Kanäle).
Arbeite token-sparsam: nur `transcript.txt`, `sheet.jpg`, gezielte `frame`-Bilder und `preview.jpg` ansehen.

## 1. Quelle besorgen (lokal)

```powershell
# beste Qualität (1080p) – 360p sieht hochskaliert weich aus
yt-dlp -S "res:1080,vcodec:h264,acodec:aac" --merge-output-format mp4 -o quelle.mp4 "<LINK>"
# altersbeschränkt: --cookies-from-browser firefox   |   nur Ausschnitt: --download-sections "*12:30-13:40"
```
Lange Videos: erst Metadaten/Transkript ansehen (`yt-dlp --skip-download --write-auto-subs --sub-format json3 --write-info-json`),
Heatmap (`heatmap` in der .info.json = „Am häufigsten wiederholt“) nutzen, dann nur den Ausschnitt (±10 s) laden.

## 2. Vorbereiten

```powershell
wdvi studio prepare quelle.mp4 -o clips\<name> [--start 2420 --end 2520]
```
Liefert `source.mp4` (Zeit 0 = Ausschnittbeginn), `transcript.txt` (Wort@Sekunde), `sheet.jpg` (Übersicht),
`analysis.json` und eine Zusammenfassung (Layout, Facecam-Box, verfügbare Shot-Arten, Laut-Peaks, Qualität).
Für genaue Boxen (focus/annotations): `wdvi studio frame clips\<name> 13.0` → Bild mit 0–1-Gitter.

## 3. plan.json schreiben (Quellzeiten von source.mp4)

```json
{
  "keep": true, "virality_score": 85, "reasoning": "…",
  "trim_start": 10.5, "trim_end": 74.3,
  "remove_ranges": [[20.3, 22.4], [26.6, 33.5]],
  "hook_style": "banner", "hook_text": "30 MIN OF LOSING… THEN THIS",
  "shots": [
    {"start": 10.5, "end": 11.8, "kind": "face"},
    {"start": 11.8, "end": 13.2, "kind": "gameplay", "focus": {"x": 0.38, "y": 0.45, "w": 0.24, "h": 0.12}, "transition": "flash"},
    {"start": 13.2, "end": 15.0, "kind": "split", "transition": "punch"}
  ],
  "annotations": [{"start": 11.3, "end": 13.5, "kind": "circle", "box": {"x": 0.42, "y": 0.47, "w": 0.16, "h": 0.08}, "color": "red", "label": "10 SEC FEAR"}],
  "word_colors": [{"word": "resist", "color": "red"}, {"word": "dead", "color": "green"}],
  "highlight_words": ["disgusting"],
  "zooms": [{"start": 72.0, "end": 74.3, "scale": 1.25, "focus": "face"}],
  "emphasis": [{"t": 72.5, "emoji": "💀", "sfx": "boom", "shake": true}],
  "outro_text": "IS WARLOCK THE MOST BROKEN CLASS?",
  "titles": {"youtube": "…", "tiktok": "…", "instagram": "…"},
  "descriptions": {"youtube": "…", "tiktok": "…", "instagram": "…"},
  "hashtags": ["wow", "pikaboo"], "tags": ["world of warcraft", "pikaboo"], "debate_score": 70
}
```
Shot-Arten: `face` (Kamera bildfüllend), `split` (Kamera oben, Spiel unten), `gameplay` (Spiel bildfüllend, optional `focus`),
`fit` (ganzes Bild, unscharfer Hintergrund). Übergänge: `cut`, `flash`, `punch`, `glitch`. Farben: white, pink, orange,
yellow, green, blue, purple, red. SFX: pop, whoosh, boom, ding, riser, record_scratch.

## Redaktionelle Regeln (gelernt)

- **Hook in Sekunde 0–1**: erster Frame = stärkstes Bild (meist das reagierende Gesicht). Ist der erste gesprochene Satz
  selbst eine starke Frage/Behauptung → `hook_style: "none"`, sonst Banner (max 6 Wörter, Neugier, nicht spoilern).
- **Länge 12–25 s** (hartes Max 59 s). Tote Luft > 0,6 s raus, Comedy-Timing behalten. Ende = Pointe/Reaktion, möglichst loopfähig.
- **Shots wechseln alle 1–3 s** (face ↔ split ↔ gameplay). Reaction-Clips: Kamera groß zeigen – Emotion schlägt Schärfe.
  Bereits geschnittene Montagen (eigene Gesichts-Zooms/Memes) → eher `fit`, da feste Boxen dort danebenliegen.
- **Annotations nur auf feste Bildelemente** (HUD, Debuff-Timer, Lebensbalken, Namensschilder, Schilder, Chat) –
  nie auf schwebenden Kampftext oder Dinge, die sich bewegen. 0–3 pro Clip.
- **Bedeutungsfarben**: Marken/Konsolen in ihren Farben (PlayStation blau, Xbox grün), Geld grün, Gefahr rot, Spielname pink.
- **Untertitel im Quellvideo** (z. B. Trailer): Spieldialog nicht doppelt captionen – nur die Worte des Streamers.
- **Outro-Frage** provoziert Kommentare (Streitfrage), Titel = kurze Frage/Behauptung + 1–2 Emojis.
- Whisper-Fehler im Transkript korrigieren ist erlaubt (Wörter in `analysis.json` anpassen, Zeiten behalten).
- Ablehnen: Leaks/unveröffentlichtes Material, NSFW, Hass, Doxxing, Clips, die Vorwissen brauchen.

## 4. Rendern & prüfen

```powershell
wdvi studio render clips\<name> --creator "Pikaboo" --platform YouTube [--facecam x,y,w,h] [--gameplay x,y,w,h] [--no-watermark]
```
Dann **`preview.jpg` ansehen** (ein Frame je Shot-Wechsel) und wie ein pingeliger Editor prüfen:
Kopf nicht angeschnitten, Markierung trifft das Detail, Captions lesbar und verdecken nichts Wichtiges, Outro lesbar,
Lautheit ≈ −14 LUFS (steht in der Ausgabe). Fehler → `plan.json` anpassen und erneut rendern (max. 3 Runden).
Bei Fehlern im Renderer selbst: Code fixen + Test schreiben (siehe CLAUDE.md).

## 5. Abgeben

Pfad der MP4 nennen + Titel-Vorschläge je Plattform + Hinweis auf Schwächen (z. B. 360p-Quelle).
