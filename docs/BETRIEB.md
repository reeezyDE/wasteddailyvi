# Betrieb: Alltag mit WastedDailyVI

So läuft der Kanal im Alltag: Clips prüfen (auch am Handy), verstehen, was im Hintergrund passiert,
mit Takedown-Wünschen umgehen, Updates einspielen.

## Der Tagesablauf

Im Hintergrund arbeitet WastedDailyVI selbstständig:

| Was | Wann |
|---|---|
| Neue Clips suchen (Twitch, Kick, YouTube, Reddit) | je Quelle alle 10–20 min (`poll_minutes`) |
| Beste Kandidaten herunterladen, analysieren, von Claude bewerten lassen, rendern | alle 2 min, je bis zu 6 (`candidates_per_cycle`) |
| Freigegebene Clips in Posting-Slots einplanen | jede Minute |
| Fällige Posts hochladen | jede Minute |
| Aufrufzahlen holen (24 h / 72 h nach dem Posten) | alle 3 h |
| Aufräumen (alte Downloads, abgelehnte und alte gepostete Videos, ungeprüfte Clips nach 7 Tagen) | täglich 04:00 UTC |

**Deine Aufgabe** (5–10 Minuten, 1–2× am Tag):

1. Dashboard öffnen → **Review**. Jeder Clip zeigt Video, Viral-Score, Claudes Begründung, die Texte je
   Plattform und den Creator.
2. Video ansehen. Passt es? Dann Titel/Beschreibung bei Bedarf anpassen, Plattformen anhaken,
   **Freigeben**. Sonst **Ablehnen** (gern mit kurzem Grund).
3. Freigegebene Clips landen automatisch im nächsten freien Slot (US-Ostküstenzeit, siehe
   `publish.*.slots`). Unter **Geplant** siehst du, wann was rausgeht.
4. **TikTok im Inbox-Modus:** Zur Slot-Zeit kommt in der TikTok-App die Benachrichtigung, dass ein Entwurf
   bereit ist. **Der Entwurf kommt OHNE Text an** – TikTok erlaubt beim Inbox-Upload keine Caption. So
   postest du ihn:
   1. Am Handy das Dashboard öffnen → **Gepostet** (der Post steht dort mit dem Chip „Entwurf“; die Caption
      steht auch schon vorher unter **Geplant**).
   2. Auf **„📋 Caption kopieren“** tippen – kopiert Titel, Beschreibung, Credit-Zeile, Fan-Hinweis und
      Hashtags in die Zwischenablage.
   3. In der TikTok-App den Entwurf öffnen, die Caption ins Textfeld einfügen, ggf. Sound wählen → *Posten*.

   Ohne die eingefügte Caption fehlt der Credit für den Creator – bitte nie ohne posten. (Tipp: Sounds aus
   der TikTok-Bibliothek sind innerhalb von TikTok lizenziert – fremde Musik im Video selbst dagegen nicht.)
5. **YouTube prüfen, solange der API-Audit fehlt:** Unter **Gepostet** markiert ein roter Chip **„Privat“**
   YouTube-Videos, die zum geplanten Termin nicht öffentlich geworden sind (siehe unten
   „YouTube: privat gesperrte Videos“).

## Vom Handy aus prüfen

Das Dashboard ist absichtlich nur auf dem PC selbst erreichbar (`127.0.0.1:8420`). Für das Handy gibt
es zwei Wege:

### Empfohlen: Tailscale (sicher, auch unterwegs, mit https)

1. **tailscale.com** → kostenloses Konto. Tailscale auf dem **PC** (Windows-Installer bzw.
   `curl -fsSL https://tailscale.com/install.sh | sh` unter Linux) und auf dem **Handy** (App Store /
   Play Store) installieren, überall mit demselben Konto anmelden.
2. Auf dem PC (Windows: PowerShell, Linux: Terminal):
   ```
   tailscale serve --bg 8420
   ```
   Tailscale zeigt dir eine Adresse wie `https://mein-pc.tailXXXX.ts.net`.
3. Diese Adresse am Handy öffnen → Passwort (`WDVI_DASHBOARD_PASSWORD`) eingeben. Fertig – das Dashboard
   ist nur für deine eigenen Tailscale-Geräte sichtbar.

Bonus: Die https-Adresse kannst du auch als Redirect-URI bei TikTok/Instagram eintragen
(`https://mein-pc.tailXXXX.ts.net/oauth/tiktok/callback` + `WDVI_TIKTOK_REDIRECT_URI` in `.env`) – dann
entfällt das „Redirect-URL einfügen“.

