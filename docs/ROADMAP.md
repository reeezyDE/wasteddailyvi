# Roadmap & Übergabe an Claude (lokal)

> **Für Claude:** Diese Datei ist deine Aufgabenliste. Sie entstand in einer Cloud-Session mit dem Nutzer
> (Cloud und lokal können nicht direkt kommunizieren – das Repo ist die Brücke). Arbeite die Bauschritte
> **der Reihe nach** ab, je Schritt: Branch `claude/<schritt>`, Tests, `ruff`, Commit, Push, PR, kurze Meldung
> an den Nutzer (Deutsch, nicht-technisch). Hake erledigte Punkte hier ab (`[x]`) und ergänze „Stand“.
> Der Nutzer nutzt **Claude über das Abo, nicht die API** → `llm.backend: cli` ist der Standard.

## Ziel & Strategie (vom Nutzer bestätigt)

- **Zwei Kanäle ab sofort**: *Wasted Daily* (EN, @WastedDailyVI, US-Publikum) und *Wasted Daily DE* (@WastedDailyDE,
  deutsche Streamer/YouTuber). Getrennte Kanäle statt Übersetzungen (Algorithmus sortiert nach Sprache).
  Sprachneutrale Clips (Fails, Stunts, Trailer-Momente) dürfen auf beide – mit eigener Sprache für Hook/Captions/Titel
  und leicht anderem Schnitt (kein exaktes Duplikat).
- **Phase 1 – Aufwärmen** (jetzt bis nach GTA-6-Release am 19.11.2026): langsam steigern (2 → 3 → 5 → 8 Posts/Tag
  je Plattform), Freigabe im Dashboard aktiv, Qualität vor Menge.
- **Phase 2 – Bezahlte Clipping-Kampagnen** (ab ~Monat 2), speziell GTA 6.
- **Phase 3 – Weitere Clip-Kanäle/Nischen**, alles vollautomatisch skalierend.
- Jede Woche **ein Best-of-Video** (lang, 16:9 oder 9:16) mit starkem Thumbnail.
- **Budget**: Tokens dürfen über den Monat nie ausgehen (Abo-Limits!) → Claude-Aufrufe begrenzen und sparsam bauen.

## Was Tokens kostet (Kurzfassung)

Nur zwei Stellen rufen Claude: `brain.editor.plan_clip` (je Kandidat, ~7k Input inkl. 8 Frames + ~5k Output) und
`brain.editor.mine_moments` (je langem YouTube-Video). Alles andere (Discovery, Whisper, Render, Upload, Stats,
Dashboard) ist lokal und kostenlos. Pro geposteten Clip ~2–3 Bewertungen (Ablehnungen). API-Richtwert (nur
Vergleich): ~0,10–0,15 $ pro Bewertung. Mit Abo zählen stattdessen die Nutzungslimits (`/usage` in Claude Code).

## Bauschritte (in dieser Reihenfolge)

### [ ] 1. Kanal-Profile (EN + DE)
- `config.py`: neue Liste `channels:` (Profil je Kanal): `id`, `name`, `handle`, `language` ("en"/"de"),
  Quellen-Overrides (Twitch-Sprachen, YouTube-Kanal-IDs, Subreddits), Branding-Overrides (Watermark-Text),
  `publish`-Block (Plattformen, Slots, Zeitzone: EN `America/New_York`, DE `Europe/Berlin`), Token-/Post-Budget.
  Ohne `channels:` = heutiges Verhalten (ein Kanal) – rückwärtskompatibel.
- DB: `channel_id` an Candidate/Clip/Post (Migration für bestehende SQLite: Spalte mit Default anlegen).
- Pipeline: Kandidaten je Kanal routen (Sprache der Quelle). Sprachneutrale Clips (wenig Sprache, `speech_ratio` klein)
  → Claude liefert Texte **für beide Sprachen im selben Aufruf** (EditPlan um `translations` o. ä. erweitern), Renderer
  rendert je Kanal mit eigener Sprache. Keine identischen Uploads (anderer Hook/Schnitt-Start).
- Prompts: Captions/Hook/Titel in der Kanalsprache; deutsche Titel natürlich, nicht übersetzt klingend.
- Dashboard: Kanal-Filter/Chip je Clip; Verbindungen (OAuth) je Kanal und Plattform (TokenStore-Name mit Kanal-ID).
- Fertig, wenn: zwei Profile in `config.example.yaml` dokumentiert, Tests für Routing/Slots/Tokens je Kanal grün.

