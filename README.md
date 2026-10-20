# WastedDailyVI – „Wasted Daily | GTA 6 Clips“

**Dein eigener, selbst gehosteter Clip-Kanal-Autopilot für GTA 6.**
WastedDailyVI sucht rund um die Uhr auf Twitch, Kick, YouTube und Reddit nach viralen GTA-6-Momenten,
lässt **Claude** (Anthropic, Modell `claude-opus-5-5`) jeden Clip bewerten und den Schnitt planen,
rendert daraus fertige **9:16-Shorts in Submagic-Qualität** und legt sie dir zur Freigabe in ein
Dashboard. Nach deinem „Go“ plant es die Videos zu den besten Uhrzeiten ein und postet sie auf
**YouTube Shorts, TikTok und Instagram Reels** – für den Fan-Kanal **@WastedDailyVI**.

Läuft 24/7 in Docker auf deinem Windows- oder Linux-PC mit NVIDIA-Grafikkarte.

---

## Inhalt

1. [Was es kann](#was-es-kann)
2. [So funktioniert es](#so-funktioniert-es)
3. [Voraussetzungen](#voraussetzungen)
4. [Schnellstart Windows](#schnellstart-windows)
5. [Schnellstart Linux](#schnellstart-linux)
6. [Erster Start (beide Systeme)](#erster-start-beide-systeme)
7. [Kosten](#kosten)
8. [Spielregeln – bitte lesen!](#spielregeln--bitte-lesen)
9. [Fehlerbehebung](#fehlerbehebung)
10. [Für Entwickler](#für-entwickler)

Ausführliche Anleitungen:

- **[docs/SETUP.md](docs/SETUP.md)** – alle Zugangsdaten Schritt für Schritt (Twitch, Reddit, YouTube, TikTok, Instagram, Claude)
- **[docs/BETRIEB.md](docs/BETRIEB.md)** – täglicher Betrieb, Freigabe am Handy, Status-Bedeutungen, Takedowns, Updates

---

## Was es kann

| Funktion | Kennst du von | WastedDailyVI |
|---|---|---|
| Animierte Wort-für-Wort-Untertitel (aktives Wort farbig + „Pop“, Schlüsselwörter hervorgehoben) | Submagic | ✅ Whisper-Wortzeitstempel, eigene Neon-Vice-City-Farben |
| Hook-Titel in den ersten 3 Sekunden | Submagic, Opus Clip | ✅ von Claude formuliert (max. 6 Wörter) |
| Weiche Zooms auf die entscheidenden Momente | Submagic „Magic Zooms“ | ✅ Claude plant Zooms, mit „Whoosh“-Sound |
| Jump-Cuts (langweilige Stellen raus) | Submagic „Remove Silence“ | ✅ Claude schneidet, nie über 59 s |
| Emojis und Soundeffekte an Höhepunkten | Submagic „Auto Emojis / SFX“ | ✅ 💀🔥😂 + boom/pop/ding … |
| Hochformat mit Gesichtserkennung, Facecam als Split-Screen | Opus Clip „AI Reframe“ | ✅ Facecam oben, Gameplay unten – automatisch erkannt |
| Viral-Score mit Begründung | Opus Clip „Virality Score“ | ✅ 0–100 von Claude, inkl. Ablehnungsgrund |
| Aus langen Videos die besten Momente finden | Opus Clip „ClipAnything“ | ✅ YouTube: Transkript + „Am häufigsten wiederholt“-Heatmap |
| Planen und automatisch posten | Opus Clip / Submagic Scheduler | ✅ YouTube Shorts, TikTok (Entwurf oder direkt), Instagram Reels |
| Trend-Monitoring (wo passiert gerade etwas?) | – | ✅ Twitch-Kategorie, Kick, YouTube-Kanäle, r/GTA6 |
| Lernt aus deinen Zahlen | – | ✅ Aufrufe nach 24 h/72 h fließen in Claudes Bewertung ein |
| Schutz vor Ärger | – | ✅ Creator-Credit, Opt-out-Liste, Leak-Filter, Musik-Erkennung, Duplikat-Erkennung |

Nicht enthalten (für Gameplay-Clips nicht nötig): KI-Stimmen, B-Roll aus Stock-Videos.

## So funktioniert es

```
                 ┌───────────── alle 10–20 Minuten ─────────────┐
   Twitch ──┐    │                                              │
   Kick ────┤    │  1. ENTDECKEN   neue Clips + Aufrufzahlen    │
   YouTube ─┤───▶│     Vorbewertung (Aufrufe pro Stunde),       │
   Reddit ──┘    │     Block-Liste, Sprache, Länge, Alter       │
                 └──────────────────────┬───────────────────────┘
                                        ▼  (die besten zuerst, alle 2 Minuten)
                 ┌──────────────────────────────────────────────┐
                 │  2. ANALYSIEREN  Download (yt-dlp) · Whisper │
                 │     (GPU) · Musik-Erkennung · Gesichter /    │
                 │     Facecam · Lautstärke-Spitzen · Duplikate │
                 └──────────────────────┬───────────────────────┘
                                        ▼
                 ┌──────────────────────────────────────────────┐
                 │  3. CLAUDE  „Ist das viral?“ Score 0–100,    │
                 │     Schnitt, Zooms, Emojis, Hook, Titel,     │
                 │     Hashtags je Plattform – oder Ablehnung   │
                 └──────────────────────┬───────────────────────┘
                                        ▼
                 ┌──────────────────────────────────────────────┐
                 │  4. RENDERN  ffmpeg + NVENC (GPU): 1080x1920,│
                 │     Captions, Hook, Credit, Wasserzeichen    │
                 └──────────────────────┬───────────────────────┘
                                        ▼
   📱 DU  ◀──── 5. REVIEW im Dashboard (http://localhost:8420): ansehen, Titel anpassen, freigeben
                                        ▼
                 ┌──────────────────────────────────────────────┐
                 │  6. PLANEN & POSTEN  feste Uhrzeiten (US-Ost)│
                 │     YouTube · TikTok · Instagram             │
                 └──────────────────────┬───────────────────────┘
                                        ▼
                 7. STATISTIK  Aufrufe nach 24 h / 72 h ──▶ Beispiele für Claude (Feedback-Schleife)

   Alles in EINEM Docker-Container · Daten in ./data (SQLite-Datenbank, Videos, Tokens)
```

## Voraussetzungen

- **PC mit NVIDIA-Grafikkarte** (empfohlen: RTX 2060 oder neuer – auch RTX 50xx –, ≥ 8 GB Grafikspeicher)
  und aktuellem Treiber (**Version 570 oder neuer**, wegen CUDA 12.8). Ohne GPU geht es auch, aber
  Transkription und Rendern dauern ein Vielfaches – siehe [Ohne GPU](#ohne-gpu-nur-cpu).
- 16 GB RAM, **mindestens 40 GB freier Speicher** (Docker-Image ~12 GB, Whisper-Modell ~1,6 GB, Clips –
  gepostete Videos werden nach 14 Tagen automatisch gelöscht)
- Der PC sollte durchlaufen (Energiesparmodus aus), sonst wird zwischendurch nichts gepostet.
- Konten: Anthropic (Claude), Twitch-Entwickler, Google Cloud (YouTube), TikTok for Developers,
  Meta for Developers (Instagram). Alles kostenlos bis auf die Claude-Nutzung – siehe [docs/SETUP.md](docs/SETUP.md).

## Schnellstart Windows

1. **NVIDIA-Treiber** aktualisieren (GeForce Experience / nvidia.com). WSL2 braucht keinen extra Treiber.
2. **WSL2** installieren: PowerShell **als Administrator** öffnen und eingeben:
   ```powershell
   wsl --install
   ```
   Danach neu starten. Es öffnet sich „Ubuntu“ – Benutzername und Passwort festlegen.
3. **Docker Desktop** installieren (docker.com → „Download for Windows“). Bei der Installation
   „Use WSL 2 instead of Hyper-V“ anhaken. Danach in Docker Desktop: *Settings → Resources → WSL integration*
   → „Ubuntu“ einschalten. Unter *Settings → General* „Start Docker Desktop when you sign in“ aktivieren.
4. GPU-Test in der **Ubuntu**-App (Startmenü → „Ubuntu“):
   ```bash
   docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi
   ```
   Erscheint eine Tabelle mit deiner Grafikkarte: perfekt.
5. Projekt **in Ubuntu** herunterladen – **nicht** nach `C:\` (Videos und Datenbank liegen sonst auf einem
   langsamen Windows-Laufwerk-Durchgriff, SQLite ist dort unzuverlässig):
   ```bash
   sudo apt update && sudo apt install -y git
   git clone <URL-deines-Repos> wasteddailyvi
   cd wasteddailyvi
   ```
6. Weiter mit [Erster Start](#erster-start-beide-systeme).

> Lieber ohne Ubuntu-Fenster? Das Projekt bleibt trotzdem **in Ubuntu** (Schritt 5). Im Windows-Explorer
> findest du es unter `\\wsl$\Ubuntu\home\<dein-name>\wasteddailyvi`. Befehle aus der PowerShell schickst
> du so an Ubuntu: `wsl sh -c "cd ~/wasteddailyvi && docker compose ps"` – Updates und Sicherungen genauso:
> `wsl sh -c "cd ~/wasteddailyvi && sh scripts/update.sh"`. (`scripts\setup.ps1` gibt es für den Notfall,
> wenn das Projekt doch unter Windows liegt: `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`.)

## Schnellstart Linux

(Ubuntu/Debian; andere Distributionen analog)

1. NVIDIA-Treiber installieren: `sudo ubuntu-drivers install`, neu starten, `nvidia-smi` testen.
2. Docker installieren: `curl -fsSL https://get.docker.com | sh` und `sudo usermod -aG docker $USER`
   (danach ab- und wieder anmelden).
3. **NVIDIA Container Toolkit** installieren (Anleitung: docs.nvidia.com → „Installing the NVIDIA Container
   Toolkit“, Abschnitt *apt*), dann:
   ```bash
   sudo nvidia-ctk runtime configure --runtime=docker
   sudo systemctl restart docker
   sh scripts/gpu-test.sh        # (nach dem Klonen) zeigt deine Grafikkarte im Container
   ```
4. Projekt klonen: `git clone <URL-deines-Repos> wasteddailyvi && cd wasteddailyvi`
5. Weiter mit [Erster Start](#erster-start-beide-systeme).

## Erster Start (beide Systeme)

Im Projektordner (Ubuntu-Fenster bzw. Linux-Terminal):

```bash
# 1. Vorlagen kopieren (oder alles in einem: sh scripts/setup.sh)
cp .env.example .env
cp config.example.yaml config.yaml
mkdir -p data claude-home
echo "APP_UID=$(id -u)" >> .env     # Container-Benutzer = du (dann gehört dir data/)

# 2. Zugangsdaten in .env eintragen (mindestens ANTHROPIC_API_KEY und WDVI_DASHBOARD_PASSWORD)
nano .env            # speichern: Strg+O, Enter, Strg+X
#    und in config.yaml deine YouTube-Kanäle / Wunsch-Uhrzeiten anpassen
nano config.yaml

# 3. Image bauen (beim ersten Mal 10–20 Minuten) und einrichten
docker compose build
docker compose run --rm wdvi wdvi init

# 4. Systemcheck – zeigt für jedes Problem, was zu tun ist (✅ ok · ⚠️ Warnung · ❌ Fehler)
docker compose run --rm wdvi wdvi doctor

# 5. Optional: Test-Render ohne Claude (prüft GPU-Encoder, Schriften, Captions)
docker compose run --rm wdvi wdvi render-demo
#    -> Video liegt danach in data/clips/demo/

# 6. Dauerbetrieb starten
docker compose up -d
```

Dann im Browser **http://localhost:8420** öffnen (Benutzername egal, Passwort = `WDVI_DASHBOARD_PASSWORD`).
Unter **Verbindungen** verbindest du YouTube, TikTok und Instagram (Anleitung: [docs/SETUP.md](docs/SETUP.md)).
Nach ein paar Minuten tauchen die ersten Kandidaten auf, nach etwa 10–20 Minuten die ersten fertigen Clips.

Nützliche Befehle:

| Befehl | Wirkung |
|---|---|
| `docker compose logs -f --tail 100` | Live-Log ansehen (Strg+C beendet nur die Anzeige) |
| `docker compose ps` | Läuft der Container? Ist er „healthy“? |
| `docker compose restart` | Neu starten (z. B. nach Änderung der config.yaml) |
| `docker compose down` | Stoppen |
| `docker compose exec wdvi wdvi process-url <Link>` | einen bestimmten Clip sofort verarbeiten |
| `docker compose exec wdvi wdvi discover` | sofort nach neuen Clips suchen |
| `docker compose exec wdvi wdvi requeue <Clip-Nr>` | fehlgeschlagene/unterbrochene Posts eines Clips neu einplanen |
| `sh scripts/update.sh` | Update: Sicherung, neuer Code, neu bauen (neueste yt-dlp-Version), neu starten |

### Ohne GPU (nur CPU)

Langsam (Whisper und Rendern dauern ein Vielfaches), aber möglich:

1. In `.env`: `TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu` (kleineres Image ohne CUDA-PyTorch).
2. Im Projektordner eine Datei `docker-compose.override.yml` anlegen (Docker liest sie automatisch, Updates
   lassen sie in Ruhe):
   ```yaml
   services:
     wdvi:
       deploy: !reset {}
   ```
3. In `config.yaml`: `whisper.model: "small"` (oder `"medium"`) und `render.encoder: "x264"`.
4. `docker compose build` und weiter wie oben.

## Kosten

**Claude über die API** (`llm.backend: api`, Standard): bezahlt wird pro bewertetem Clip. Opus 5.5 kostet
4 $ pro Million Eingabe-Tokens und 20 $ pro Million Ausgabe-Tokens. Ein Clip (Metadaten, Transkript,
8 Standbilder, Antwort mit Schnittplan) kostet damit **etwa 0,05–0,15 $**. Ein langes YouTube-Video nach
Momenten zu durchsuchen kostet ähnlich viel.

- Beispiel: 100 bewertete Clips pro Tag ≈ **5–15 $ pro Tag**.
- Kostenbremse: `llm.max_calls_per_hour` in der config.yaml (Beispiel-Konfiguration: 20 pro Stunde).
  Weitere Hebel: `sources.twitch.min_views` erhöhen, `llm.effort: medium`, `llm.max_frames: 6`.
- Clips, die schon an Sprache, Musik, Länge oder als Duplikat scheitern, kosten **nichts** – sie kommen gar
  nicht erst bei Claude an.
- Verbrauch prüfen: console.anthropic.com → *Usage*. Dort kannst du auch ein Monatslimit setzen.

**Claude über die CLI mit deinem Claude-Abo** (`llm.backend: cli`): keine Kosten pro Clip, aber es gelten
die Nutzungslimits deines Abos (Pro/Max). Ist das Limit erreicht, pausiert die Pipeline automatisch und macht
später weiter. Einrichtung: [docs/SETUP.md → Claude](docs/SETUP.md#1-claude).

Alles andere (Whisper, Rendern, Plattform-APIs) läuft lokal bzw. kostenlos. Strom: ein PC im Dauerbetrieb.

## Spielregeln – bitte lesen!

Ein Clip-Kanal lebt von den Creatorn, deren Momente er zeigt. Damit der Kanal lange lebt:

1. **Jeder Creator wird genannt – automatisch.** Im Video steht oben links der Credit (z. B.
   „TWITCH · xQc“), in jeder Beschreibung die Zeile „🎥 Clip: … on Twitch – twitch.tv/…“ (bei Reddit-Reposts
   eines Twitch-Clips zusätzlich „(via u/… on Reddit)“). Bitte nicht entfernen.
   Auch wenn du Texte im Dashboard bearbeitest, hängt das Programm Credit, Fan-Hinweis und #GTA6 wieder an.
2. **Opt-outs respektieren.** Möchte ein Creator nicht geclippt werden: im Dashboard unter **Creators** auf
   die Block-Liste setzen. Neue Clips von ihm werden dann sofort übersprungen, schon freigegebene oder
   eingeplante Clips innerhalb einer Minute verworfen. Bereits gepostete Videos (und bei YouTube schon
   vorab hochgeladene) löschst du selbst auf den Plattformen (siehe [docs/BETRIEB.md](docs/BETRIEB.md#takedowns-und-opt-outs)).
3. **Keine Leaks.** Geleaktes oder unveröffentlichtes Material (Testbuilds, interne Videos) wird automatisch
   verworfen (`filters.leak_policy: reject`). Take-Two lässt solches Material konsequent entfernen – Strikes
   gefährden den ganzen Kanal.
4. **Keine Musik.** Clips mit erkannter Hintergrundmusik werden verworfen (`filters.music_policy: reject`).
   Musik ist der häufigste Grund für Content-ID-Sperren. Eigene Musik nur lizenzfrei (`render.music_bed`).
5. **Fan-Kanal-Hinweis.** Jede Beschreibung enthält „Fan channel – not affiliated with Rockstar Games.“
6. **Reine Reuploads werden abgestraft.** YouTube, TikTok und Instagram bewerten unbearbeitete Reposts
   als „wiederverwendete Inhalte“ (keine Monetarisierung, weniger Reichweite). Deshalb schneidet und verpackt
   das Programm jeden Clip neu – und deshalb gilt: **Freigabe anlassen** (`publish.require_approval: true`),
   jedenfalls in den ersten Wochen. Schau dir jeden Clip an, bevor er rausgeht.
7. **Fair bleiben.** Kurze Ausschnitte, klarer Mehrwert (Schnitt, Untertitel, Kontext), Credit und
   Verlinkung. Wenn ein Creator ausdrücklich zustimmt, kannst du ihn auf die Allow-Liste setzen.

Keine Rechtsberatung: Clip-Kanäle bewegen sich in einer Grauzone („Fair Use“ ist in Deutschland/EU kein
Rechtsbegriff). Im Zweifel den Creator fragen.

## Fehlerbehebung

Erster Schritt immer: `docker compose run --rm wdvi wdvi doctor` – das zeigt für jedes Problem den Lösungsweg.

| Problem | Lösung |
|---|---|
| `config.yaml ist ein Ordner` | Docker hat beim Start einen Ordner angelegt, weil die Datei fehlte: `rm -r config.yaml && cp config.example.yaml config.yaml` |
| `env file .env not found` | `cp .env.example .env` und ausfüllen |
| `could not select device driver "nvidia"` | Linux: NVIDIA Container Toolkit installieren und `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker`. Windows: Docker Desktop + aktueller NVIDIA-Treiber, WSL-Integration an. Keine NVIDIA-GPU: [Ohne GPU](#ohne-gpu-nur-cpu). |
| `unsatisfied condition: cuda>=12.8` | NVIDIA-Treiber zu alt – Version 570 oder neuer installieren. |
| doctor: „NVENC nicht nutzbar“ | GPU ist im Container nicht sichtbar oder Treiber zu alt. `sh scripts/gpu-test.sh`; Treiber aktualisieren. Es wird dann (langsamer) auf der CPU gerendert. |
| Dashboard zeigt „401“ / fragt nach Passwort | Benutzername egal, Passwort = `WDVI_DASHBOARD_PASSWORD` aus der .env. Nach Änderung: `docker compose up -d` |
| Dashboard nicht erreichbar | `docker compose ps` (läuft er?), `docker compose logs --tail 100`. Port 8420 belegt? Dann in `.env` `WDVI_HOST_PORT=8421` eintragen, `public_base_url` in config.yaml anpassen, `docker compose up -d`. (docker-compose.yml nicht bearbeiten – sonst klemmt das nächste Update.) |
| `docker compose ps` zeigt „unhealthy“ | Dashboard oder Hintergrund-Jobs hängen. `docker compose logs --tail 200` ansehen, dann `docker compose restart`. Docker startet einen „unhealthy“ Container nicht von selbst neu. |
| Keine Kandidaten | `docker compose exec wdvi wdvi discover` zeigt pro Quelle, was los ist (Zugangsdaten? Kanäle eingetragen?). Vor dem GTA-6-Release gibt es schlicht wenige Clips. |
| Kandidaten bleiben auf „new“ | Claude nicht eingerichtet (`ANTHROPIC_API_KEY`) oder Stundenlimit erreicht – siehe Dashboard → Health. |
| Viele „failed: Download …“ | Clip gelöscht/privat oder yt-dlp veraltet → `sh scripts/update.sh` (baut immer mit der neuesten yt-dlp-Version neu). YouTube „not a bot“: Cookies-Datei, siehe .env.example (`WDVI_YTDLP_COOKIES_FILE`). Netzwerkfehler zählen nicht: die Verarbeitung pausiert, die Kandidaten bleiben in der Warteschlange und werden später erneut versucht (nach 4 Versuchen aufgegeben). |
| Post „Upload wurde unterbrochen“ | Der Container wurde mitten im Upload neu gestartet. Auf der Plattform nachsehen, ob das Video doch ankam; falls nicht: `docker compose exec wdvi wdvi requeue <Clip-Nr>`. |
| Erster Clip dauert ewig | Beim ersten Mal lädt Whisper sein Modell (~1,6 GB) und die Musik-Erkennung ihr Modell. Danach geht es schnell. |
| YouTube-Uploads sind „privat“ (Chip „Privat“ unter Gepostet) | Normal für neue Google-Cloud-Projekte: erst nach dem YouTube-API-Audit dürfen API-Uploads öffentlich sein – im YouTube Studio lässt sich die Sperre nicht aufheben. Audit beantragen; bis dahin YouTube abschalten und Videos aus `data/clips/` selbst hochladen. Details: [docs/SETUP.md](docs/SETUP.md#45-wichtig-api-audit-und-kontingent). |
| YouTube muss jede Woche neu verbunden werden | OAuth-App steht noch auf „Testing“ → in der Google Cloud Console auf „In production“ stellen ([docs/SETUP.md](docs/SETUP.md#4-youtube-hochladen)). |
| TikTok/Instagram: Fehlerseite nach dem Login | Erwartet (die Plattformen verlangen https). Die komplette Adresse aus der Browserzeile kopieren und im Dashboard unter „Redirect-URL einfügen“ einfügen. |
| Kick liefert nichts / „Cloudflare“ | Kick hat keine offizielle API und blockiert manchmal. `sources.kick.enabled: false`. |
| Festplatte voll | Läuft normalerweise nicht voll: gepostete Videos werden nach 14 Tagen gelöscht, unter 5 GB frei pausiert die Verarbeitung (`cleanup` in config.yaml, siehe [docs/BETRIEB.md](docs/BETRIEB.md#speicherplatz)). |
| `permission denied` in `data/` | Linux/WSL: `APP_UID=$(id -u)` in `.env` eintragen (deine Benutzer-ID), `sudo chown -R $(id -u):$(id -g) data claude-home`, dann `docker compose build && docker compose up -d`. |
| „Unbekannte Einträge in config.yaml“ | Tippfehler oder falsch eingerückt – die Einstellung wirkt sonst nicht. Schreibweise mit `config.example.yaml` vergleichen. |

## Für Entwickler

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"     # optional: ".[dev,music]"
.venv/bin/pytest                      # alle Tests (inkl. echter ffmpeg-Renders, ein paar Minuten)
.venv/bin/ruff check src tests
.venv/bin/wdvi --help
```

Aufbau (`src/wdvi/`): `sources/` (Twitch, Kick, YouTube, Reddit) · `media/` (Download, Whisper, Musik,
Gesichter, Dedup) · `brain/` (Claude: Bewertung, Schnittplan, Momente) · `render/` (ffmpeg, Captions,
Overlays) · `publish/` (YouTube, TikTok, Instagram, Slots, Texte) · `dashboard/` (FastAPI) ·
`pipeline.py` (Ablauf) · `scheduler.py` (Hintergrund-Jobs) · `cli.py` (`wdvi …`) · `doctor.py` (Systemcheck).
Konfiguration: `config.py` (+ `config.example.yaml`), Datenmodell: `models.py`, Schnittstellen: `schemas.py`.

Schriften: Archivo Black und Anton (SIL Open Font License, siehe `assets/fonts/`).
Fan-Projekt – nicht mit Rockstar Games oder Take-Two Interactive verbunden.