### Alternative: im Heimnetz (WLAN)

1. In `.env` die Zeile `WDVI_BIND_ADDR=0.0.0.0` eintragen (nicht `docker-compose.yml` bearbeiten – sonst
   klemmt das nächste Update).
2. **Unbedingt** `WDVI_DASHBOARD_PASSWORD` gesetzt haben, dann `docker compose up -d`.
3. Am Handy (im selben WLAN) `http://<IP-des-PCs>:8420` öffnen. Die IP zeigt `ipconfig` (Windows) bzw.
   `hostname -I` (Linux). Windows fragt evtl. nach einer Firewall-Freigabe → nur „Private Netzwerke“ erlauben.
4. Niemals den Port im Router freigeben (Portweiterleitung) – dafür ist Tailscale da.

## Was die Status bedeuten

### Kandidaten (Dashboard → Kandidaten)

| Status | Bedeutung |
|---|---|
| `new` | gefunden, wartet auf Verarbeitung (die mit dem höchsten Prescore zuerst). Mit Grund „neuer Versuch ab …“: ein vorübergehender Fehler (Netz, Limit, Livestream läuft noch) – kommt zum genannten Zeitpunkt wieder dran |
| `queued` | für den laufenden Durchgang ausgewählt |
| `processing` | wird gerade heruntergeladen / analysiert / bewertet / gerendert |
| `rendered` | fertig, ein Clip liegt im Review |
| `rejected` | aussortiert, **mit Grund**: z. B. „Claude: nothing happens (Score 45)“, „Musik erkannt“, „Sprache 'es'“, „Duplikat“, „Leak“ |
| `skipped` | übersprungen: Block-Liste, zu alt, zu lang/kurz (bei Reddit-Links erst nach dem Download messbar), falsche Twitch-Sprache, „gleicher Moment wie …“, „gleicher Clip wie …“ (derselbe Clip über Twitch und Reddit) |
| `failed` | technischer Fehler, z. B. „Download: Clip gelöscht“, „Rendern: …“ – Details im Grund. Vorübergehende Download-/Whisper-Fehler werden erst nach 4 Versuchen zu `failed`, andere vorübergehende Fehler nach 5 neuen Versuchen (jeweils über mehrere Stunden verteilt) |
| `mined` | langes YouTube-Video: Claude hat daraus Momente als neue Kandidaten angelegt |

Bleiben Kandidaten mit dem Grund „Claude vorübergehend nicht verfügbar“ auf `new`, wurde ein Limit
erreicht (Anthropic-Rate-Limit, `llm.max_calls_per_hour` oder Abo-Limit der CLI). WastedDailyVI pausiert dann
15 Minuten und macht von selbst weiter. Scheitert nur ein einzelner Clip immer wieder an Claude (z. B.
Zeitüberschreitung), wird er nach 3 Versuchen aufgegeben – die anderen laufen in der Zwischenzeit weiter.

**Vorübergehende Fehler** verbrennen keine Kandidaten:

- **Download oder Whisper** scheitert vorübergehend (Internet weg, DNS „Temporary failure in name
  resolution“, Zeitüberschreitung, HTTP 429/5xx, YouTube-Bot-Check, volle Platte, Whisper-Modell nicht
  ladbar): Der Clip bleibt `new` (Grund „… vorübergehend fehlgeschlagen – neuer Versuch ab …“) und die
  Verarbeitung **pausiert sofort** 15 Minuten – der nächste Kandidat würde ja genauso scheitern. Nach
  4 Versuchen gibt WastedDailyVI diesen einen Clip auf (`failed`). Endgültige Fehler (Clip gelöscht, privat,
  404) sind dagegen sofort `failed`, ohne Pause. Clips, die laut Metadaten länger als
  `filters.max_source_duration_s` sind, werden gar nicht erst heruntergeladen (`skipped`, „Zu lang“).
- **Andere vorübergehende Fehler** (z. B. Festplatte voll beim Rendern): der Clip kommt nach 15, 30, 60 …
  Minuten wieder dran; treffen zwei verschiedene Kandidaten hintereinander solche Fehler, pausiert die
  Verarbeitung ebenfalls.