### [ ] 2. Zweistufige Bewertung + Abo-Budget
- Stufe A (billig): Text-only-Vorbewertung (Metadaten + Transkript, **keine Bilder**) mit `llm.triage_model`
  (Standard `claude-haiku-5-5`, im CLI-Backend `--model claude-haiku-5-5`) → Score + Kurzbegründung; unter
  `filters.triage_min_score` (Start 50) verwerfen. Stufe B (Opus wie heute) nur für die übrigen.
- **Abo-Schutz**: neues `llm.max_calls_per_day` (je Modell zählen, in DB/JobRun persistieren, nicht nur im Speicher);
  bei Erreichen pausieren statt Kandidaten zu verbrennen. Dashboard/Health zeigt Verbrauch heute/Woche.
- Stichprobe: 10 % der Triage-Ablehnungen trotzdem an Opus (Log-Vergleich), um Fehl-Ablehnungen zu erkennen.
- Fertig, wenn: Tests mit Fake-LLM für beide Stufen, Budget-Pause, Stichprobe.

### [ ] 3. Automatische Steigerung (Warm-up-Rampe)
- Je Kanal+Plattform: `ramp: [2, 3, 5, 8]` Posts/Tag, Stufenwechsel frühestens nach 7 Tagen und nur, wenn die
  Median-Views der letzten 7 Tage (24-h-Metrik) nicht fallen; bei Einbruch eine Stufe zurück. Slots ergeben sich aus
  der aktuellen Stufe (gleichmäßig über die Kanal-Zeitzone verteilt). YouTube-Limit `max_uploads_per_day` respektieren.
- Fertig, wenn: Tests mit simulierten Metriken (hoch/runter/halten).

### [ ] 4. Musik in drei Stufen (statt „jede Musik = ablehnen“)
- Problem: fast jeder GTA-Clip hat Radio-Musik → heutiger Filter (`filters.music_policy: reject`) verwirft zu viel.
- Messen: Musik **relativ zur Sprache** (Lautheit/Anteil), nicht nur Wahrscheinlichkeit (`media/audio_tags.py`).
- Stufen: (a) leise Spielmusik unter Sprache → erlauben (Risiko: Claim, kein Strike); (b) laute/zentrale Musik →
  **Stimme isolieren** (Speech-Enhancement, z. B. DeepFilterNet oder Demucs-Vocals; optionales Extra wie `[music]`),
  Spielgeräusche teils weg → eigene SFX + optional lizenzfreier Track aus `assets/music/` leise darunter;
  (c) Musik ist der Inhalt (Singen, Konzert) → ablehnen.
- Erst an echten Clips anhören/prüfen, dann scharf schalten (`music_policy: tiered`).

### [ ] 5. Strike-Frühwarnung
- Periodisch Status aller Uploads prüfen (YouTube `videos.list` status/`rejectionReason`/`uploadStatus`,
  TikTok/Instagram Status-Endpunkte). Bei Copyright-Entfernung/Sperre: Creator automatisch auf Sperrliste,
  geplante Posts des Creators stoppen, Kanal 48 h pausieren, rote Warnung im Dashboard + `/health`.
- Fertig, wenn: Tests mit gefälschten API-Antworten (entfernt, gesperrt, ok).

### [ ] 6. Erlaubnis-Liste & Streamer-Anfrage
- `CreatorRule.rule == "allow"` bevorzugen (Prescore-Bonus), unbekannte Creator nur ab höherem Virality-Score
  (`filters.min_virality_score_unknown`, z. B. 80). Dashboard: Liste „Erlaubnis liegt vor“ pflegen.
- Vorlage (DE + EN) für eine kurze DM an Streamer („Wir clippen deine Streams mit Credit + Link – okay?“) in
  `docs/BETRIEB.md`.

### [ ] 7. Wöchentliches Best-of mit Thumbnail
- Job (z. B. sonntags): Top-Clips der Woche je Kanal nach Views → Claude (**ein** Aufruf) wählt 8–12, Reihenfolge
  (stärkster zuerst, Spannungsbogen), Titel, Beschreibung, Kapitel, Thumbnail-Text.
