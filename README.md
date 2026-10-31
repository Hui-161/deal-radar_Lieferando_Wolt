# Deal-Radar 🍔

Gerichte und Restaurants von **Lieferando, Wolt und Uber Eats** in einer Suche: Was gibt es in meiner
Nähe als vegane Pizza, wo gibt es Pad Thai unter 12 €, welche Inder haben gerade 2-für-1-Deals?
Filter für vegetarisch/vegan, Küche, Preis, Anbieter und Angebote. Ein Klick zeigt die komplette
Speisekarte; bestellt wird wie gewohnt beim Anbieter.

## Start (lokal)

```
pip install -r requirements.txt
python app.py
```

Dann im Browser: **http://localhost:8765** → Lieferadresse eingeben (z. B. „Torstraße 1, 10119
Berlin"). Die Restaurants erscheinen sofort; die Speisekarten der nächstgelegenen geöffneten
Lieferando-Restaurants sind nach etwa 10 Sekunden durchsuchbar. Ohne Besucher läuft nichts im
Hintergrund.

## Betrieb als Container

```
sudo docker compose -f betrieb/docker-compose.yml up -d --build
sudo python3 betrieb/rauchtest.py      # Prüfung nach einem Update
```

Lauscht nur auf `127.0.0.1:8765` (für einen Reverse-Proxy/Tunnel davor), läuft als
Nicht-root mit schreibgeschütztem Dateisystem, hält keine Daten (nur Zwischenspeicher im RAM,
etwa 120 MB pro gesuchter Adresse).

## Wie es funktioniert

- **Lieferando:** Restaurantliste per PLZ über die Discovery-Schnittstelle, Speisekarten
  (Kategorien, Gerichte, Kennzeichen vegetarisch/vegan, Bilder) über das Menü-CDN, Angebote über
  die Angebots-Schnittstelle. Die 300 nächstgelegenen geöffneten Restaurants werden beim Laden
  einer Adresse indiziert.
- **Wolt:** Restaurantliste per Koordinaten, Gerichte über Wolts eigene Suche (eine Abfrage pro
  Suchbegriff), Angebote nach und nach pro Restaurant. Wolt erlaubt nur etwa 15–20 Abfragen pro
  Minute; die App hält sich daran.
- **Uber Eats:** Restaurantliste seitenweise samt Angeboten (bis 5 Seiten, mindestens 2 s Abstand,
  ehrliche Kennung, keine Suche – die sperrt Ubers `robots.txt`). Speisekarten erst beim Öffnen
  eines Restaurants; bei einer Sperre (403/429) 15 Minuten Pause.
- Laufende Ladevorgänge stehen als Balken direkt unter der Suchleiste, mit Kurzhinweis und
  „ⓘ Einschränkungen".
- **Vegetarisch/vegan** kommt aus den Kennzeichen der Anbieter, aus Name/Beschreibung oder – als
  „vermutlich" markiert – aus den Zutaten. Im Zweifel beim Anbieter nachsehen.

Alles **inoffizielle** Endpunkte — kann jederzeit brechen, bitte mit moderater Frequenz nutzen
(privater Gebrauch). Details zu Schnittstellen und Datenformaten in `SPEC.md`.

## Dateien

- `app.py` — Flask-App (Port 8765), API und Sicherheitsheader
- `area.py` — Zustand pro Adresse: Restaurantlisten, Speisekarten-Index, Gerichtesuche
- `providers/lieferando.py`, `providers/wolt.py`, `providers/uber.py` — Datenbeschaffung + Normalisierung
- `providers/food.py` — Küchen, Erkennung vegetarisch/vegan, Getränke/Extras
- `static/index.html` — Frontend (eine Datei)
- `betrieb/` — Dockerfile, Compose, Rauchtest
- `SPEC.md` — Spezifikation / Datenquellen-Doku