Scheitern **drei Kandidaten hintereinander** endgültig (typisch: falscher API-Key, kaputtes Programm),
pausiert sie ebenfalls; diese drei kommen nach der Pause einmal erneut dran. Jede weitere Pause ohne Erfolg
dazwischen dauert doppelt so lang (bis 120 Minuten). Unter **Health** steht beim Job `process` (rot) der
letzte Fehler – den beheben, der Rest läuft von allein weiter.

Unter 5 GB freiem Speicher (`cleanup.min_free_gb`) werden keine neuen Clips verarbeitet (Meldung unter Health).

### Clips

| Status | Bedeutung |
|---|---|
| `pending_review` | wartet auf deine Freigabe |
| `approved` | freigegeben, wird im nächsten Durchlauf eingeplant |
| `scheduled` | hat Posting-Termine (siehe „Geplant“) |
| `posted` | auf allen gewählten Plattformen raus (oder bei YouTube fest geplant / bei TikTok als Entwurf zugestellt) |
| `rejected` | von dir abgelehnt, 7 Tage nicht geprüft oder Creator blockiert (Video wird nach 7 Tagen gelöscht) |
| `failed` | alle Uploads sind endgültig gescheitert – Grund steht beim Clip. Neu einplanen: Dashboard → **Gepostet** → „↻ Erneut einplanen“ (oder `wdvi requeue <Clip-Nr>`) |

### Posts (je Plattform)

| Status | Bedeutung |
|---|---|
| `scheduled` | wartet auf seinen Termin. Bei YouTube mit „Remote-ID“: schon hochgeladen und bei YouTube geplant; nach dem Termin prüft WastedDailyVI noch, ob YouTube es wirklich veröffentlicht hat |
| `uploading` | wird gerade hochgeladen |
| `posted` | veröffentlicht. Mit Chip **„Privat“**: YouTube hat das Video nicht öffentlich gemacht (fehlender API-Audit), mit **„Prüfen“**: Sichtbarkeit konnte nicht geprüft werden |
| `draft_sent` | TikTok-Entwurf ist in deiner App angekommen (ohne Text!) – Caption im Dashboard kopieren, einfügen, *Posten* |
| `failed` | aufgegeben (Fehlermeldung steht dabei), Upload durch einen Neustart unterbrochen oder das geplante YouTube-Video ist verschwunden |
| `canceled` | abgebrochen (im Dashboard, Clip abgelehnt, Creator blockiert oder Plattform deaktiviert) |

**Wiederholungen:** Lehnt eine Plattform das Video ab (oder ist der Fehler unbekannt), versucht
WastedDailyVI es nach 15 min, 2 h und 12 h erneut; nach dem vierten Fehlschlag ist Schluss.
**Vorübergehende Probleme zählen nicht als Fehlversuch:** Netzwerk-/Serverfehler, Kontingent oder
Rate-Limit erschöpft (YouTube-Tageskontingent, TikTok „zu viele offene Entwürfe“, Instagram-Limit) und
„nicht (mehr) verbunden“ (z. B. abgelaufene YouTube-Anmeldung). Dann wartet der Post 15 min, 1 h, 4 h,
12 h … und rückt bei Limits und fehlender Verbindung in den nächsten freien Slot – nach dem Neu-Verbinden
wird also nicht alles auf einmal gepostet. Erst nach 5 Tagen ohne Erfolg gibt er auf.
Bei YouTube wird bis zu 24 h vor dem Termin vorab hochgeladen (nur bei `privacy: public` – nur dann kann
YouTube selbst zum Termin veröffentlichen); klappt das nicht, probiert es alle 30 min weiter, ohne dass das
als Fehlversuch zählt.

**Fehlgeschlagene Posts neu einplanen:** Dashboard → **Gepostet** → Abschnitt „Fehlgeschlagene Uploads“ →
**„↻ Erneut einplanen“** (gleichwertig: `docker compose exec wdvi wdvi requeue <Clip-Nr>`). Nimmt alle
gescheiterten Posts des Clips, setzt die Versuche zurück und plant sie in den nächsten freien Slot. Wurde ein
Upload unterbrochen, vorher auf der Plattform nachsehen, ob das Video nicht doch schon online ist.

**Geplante Posts abbrechen:** Dashboard → **Geplant** → *Abbrechen*. Liegt das Video schon geplant bei
YouTube, zieht WastedDailyVI es dort zurück (privat, ohne Veröffentlichungstermin – nicht gelöscht). Klappt
das nicht (z. B. YouTube nicht verbunden oder Berechtigung `youtube.force-ssl` fehlt), sagt die Meldung,
dass du es im YouTube Studio selbst löschen bzw. auf privat stellen musst. Dasselbe passiert, wenn ein
Creator blockiert wird.