- Render: lange Compilation (16:9 1920×1080 aus den Originalquellen, nicht aus den 9:16-Shorts) mit Intro-Hook,
  Übergängen, Credit-Einblendung je Clip, Kapitelmarken; Upload als normales YouTube-Video.
- Thumbnail **ohne Kosten** aus den besten Frames rendern: großes Reaktionsgesicht (freigestellt/zugeschnitten),
  kräftiger Text (2–4 Wörter, Markenfarben), Pfeil/Kreis, hoher Kontrast; 1280×720 JPG. YouTube `thumbnails.set`
  (braucht verifizierten Kanal).

### [ ] 8. Dashboard als Kommandozentrale
Der Nutzer will alles im eigenen Dashboard sehen (nicht im Terminal). Ausbau von `src/wdvi/dashboard/`:
- **Übersicht/Startseite**: je Kanal (EN/DE) und Plattform: Posts heute/Woche, Views/Likes-Verlauf (Diagramme),
  beste Clips, Warm-up-Stufe, nächste geplante Posts, Claude-Verbrauch (Aufrufe heute/Woche vs. Limit),
  Warnungen (Strikes, Fehler, fehlende Zugangsdaten) – alles auf einen Blick, handytauglich.
- **Steuerung**: Pipeline pausieren/fortsetzen, Kanal an/aus, Posts/Tag-Stufe manuell setzen, Link einfügen →
  Clip erzeugen (statt `process-url` im Terminal), Best-of manuell anstoßen.
- **Einrichtung im Browser**: Zugangsdaten/Quellen (Twitch-Keys, YouTube-Kanal-IDs, Subreddits) über Formulare statt
  `.env`/`config.yaml` von Hand (Secrets nur verdeckt anzeigen, sicher speichern).
- Läuft weiter lokal (`wdvi run`, Start per Desktop-Verknüpfung); später optional von unterwegs über Tailscale.

## Später (nicht jetzt bauen)

- Phase 2: Kampagnen-Finder (z. B. Whop Content Rewards) für GTA-6-Kampagnen, Kampagnen-Regeln (Länge, Pflicht-Hashtags,
  Text) in den Schnitt übernehmen. Einreichen anfangs per Klick des Nutzers (Nutzungsbedingungen je Plattform prüfen).
- Phase 3: beliebig viele Kanal-Profile/Nischen; ab ~20 Clips/Tag GPU-Rechner.
- Feinschliff: Hook-Varianten-A/B, Loop-Schnitt, Tracking für bewegte Annotations, Batch-API für Nicht-Eiliges.

## Leitplanken (bei allen Schritten einhalten)

- Kein Spam/keine Bot-Netzwerke: jeder Kanal klar als eigener Fan-Kanal erkennbar, keine gefälschte Interaktion,
  nie Sperren mit neuen Kanälen umgehen. Offizielle APIs.
- Credits immer; Opt-out sofort respektieren; Leaks ablehnen; Clips < 60 s.
- Plattform-Limits: YouTube ~6 Uploads/Tag ohne Quota-Antrag, TikTok öffentlich erst nach App-Audit.
- Rückwärtskompatibel bauen (alte `config.yaml` muss weiter laufen), v1/v2-Render-Tests bleiben grün.

## To-dos für den Nutzer (nicht für Claude)

- [ ] PR #2 auf GitHub mergen (enthält #1) – sonst fehlt der aktuelle Stand auf `main`.
- [ ] Handles reservieren: YouTube/TikTok/Instagram @WastedDailyVI und @WastedDailyDE.
- [x] Lokal eingerichtet (Claude Desktop App, Projektordner `C:\Users\eljay\Videos\clipping\wasteddailyvi`).
- [ ] Desktop-Verknüpfung anlegen: Claude bitten, `scripts\install-shortcut.ps1` auszuführen (optional `-Autostart`).
- [ ] Zugangsdaten nach `docs/SETUP.md` (zuerst Twitch), Claude-CLI ist über das Abo angemeldet.
- [ ] YouTube-API-Audit und TikTok-App-Audit beantragen.

## Stand

- 2026-10-20 (Cloud-Session): Pipeline, Edit-Engine v2, Studio-Modus, Übergabe-Doku fertig; 957 Tests grün.
  Plattform-Uploads, Docker/NVENC und Windows-Setup noch nie live getestet.
