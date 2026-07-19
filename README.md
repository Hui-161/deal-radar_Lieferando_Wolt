# Deal-Radar 🍔

Kleine lokale Webapp, die Lieferando- und Wolt-Restaurants nach Rabatten filtert —
insbesondere **"2 für 1"-Deals**. Klick auf ein Restaurant öffnet es direkt auf der
echten Website (dort normal bestellen, ggf. öffnet sich die App).

## Start

```
pip install flask requests
python app.py
```

Dann im Browser: **http://localhost:8765**

Adresse eingeben (z.B. "Torstraße 1, 10119 Berlin") → Lieferando-Deals erscheinen
sofort, Wolt wird im Hintergrund gescannt (dauert ~1 Minute, Fortschritt wird
angezeigt). "Gerichte anzeigen ▾" auf einer Karte lädt die konkreten Deal-Gerichte
mit Preis und Rabatt ("2. gratis" bei 2-für-1, "−26 %" bei Prozent-Aktionen).

## Wie es funktioniert

- **Lieferando:** Die Restaurantliste einer PLZ wird als eine einzige Seite geladen;
  darin steckt ein JSON mit allen Restaurants inkl. `deals` (offerType `Bogof` = 2 für 1).
- **Wolt:** Venue-Liste per Koordinaten (Geocoding via OpenStreetMap/Nominatim), dann
  pro Restaurant ein Detail-Request mit strukturierten Rabatten (max. 8 parallel).

Beides sind **inoffizielle** Endpunkte — kann jederzeit brechen, bitte mit moderater
Frequenz nutzen (privater Gebrauch).

## Dateien

- `app.py` — Flask-Server (Port 8765)
- `providers/lieferando.py`, `providers/wolt.py` — Datenbeschaffung + Normalisierung
- `static/index.html` — Frontend (eine Datei)
- `SPEC.md` — Spezifikation / Datenquellen-Doku