**Kein freier Slot:** Findet ein freigegebener Clip 14 Tage lang keinen Slot (zu wenige `slots`, zu großer
`min_gap_minutes`, YouTube-Tageslimit), steht er unter **Geplant** bei „Freigegeben, noch nicht eingeplant“,
und unter **Health** ist der Job `schedule` rot mit der Clip-Nummer.

### YouTube: privat gesperrte Videos (fehlender API-Audit)

Solange dein Google-Cloud-Projekt den **YouTube-API-Audit** nicht bestanden hat, sperrt YouTube alle über
die API hochgeladenen Videos auf **„privat“** – auch nativ geplante Videos werden zum Termin nicht
öffentlich, und im YouTube Studio lässt sich die Sperre nicht selbst aufheben. WastedDailyVI prüft deshalb
10–30 Minuten nach dem geplanten Termin bei YouTube nach:

- **öffentlich** → alles gut, der Post zählt als gepostet;
- **weiterhin privat** → der Post steht unter **Gepostet** mit rotem Chip **„Privat“** und dem Hinweis auf
  den Audit (außerdem in der `publish`-Meldung unter Health). Für diese Videos werden keine Aufrufzahlen
  gesammelt (0 Views wären falsches Feedback für Claude);
- **nicht mehr vorhanden** (gelöscht) → der Post wird `failed` und kann mit „Erneut einplanen“ neu
  hochgeladen werden;
- **nicht prüfbar** (Netz, Kontingent) → Chip **„Prüfen“**: selbst im Studio nachsehen.

