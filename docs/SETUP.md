# Einrichtung: alle Zugangsdaten Schritt für Schritt

Diese Anleitung erklärt, woher du jeden Eintrag der Datei **`.env`** bekommst und wie du die Plattformen
verbindest. Du brauchst keine Programmierkenntnisse – nur etwas Geduld beim Durchklicken der
Entwickler-Portale. Plane insgesamt 1–2 Stunden ein (plus Wartezeit auf Prüfungen bei YouTube/TikTok).

> **Wichtig:** Die `.env` enthält Geheimnisse. Nie teilen, nie in Screenshots zeigen, nie in Git hochladen
> (sie steht deshalb in `.gitignore`). Nach jeder Änderung an `.env`: `docker compose up -d`.

## Übersicht

| Was | Wofür | Pflicht? | Abschnitt |
|---|---|---|---|
| `ANTHROPIC_API_KEY` *oder* Claude-CLI-Login | Claude bewertet und schneidet | **ja** (eins von beiden) | [1. Claude](#1-claude) |
| `WDVI_DASHBOARD_PASSWORD` | Schutz des Dashboards | **ja** (Docker) | [8. Dashboard](#8-dashboard-passwort-und-adresse) |
| `WDVI_TWITCH_CLIENT_ID/SECRET` | Twitch-Clips finden | empfohlen | [2. Twitch](#2-twitch) |
| `WDVI_REDDIT_CLIENT_ID/SECRET` | r/GTA6 (stabiler mit Login) | optional | [3. Reddit](#3-reddit) |
| `WDVI_GOOGLE_CLIENT_SECRETS_FILE` | auf YouTube hochladen | für YouTube | [4. YouTube](#4-youtube-hochladen) |
| `WDVI_YOUTUBE_API_KEY` | YouTube-Suche als Quelle | optional | [4. YouTube](#optional-api-schlüssel-für-die-youtube-suche) |
| `WDVI_TIKTOK_CLIENT_KEY/SECRET` | auf TikTok posten | für TikTok | [5. TikTok](#5-tiktok) |
| `WDVI_META_APP_ID/SECRET` | auf Instagram posten | für Instagram | [6. Instagram](#6-instagram) |
| YouTube-Kanal-IDs in `config.yaml` | lange Videos als Quelle | empfohlen | [7. Kanal-IDs](#7-youtube-kanal-ids-finden) |

Nach jedem Abschnitt kannst du mit `docker compose run --rm wdvi wdvi doctor` prüfen, ob es geklappt hat.

---

## 1. Claude

WastedDailyVI kann Claude auf zwei Wegen nutzen. Du brauchst **einen** davon.

### Weg A: API-Key (Standard, `llm.backend: api`)

Abrechnung pro Clip (etwa 0,05–0,15 $, siehe README → Kosten).

1. Auf **console.anthropic.com** registrieren und anmelden.
2. **Billing / Plans & Billing**: Zahlungsmethode hinterlegen und Guthaben aufladen (z. B. 20 $).
   Unter **Limits** ein Monatslimit setzen – dann kann nie mehr ausgegeben werden.
3. **API Keys → Create Key**, Name z. B. „WastedDailyVI“. Den Schlüssel (beginnt mit `sk-ant-`) sofort
   kopieren – er wird nur einmal angezeigt.
4. In `.env` eintragen: `ANTHROPIC_API_KEY=sk-ant-…`
5. In `config.yaml` steht bereits `llm.backend: "api"`.

### Weg B: Claude-Code-CLI mit deinem Claude-Abo (`llm.backend: cli`)

Keine Kosten pro Clip, dafür gelten die Nutzungslimits deines Abos (Pro oder Max; für viele Clips
pro Tag ist Max sinnvoll). Ist das Limit erreicht, pausiert WastedDailyVI automatisch und macht später weiter.

1. In `.env` die Zeile `WITH_CLAUDE_CLI=1` einfügen (Raute davor entfernen).
2. Image neu bauen: `docker compose build` (dauert nur wenige Minuten – PyTorch bleibt im Cache)
3. Einmal anmelden – **eine** der beiden Varianten:
   - **Interaktiv:** `docker compose run --rm wdvi claude` → in der CLI `/login` eingeben → Anmeldung mit
     deinem Claude-Konto wählen → den angezeigten Link im Browser öffnen, anmelden, den Code zurück in die
     CLI kopieren → `/exit`. Der Login wird im Ordner `./claude-home` gespeichert und bleibt erhalten.
   - **Token:** `docker compose run --rm wdvi claude setup-token` → Anweisungen folgen → das ausgegebene
     Token in `.env` als `CLAUDE_CODE_OAUTH_TOKEN=…` eintragen.
4. In `config.yaml`: `llm.backend: "cli"`
5. `docker compose up -d` und `docker compose exec wdvi wdvi doctor` → „Claude (CLI)“ sollte ✅ zeigen.

---

## 2. Twitch

Damit findet WastedDailyVI die meistgesehenen Clips der Kategorie „Grand Theft Auto VI“.

1. Auf **dev.twitch.tv/console** mit deinem Twitch-Konto anmelden. (Twitch verlangt dafür die
   Zwei-Faktor-Authentifizierung – ggf. erst in den Twitch-Einstellungen unter *Sicherheit* aktivieren.)
2. **Anwendungen → Anwendung registrieren** (*Register Your Application*):
   - **Name:** z. B. `WastedDailyVI-Clips` (muss auf Twitch eindeutig sein)
   - **OAuth-Redirect-URLs:** `http://localhost` (wird nicht benutzt, ist aber Pflicht) → *Hinzufügen*
   - **Kategorie:** *Analytics Tool* (oder *Other*)
   - **Client-Typ:** *Vertraulich* (*Confidential*)
   - *Erstellen*
3. Bei der neuen Anwendung auf **Verwalten** klicken:
   - **Client-ID** kopieren → `.env`: `WDVI_TWITCH_CLIENT_ID=…`
   - **Neues Geheimnis** (*New Secret*) → kopieren → `.env`: `WDVI_TWITCH_CLIENT_SECRET=…`
4. In `config.yaml` unter `sources.twitch` kannst du Sprache (`languages`) und Mindest-Aufrufe (`min_views`) anpassen.

---

## 3. Reddit

Ohne Zugangsdaten nutzt WastedDailyVI die öffentliche Reddit-Schnittstelle – das funktioniert, ist aber
strenger begrenzt. Mit eigener App ist es stabiler.

1. Mit dem Reddit-Konto des Kanals anmelden und **reddit.com/prefs/apps** öffnen.
2. Ganz unten **„are you a developer? create an app…“**:
   - **name:** `wasteddailyvi`
   - Typ: **script**
   - **redirect uri:** `http://localhost:8080` (Pflichtfeld, wird nicht benutzt)
   - *create app*
3. Die Zeichenkette direkt unter „personal use script“ ist die **Client-ID**, das Feld **secret** das Geheimnis:
   - `.env`: `WDVI_REDDIT_CLIENT_ID=…` und `WDVI_REDDIT_CLIENT_SECRET=…`
4. In `config.yaml` bei `sources.reddit.user_agent` deinen Reddit-Namen eintragen, z. B.
   `"wdvi/0.1 (by u/DeinName)"` – Reddit verlangt einen eindeutigen User-Agent.

Hinweis: Reddit prüft neue API-Zugänge inzwischen teilweise manuell (Antrag über das Reddit-Hilfe-Center).
Wird deine App nicht freigeschaltet, die beiden Felder einfach leer lassen.

---

## 4. YouTube (Hochladen)

Zum Hochladen braucht WastedDailyVI eine eigene „OAuth-App“ in der Google Cloud. Das ist kostenlos.

### 4.1 Projekt und API

1. **console.cloud.google.com** öffnen, mit dem Google-Konto anmelden, dem der YouTube-Kanal gehört.
2. Oben auf die Projektauswahl → **Neues Projekt** → Name `WastedDailyVI` → *Erstellen* → Projekt auswählen.
3. Menü **APIs & Dienste → Bibliothek** → nach **YouTube Data API v3** suchen → **Aktivieren**.

### 4.2 OAuth-Zustimmungsbildschirm (Google Auth Platform)

1. Menü **APIs & Dienste → OAuth-Zustimmungsbildschirm** (heißt in neuen Konsolen „Google Auth Platform“)
   → *Jetzt starten*.
2. **App-Informationen:** App-Name `WastedDailyVI`, Support-E-Mail = deine Adresse.
3. **Zielgruppe:** *Extern*. Kontaktdaten angeben, Richtlinie akzeptieren, *Erstellen*.
4. **Datenzugriff → Bereiche hinzufügen:** `…/auth/youtube.upload`, `…/auth/youtube.readonly` und
   `…/auth/youtube.force-ssl` → *Aktualisieren* → *Speichern*. (`force-ssl` braucht WastedDailyVI, um ein
   schon bei YouTube geplantes Video beim Abbrechen/Ablehnen wieder zurückzuziehen. Beim Verbinden alle
   Häkchen setzen.)
5. **Zielgruppe → Veröffentlichungsstatus:** auf **„App veröffentlichen“ / „In Produktion“** stellen.
   - Warum? Im Status „Test“ laufen die Anmeldungen nach **7 Tagen** ab – du müsstest YouTube jede Woche
     neu verbinden.
   - Eine Google-Überprüfung ist für die private Nutzung nicht nötig. Beim Verbinden zeigt Google dann
     „Google hat diese App nicht überprüft“ → **Erweitert → Weiter zu WastedDailyVI (unsicher)**. Das ist
     in Ordnung, es ist ja deine eigene App.
   - Bleibst du im Test-Modus: unter *Testnutzer* dein Google-Konto eintragen.

### 4.3 OAuth-Client und client_secret.json

1. **APIs & Dienste → Anmeldedaten → Anmeldedaten erstellen → OAuth-Client-ID** (bzw. *Clients → Client erstellen*).
2. **Anwendungstyp: Webanwendung**, Name `WastedDailyVI Dashboard`.
3. **Autorisierte Weiterleitungs-URIs → URI hinzufügen:**
   ```
   http://localhost:8420/oauth/youtube/callback
   ```
   (Hast du `dashboard.public_base_url` geändert, entsprechend `<deine-Adresse>/oauth/youtube/callback`.)
4. *Erstellen* → **JSON herunterladen**.
5. Die Datei in den Projektordner nach **`data/client_secret.json`** kopieren
   (Windows: im Explorer `\\wsl$\Ubuntu\home\<dein-name>\wasteddailyvi\data\`).
6. In `.env` steht bereits `WDVI_GOOGLE_CLIENT_SECRETS_FILE=data/client_secret.json`.

### 4.4 Verbinden

1. `docker compose up -d`, dann **http://localhost:8420/connections** öffnen → bei **YouTube** auf *Verbinden*.
2. Google-Konto wählen. Gehört der Kanal zu einem **Brand-Konto**, im nächsten Schritt den Kanal
   „Wasted Daily | GTA 6 Clips“ auswählen (nicht dein persönliches Profil).
3. Zugriff erlauben → du landest wieder im Dashboard, YouTube steht auf „verbunden“.

### 4.5 Wichtig: API-Audit und Kontingent

- **Uploads sind zunächst privat gesperrt.** YouTube sperrt Videos, die über ein *ungeprüftes* API-Projekt
  hochgeladen werden, auf „privat“ – auch ein geplanter Veröffentlichungstermin (`publishAt`) macht sie
  nicht öffentlich, und im YouTube Studio lässt sich die Sperre **nicht** selbst aufheben. Erst nach dem
  **YouTube-API-Audit** dürfen API-Uploads öffentlich sein.
  - Antrag: Formular „YouTube API Services – Audit and Quota Extension Form“ (in der Google-Hilfe nach
    „YouTube API audit form“ suchen). Beschreibe ehrlich: eigener Fan-Kanal, Upload eigener bearbeiteter
    Clips, ein Nutzer (du). Screenshots des Dashboards helfen. Die Prüfung dauert Tage bis Wochen.
  - So zeigt es das Dashboard: 10–30 Minuten nach dem geplanten Termin fragt WastedDailyVI bei YouTube
    nach. Ist das Video noch privat, steht es unter **Gepostet** mit rotem Chip **„Privat“** und dem
    Audit-Hinweis – statt still als „gepostet“ zu gelten (Details: [BETRIEB.md](BETRIEB.md#youtube-privat-gesperrte-videos-fehlender-api-audit)).
  - Bis der Audit durch ist: YouTube in `config.yaml` abschalten (`publish.youtube.enabled: false`) und die
    fertigen Videos aus `data/clips/` selbst im YouTube Studio hochladen – oder die privaten Uploads in Kauf
    nehmen. Achtung: Gesperrte Posts zählen als gepostet und lassen sich nicht per „Erneut einplanen“ neu
    hochladen (das gilt nur für fehlgeschlagene Posts) – solche Clips nach dem Audit bei Bedarf selbst aus
    `data/clips/` hochladen, solange die Datei noch da ist (`cleanup.keep_posted_days`, Standard 14 Tage).
- **Kontingent:** Standardmäßig 10.000 Einheiten pro Tag, ein Upload kostet 1.600 → **etwa 6 Uploads pro Tag**
  (`publish.youtube.max_uploads_per_day: 6`). Mehr kannst du im selben Formular beantragen.
- `native_schedule: true`: WastedDailyVI lädt das Video bis zu 24 h vorher als „privat + geplant“ hoch und
  YouTube veröffentlicht es pünktlich – auch wenn dein PC gerade aus ist.

### Optional: API-Schlüssel für die YouTube-Suche

Nur nötig, wenn du in `config.yaml` unter `sources.youtube.search_queries` Suchbegriffe einträgst.

1. **APIs & Dienste → Anmeldedaten → Anmeldedaten erstellen → API-Schlüssel**.
2. Schlüssel bearbeiten → *API-Einschränkungen* → nur **YouTube Data API v3** erlauben → *Speichern*.
3. `.env`: `WDVI_YOUTUBE_API_KEY=…` (Jede Suche kostet 100 Einheiten; WastedDailyVI sucht höchstens alle 6 h.)

---

## 5. TikTok

1. Auf **developers.tiktok.com** mit deinem TikTok-Konto anmelden und ein Entwicklerkonto anlegen
   (Einzelperson reicht).
2. **Manage apps → Connect an app / Create app.** Ausfüllen:
   - App-Symbol (1024×1024), Name `WastedDailyVI`, Kategorie *Entertainment*, Beschreibung
     („Postet meine bearbeiteten GTA-6-Clips als Entwurf in mein TikTok-Konto“).
   - **Terms of Service URL** und **Privacy Policy URL** sind Pflicht – eine einfache öffentliche Seite
     reicht (z. B. eine GitHub-Seite oder ein Notion-Dokument mit zwei Absätzen).
   - Plattform: **Web**.
3. **Produkte hinzufügen** (*Add products*):
   - **Login Kit**
   - **Content Posting API** (für „Direct Post“ zusätzlich den Schalter *Direct Post* aktivieren)
   - **Display API** (für die Aufrufzahlen im Feedback)
4. **Scopes:** `user.info.basic`, `video.upload`, `video.publish`, `video.list`.
5. **Login Kit → Redirect URI (Web):** TikTok verlangt **https**. Trage ein:
   ```
   https://localhost:8420/oauth/tiktok/callback
   ```
   Lehnt TikTok `localhost` ab, nimm eine https-Adresse über Tailscale (siehe
   [BETRIEB.md → Handy-Zugriff](BETRIEB.md#vom-handy-aus-prüfen)) und trage dieselbe Adresse zusätzlich
   in `.env` als `WDVI_TIKTOK_REDIRECT_URI=https://…/oauth/tiktok/callback` ein.
6. **Client Key** und **Client Secret** kopieren → `.env`: `WDVI_TIKTOK_CLIENT_KEY=…`, `WDVI_TIKTOK_CLIENT_SECRET=…`

### Sandbox oder Produktion?

- **Sandbox** (sofort nutzbar): Unter *Sandbox → Target users* dein TikTok-Konto eintragen. Achtung:
  Sandbox und Produktion haben **eigene** Client-Keys – nimm die der Variante, die du nutzt.
- Ohne geprüfte App (Audit) sind **Direktposts nur privat** (`SELF_ONLY`) möglich. Deshalb ist
  `publish.tiktok.mode: "inbox"` voreingestellt: Das Video landet als **Entwurf in deiner TikTok-App**
  (Benachrichtigung „Upload bereit“). Wichtig: Der Entwurf kommt **ohne Text** an – TikTok lässt beim
  Inbox-Upload keine Caption zu. Deshalb: im Dashboard **Gepostet** öffnen (der Post hat den Chip
  „Entwurf“), auf **„📋 Caption kopieren“** tippen, in der TikTok-App den Entwurf öffnen, die Caption
  einfügen (enthält Credit, Fan-Hinweis und Hashtags) und *Posten*. Das ist auch die sicherste Variante.
- **Produktion:** *Submit for review* mit Beschreibung und Demo-Video (Bildschirmaufnahme: Verbinden im
  Dashboard → Video kommt in TikTok an). Erst danach sind öffentliche Direktposts möglich
  (`mode: "direct"`, `privacy_level: "PUBLIC_TO_EVERYONE"`).

### Verbinden (mit „Redirect-URL einfügen“)

1. Dashboard → **Verbindungen → TikTok → Verbinden** → bei TikTok anmelden und zustimmen.
2. Danach zeigt der Browser eine **Fehlerseite** (z. B. „Diese Website ist nicht erreichbar“) – das ist
   normal, weil das Dashboard kein https spricht.
3. Die **komplette Adresse aus der Browserzeile** kopieren (sie beginnt mit
   `https://localhost:8420/oauth/tiktok/callback?code=…`).
4. Zurück im Dashboard unter **Verbindungen → TikTok → „Redirect-URL einfügen“** einfügen und speichern.
   Der Code ist nur wenige Minuten gültig – also zügig.

---

## 6. Instagram

Instagram-Reels werden über die „Instagram API with Instagram Login“ gepostet. Dafür brauchst du **keine**
Facebook-Seite, aber ein **professionelles Instagram-Konto**.

### 6.1 Professionelles Konto

Instagram-App → Profil → Menü (☰) → **Kontotyp und Tools** → **Zu professionellem Konto wechseln** →
*Creator* (oder *Business*), Kategorie z. B. „Gaming-Videoersteller“.

### 6.2 Meta-App anlegen

1. **developers.facebook.com** → *Meine Apps* → **App erstellen**.
2. Anwendungsfall: **„Nachrichten und Inhalte auf Instagram verwalten“**
   (*Manage messaging & content on Instagram*) → weiter → App-Name `WastedDailyVI` → *Erstellen*.
3. Im App-Dashboard: **Anwendungsfälle → Anpassen → „API-Einrichtung mit Instagram-Login“**
   (*API setup with Instagram login*).
   - **Berechtigungen** hinzufügen: `instagram_business_basic`, `instagram_business_content_publish`,
     `instagram_business_manage_insights`.
   - Oben auf dieser Seite stehen **Instagram-App-ID** und **Instagram-App-Geheimcode** (nicht die
     Facebook-App-ID!) → `.env`: `WDVI_META_APP_ID=…`, `WDVI_META_APP_SECRET=…`
   - **„Instagram-Business-Login einrichten“ → Business-Login-Einstellungen → OAuth-Weiterleitungs-URIs:**
     ```
     https://localhost:8420/oauth/instagram/callback
     ```
     → *Speichern*. (Eigene https-Adresse: zusätzlich `WDVI_INSTAGRAM_REDIRECT_URI` in `.env`.)

### 6.3 Rollen im Entwicklungsmodus

Solange die App im **Entwicklungsmodus** ist (das reicht für deinen eigenen Kanal, keine App-Prüfung nötig),
dürfen nur Konten mit einer Rolle in der App sie nutzen:

1. App-Dashboard → **App-Rollen → Rollen → Personen hinzufügen → Instagram-Tester** → Instagram-Name
   `WastedDailyVI` eingeben.
2. Die Einladung annehmen: auf **instagram.com** (Browser) → *Einstellungen* → **Apps und Websites →
   Tester-Einladungen** → *Annehmen*.

### 6.4 Verbinden

- Dashboard → **Verbindungen → Instagram → Verbinden** → anmelden und zustimmen → Fehlerseite → Adresse
  aus der Browserzeile kopieren → im Dashboard unter **„Redirect-URL einfügen“** einfügen (wie bei TikTok).
- **Alternative:** Auf der Seite „API-Einrichtung mit Instagram-Login“ unter *Zugriffstoken generieren* dein
  Konto hinzufügen und **Token generieren**. Token und Instagram-User-ID im Dashboard bei Instagram einfügen.
- Das Token gilt 60 Tage und wird von WastedDailyVI automatisch verlängert, solange der Container läuft.

---

## 7. YouTube-Kanal-IDs finden

Lange YouTube-Videos (z. B. Stream-Highlights großer GTA-Creator) zerlegt WastedDailyVI in Short-Momente.
Dazu brauchst du die **Kanal-ID** (beginnt mit `UC`, 24 Zeichen) – nicht den @Namen.

- **Am einfachsten:** Kanal im Browser öffnen → auf die Kanalbeschreibung („…mehr“) klicken → ganz unten
  **Kanal teilen → Kanal-ID kopieren**.
- Steht in der Adresse `youtube.com/channel/UC…`, ist der Teil ab `UC` die ID.
- **Per Befehl** (im Projektordner):
  ```bash
  docker compose run --rm wdvi yt-dlp --print channel_id --playlist-items 1 "https://www.youtube.com/@KANALNAME/videos"
  ```

In `config.yaml` eintragen:

```yaml
sources:
  youtube:
    channel_ids:
      - "UCxxxxxxxxxxxxxxxxxxxxxx"   # Name des Kanals als Notiz
      - "UCyyyyyyyyyyyyyyyyyyyyyy"
```

Bitte nur Creator eintragen, die Clips ihrer Inhalte erlauben (viele Streamer wünschen sich Clip-Kanäle
ausdrücklich) – oder frag kurz nach. Danach `docker compose restart`.

---

## 8. Dashboard-Passwort und Adresse

- `.env`: `WDVI_DASHBOARD_PASSWORD=…` – ein langes Passwort (z. B. vier zufällige Wörter). Beim Öffnen
  fragt der Browser nach Benutzername (egal) und Passwort. Im Docker-Betrieb ist das Passwort **Pflicht**.
- **Sonderzeichen:** Enthält das Passwort (oder ein anderer Wert in `.env`) ein `$`, ein `#` oder
  Leerzeichen, den Wert in **einfache** Anführungszeichen setzen: `WDVI_DASHBOARD_PASSWORD='Ab$cd#x9 yz'`.
  Ohne Anführungszeichen macht Docker Compose aus `$cd` eine (leere) Variable und schneidet alles ab ` #`
  ab – das gespeicherte Passwort stimmt dann nicht mit dem überein, das du eintippst („401“ für immer).
  Ein einfaches Anführungszeichen `'` im Passwort selbst geht nicht – so ein Zeichen einfach meiden.
- Anderer Port oder Zugriff aus dem Heimnetz: `WDVI_HOST_PORT` bzw. `WDVI_BIND_ADDR` in `.env`
  (siehe `.env.example`) – `docker-compose.yml` bitte nicht bearbeiten, sonst klemmt das nächste Update.
- `config.yaml` → `dashboard.public_base_url`: die Adresse, unter der **du** das Dashboard öffnest.
  Standard `http://localhost:8420`. Sie wird für die OAuth-Weiterleitungen benutzt – verbinde die
  Plattformen am besten direkt am PC.

---

## Checkliste

- [ ] `ANTHROPIC_API_KEY` (oder CLI-Login + `llm.backend: cli`)
- [ ] `WDVI_DASHBOARD_PASSWORD`
- [ ] Twitch Client-ID + Secret
- [ ] YouTube: `data/client_secret.json`, App „In Produktion“, verbunden, Audit beantragt
- [ ] TikTok: Client-Key + Secret, verbunden (Inbox-Modus)
- [ ] Instagram: professionelles Konto, Tester-Einladung angenommen, verbunden
- [ ] Ein paar YouTube-Kanal-IDs in `config.yaml`
- [ ] `docker compose run --rm wdvi wdvi doctor` zeigt keine ❌ mehr