Bis der Audit durch ist (Antrag: [SETUP.md → API-Audit](SETUP.md#45-wichtig-api-audit-und-kontingent)):
YouTube in `config.yaml` mit `publish.youtube.enabled: false` abschalten und die fertigen Videos aus
`data/clips/` selbst im YouTube Studio hochladen – oder die gesperrten Uploads in Kauf nehmen.

## Edit-Engine v2 im Alltag

Seit der Edit-Engine v2 plant Claude nicht nur Schnitt und Zooms, sondern das ganze Short wie ein
Top-Clip-Kanal (Überblick: [README → Edit-Engine v2](../README.md#edit-engine-v2)):

- **Shots:** Die Ansicht wechselt alle 1–3 s zwischen Gesicht groß, Kamera oben/Gameplay unten,
  Gameplay-Ausschnitt und ganzem Bild – mit harten Schnitten, Blitz, Zoom-Punch oder (selten) Glitch.
- **Hook:** Ist der erste gesprochene Satz stark, *ist* er der Hook (Gesicht bildfüllend, kein Balken).
  Nur sonst steht oben der Hook-Text.
- **Markierungen:** animierter Kreis/Rahmen/Pfeil um das entscheidende Detail im Bild.
- **Farben:** Schlüsselwörter in den Captions in ihrer Bedeutungsfarbe (PlayStation blau, Xbox grün,
  GTA 6 pink, Geld grün, Polizei rot) mit Glow.
- **Schlussfrage:** am Ende eine Frage für die Kommentare; das Video loopt in den Anfang zurück. Wie sehr
  ein Clip zum Diskutieren einlädt, schätzt Claude als `debate_score` (0–100, steht im Plan des Clips).
- **Texte:** Die YouTube-Beschreibung enthält nach Credit und Fan-Hinweis einen Block **„Transcript:“** mit
  dem gesprochenen Text des fertigen Clips (nur die behaltenen Stellen, max. ~600 Zeichen) – YouTube liest
  ihn für die Suche. Danach 3–5 Hashtags. Die YouTube-Tags (Keywords) kommen aus dem Plan. TikTok und
  Instagram bleiben kurz (Titel, Beschreibung, Credit, 3–5 Hashtags). `#GTA6` steht nur noch bei Clips, deren
  Titel/Hashtags/Tags GTA erwähnen.

**Beim Review beachten:**

1. Ansehen, ob die Schlussfrage passt und nicht beleidigend wirkt. Passt sie nicht: Clip ablehnen bzw.
   mit `wdvi process-url <Link> --force` neu rendern lassen (Claude plant dann neu).
2. Das Transkript in der YouTube-Beschreibung stammt von Whisper. Falsch erkannte Wörter (vor allem
   Namen) kannst du vor der Freigabe direkt im Textfeld korrigieren – der korrigierte Text bleibt erhalten.
   Löschst du nur den Transkript-Block, bleibt er weg; löschst du den ganzen Anhang, hängt WastedDailyVI
   Credit, Fan-Hinweis, Transkript und Hashtags neu an.
3. Wirkt ein Video zu hektisch oder zu bunt, in der `config.yaml` unter `render` nachregeln
   (`transition_ms`, `grade_saturation`, `grade_warmth`, `color_grade: false`, `caption_glow: false`), dann
   `docker compose restart`. Die Wunschlänge steuern `filters.target_output_min_s`/`target_output_max_s`
   (Standard 12–25 s).

## Beste Quellqualität

Shots vergrößern Ausschnitte der Quelle stark – **360p-Quellen sehen im fertigen 1080×1920-Video weich und
verwaschen aus**. WastedDailyVI lädt automatisch die beste Auflösung bis 1080p (yt-dlp `-S "res:1080"`) und
warnt im Log: „Quelle nur 360p – Ergebnis wird unscharf“.

Einen Link selbst prüfen und lokal laden:

```bash
docker compose exec wdvi yt-dlp -F "<Link>"                                  # Formate auflisten (Spalte RESOLUTION)
docker compose exec wdvi yt-dlp -S "res:1080" -o "data/quelle-test.%(ext)s" "<Link>"   # beste Qualität bis 1080p
```

Steht bei `-F` höchstens `640x360`, eine bessere Quelle suchen (Original-Clip auf Twitch oder das
YouTube-Video des Streamers statt Reddit-Repost/Re-Upload) und diese mit
`docker compose exec wdvi wdvi process-url "<Link>"` verarbeiten. Liefert YouTube nur niedrige Auflösungen
oder fragt nach „not a bot“: Cookies-Datei eintragen (`WDVI_YTDLP_COOKIES_FILE` in `.env`).

## Takedowns und Opt-outs

Früher oder später schreibt ein Creator: „Bitte keine Clips von mir.“ Oder eine Plattform meldet eine
Urheberrechtsbeschwerde. So gehst du vor:

1. **Sofort auf die Block-Liste:** Dashboard → **Creators** → Plattform (z. B. Twitch) und Name/Handle
   (z. B. `xqc`) eintragen, Regel **block**, Notiz mit Datum und Anlass. Ab sofort werden neue Clips dieses
   Creators übersprungen – auch Reddit-Posts, die auf seine Twitch-Clips verlinken. Clips von ihm, die noch
   im Review liegen, freigegeben oder eingeplant sind, werden innerhalb einer Minute verworfen bzw. ihre
   Posts abgebrochen (Status „Creator blockiert (Opt-out)“). Reddit-Reposter blockst du mit Plattform
   **Reddit** und ihrem Namen (z. B. `u/clipposter`) – auch wenn ihr Post auf einen Twitch-Clip verlinkt und
   der Credit an den Streamer geht („🎥 Clip: shroud on Twitch – twitch.tv/shroud (via u/clipposter on Reddit)“).
2. **Bestehende Videos löschen:** WastedDailyVI löscht nichts auf den Plattformen. In YouTube Studio,
   der TikTok-App und Instagram die betroffenen Videos selbst entfernen. Unter **Gepostet** siehst du, welche
   Clips von welchem Creator wo liegen (mit Links). **Achtung YouTube:** Ein Video, das schon vorab hochgeladen
   und bei YouTube geplant ist, geht sonst zum Termin online – der Clip zeigt dann den Hinweis mit der
   Video-ID; in YouTube Studio löschen oder auf privat stellen. (Normalerweise zieht WastedDailyVI so ein
   Video beim Blockieren selbst zurück – der Hinweis erscheint nur, wenn das nicht geklappt hat.)
3. **Freundlich antworten**, am besten innerhalb von 24 Stunden: entschuldigen, Löschung bestätigen.
4. **Urheberrechtsbeschwerden / Strikes** ernst nehmen: Bei YouTube führen 3 Strikes in 90 Tagen zur
   Kanalsperrung. Nicht widersprechen, wenn du nicht sicher im Recht bist. Betroffenen Creator blocken.
5. Häufen sich Probleme mit einer Quelle oder Art von Clips: in `config.yaml` die Quelle abschalten oder
   `min_virality_score` erhöhen.

Umgekehrt: Erlaubt dir ein Creator ausdrücklich das Clippen, setze ihn auf **allow** oder **boost** –
seine Clips werden dann bevorzugt verarbeitet.

## Überwachung

- **Dashboard → Health:** letzte Läufe aller Hintergrund-Jobs mit Meldung. Typisch:
  - `discover:twitch ✓ 87 gefunden, 6 neu, 81 aktualisiert, 0 übersprungen`
  - `process ✓ 6 Kandidaten bearbeitet, 2 Clips gerendert, 0 Momente gefunden, 3 übersprungen`
  - `publish ✓ 1 hochgeladen, 0 fehlgeschlagen`
  - Rote Einträge (✗) mit Meldung wie „Quelle 'twitch' ist nicht konfiguriert“ oder „Kein
    ANTHROPIC_API_KEY gesetzt“ zeigen, was zu tun ist.
- **Container-Zustand:** `docker compose ps` → Spalte *STATUS* sollte `Up … (healthy)` zeigen. Der
  eingebaute Healthcheck prüft jede Minute, ob das Dashboard antwortet **und** die Hintergrund-Jobs laufen
  (Datenbank lesbar, Minuten-Job zuletzt vor höchstens 15 min). Wichtig: Docker zeigt dann nur
  `(unhealthy)` an, **startet den Container aber nicht von selbst neu** (`restart: unless-stopped` greift nur,
  wenn das Programm abstürzt). Bei `(unhealthy)`: `docker compose logs --tail 200` ansehen, dann
  `docker compose restart`.
- **Log:** `docker compose logs -f --tail 200` (Strg+C beendet nur die Anzeige).
- **Systemcheck jederzeit:** `docker compose exec wdvi wdvi doctor`
- **Kosten:** console.anthropic.com → *Usage*.

## Updates

```bash
cd ~/wasteddailyvi          # Projektordner
sh scripts/update.sh        # sichert die Datenbank, holt neuen Code, baut neu, startet neu, prüft
```

Aus der Windows-PowerShell (das Projekt liegt in Ubuntu/WSL):

```powershell
wsl sh -c "cd ~/wasteddailyvi && sh scripts/update.sh"
```

Von Hand (Ubuntu/Linux):

```bash
git pull
WDVI_REFRESH=$(date +%s) docker compose build --pull
docker compose up -d
docker compose exec wdvi wdvi doctor
```

Nur mit `WDVI_REFRESH=…` (macht `update.sh` automatisch) holt der Build die neueste yt-dlp-Version samt
Deno (JavaScript für YouTube) und Claude-CLI – wichtig, wenn Downloads plötzlich scheitern; ein einfaches
`docker compose build` nimmt die zwischengespeicherte alte Version. PyTorch & Co. werden dabei nicht neu
geladen. Neue Einstellungen aus `config.example.yaml` kannst du bei Bedarf in deine `config.yaml`
übernehmen (fehlende Einträge haben automatisch Standardwerte; Tippfehler meldet das Programm als
„Unbekannte Einträge“).

Eigene Anpassungen (Port, Heimnetz, Benutzer-ID, CPU-Build) gehören in `.env` bzw.
`docker-compose.override.yml` – **nicht** in `docker-compose.yml` oder andere mitgelieferte Dateien, sonst
bricht `git pull` beim nächsten Update ab.

## Sicherung

`sh scripts/backup.sh` legt `backups/wdvi-<Datum>.tar.gz` an mit Datenbank, `config.yaml`, `.env`,
OAuth-Tokens und `client_secret.json`. **Enthält Geheimnisse** – sicher aufbewahren (nicht in die Cloud
ohne Verschlüsselung). `update.sh` sichert vor jedem Update automatisch.

Wiederherstellen (im Projektordner; `<Datum>` durch den Namen der Sicherung ersetzen):

```bash
docker compose down                                   # 1. Container stoppen (schreibt sonst weiter)
tar -xzf backups/wdvi-<Datum>.tar.gz                  # 2. Archiv entpacken (überschreibt config.yaml/.env/Tokens)
rm -f data/wdvi.db-wal data/wdvi.db-shm               # 3. Reste der alten Datenbank entfernen – wichtig!
cp data/backup/wdvi-<Datum>.db data/wdvi.db           # 4. gesicherte Datenbank einsetzen
[ -f data/backup/wdvi-<Datum>.db-wal ] && cp data/backup/wdvi-<Datum>.db-wal data/wdvi.db-wal
docker compose up -d                                  # 5. starten
```

Ohne Schritt 3 kann SQLite ein altes Änderungsprotokoll (`-wal`) auf die wiederhergestellte Datenbank
anwenden und sie verfälschen.

## Speicherplatz

Fertige Clips brauchen je 20–60 MB. Damit die Platte im Dauerbetrieb nicht vollläuft, räumt
WastedDailyVI täglich um 04:00 UTC auf (Einstellungen unter `cleanup` in der config.yaml):

- Downloads/Zwischendateien: sobald ein Kandidat fertig ist, spätestens nach 2 Tagen.
- Videos gepostete oder endgültig gescheiterter Clips: nach 14 Tagen (`cleanup.keep_posted_days`, 0 = nie).
  Das Vorschaubild bleibt fürs Dashboard; die Videos liegen ja auf den Plattformen.
- Clips, die 7 Tage nicht geprüft wurden, verfallen (`cleanup.review_expire_days`) – virale Momente sind
  dann ohnehin vorbei; ihr Video wird wie bei abgelehnten Clips 7 Tage später gelöscht.
- Videos abgelehnter Clips: nach 7 Tagen.
- Unter 5 GB freiem Speicher (`cleanup.min_free_gb`) werden keine neuen Clips mehr verarbeitet, bis wieder
  Platz ist (Meldung unter Health). Clips, die noch auf Freigabe warten oder eingeplant sind, werden **nie**
  automatisch gelöscht – also nicht von Hand mit `find … -delete` aufräumen.
- Modelle: `data/cache/` (Whisper ~1,6 GB, Musik-Erkennung ~0,3 GB) – nicht löschen, sonst werden sie neu geladen.

## Dauerbetrieb und Neustarts

- Der Container startet nach einem Absturz oder PC-Neustart automatisch (`restart: unless-stopped`),
  sobald Docker läuft. Unter Windows: Docker Desktop → *Settings → General → Start Docker Desktop when you
  sign in*, und Windows so einstellen, dass du automatisch angemeldet wirst oder der PC nicht neu startet.
- **Energiesparmodus aus:** Windows → *Einstellungen → System → Energie* → „Bildschirm aus“ ist okay,
  aber **Standby/Ruhezustand: Nie**.
- Beim Stoppen (`docker compose down`, Update, PC herunterfahren) wartet WastedDailyVI bis zu 10 Minuten,
  bis ein laufender Upload bzw. der aktuelle Clip fertig ist. Das Stoppen kann deshalb etwas dauern.
- Wird trotzdem mitten in einem Upload abgebrochen (Stromausfall, Absturz), lädt WastedDailyVI das Video
  **nicht** automatisch erneut hoch – es könnte schon angekommen sein, und ein Doppelpost schadet mehr.
  Der Post steht dann auf `failed` mit „Upload wurde unterbrochen“: auf der Plattform nachsehen und, falls
  das Video fehlt, `docker compose exec wdvi wdvi requeue <Clip-Nr>`.
- Clips, die beim Neustart gerade verarbeitet wurden, kommen sofort wieder in die Warteschlange.

## Praktische Befehle

| Befehl | Wofür |
|---|---|
| `docker compose exec wdvi wdvi process-url <Link>` | einen bestimmten Clip sofort verarbeiten (Twitch-/Kick-Clip, Reddit-Post, YouTube-Video; bei YouTube optional `--start 120 --end 165`). Gibt es dazu schon einen Clip im Review/freigegeben/eingeplant, bricht der Befehl ab – mit `--force` wird trotzdem neu gerendert |
| `docker compose exec wdvi wdvi discover --source twitch` | sofort nach neuen Twitch-Clips suchen |
| `docker compose exec wdvi wdvi process` | sofort einen Verarbeitungsdurchgang starten |
| `docker compose exec wdvi wdvi requeue <Clip-Nr>` | fehlgeschlagene/unterbrochene Posts eines Clips neu einplanen (wie „↻ Erneut einplanen“ im Dashboard) |
| `docker compose exec wdvi wdvi render-demo` | Test-Render ohne Claude (prüft GPU/Encoder/Schriften) |
| `docker compose exec wdvi wdvi auth youtube` | zeigt, wo du YouTube (neu) verbindest |
| `docker compose exec wdvi wdvi doctor --full` | Systemcheck inkl. Laden des Whisper-Modells |
