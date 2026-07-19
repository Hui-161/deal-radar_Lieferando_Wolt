# Deal-Radar — Spezifikation

Kleine lokale Webapp: Lieferando- und Wolt-Restaurants nach Rabatten filtern
(v.a. "2 für 1"), Klick öffnet das Restaurant auf der echten Website.

## Architektur

- `app.py` — Flask-Server (Port 8765), statisches Frontend + JSON-API. (fertig, nicht anfassen)
- `providers/lieferando.py` — Modul, holt & normalisiert Lieferando-Daten.
- `providers/wolt.py` — Modul, holt & normalisiert Wolt-Daten (Scan mit ThreadPool).
- `static/index.html` — komplettes Frontend (ein File, CSS/JS inline).

## Normalisiertes Restaurant-Objekt (beide Provider liefern exakt dieses Format)

```json
{
  "source": "lieferando" | "wolt",
  "name": "Burger Vision Torstraße",
  "link": "https://www.lieferando.de/speisekarte/burger-vision",
  "rating": 4.3,
  "rating_count": 3300,
  "eta": "35-55",
  "delivery_fee_cents": 149,
  "min_order_cents": 0,
  "open_now": true,
  "deals": [
    { "type": "bogof", "text": "2 für 1 Deal" }
  ]
}
```

`deals[].type` — genau eine dieser Kategorien:
- `bogof` — 2-für-1 / Buy-one-get-one (auch "1 kaufen, 2. halber Preis")
- `item_discount` — % oder €-Rabatt auf ausgewählte Produkte/Gerichte
- `basket_discount` — Rabatt auf den Warenkorb (z.B. "25 % ab 25 €", "10 € auf erste Bestellung")
- `free_delivery` — Gratis-/rabattierte Lieferung
- `free_item` — Gratis-Artikel
- `stampcard` — Treuekarte (wird im UI standardmäßig ausgeblendet)
- `other` — alles andere

`text` = menschenlesbare Beschreibung (Original übernehmen wenn vorhanden).
Restaurants OHNE Deals werden von den Providern NICHT zurückgegeben.

## HTTP-API (app.py ruft Provider-Funktionen auf)

- `GET /api/lieferando?city=berlin&plz=10178`
  → `{"ok": true, "restaurants": [...]}` oder `{"ok": false, "error": "..."}`
  Provider-Funktion: `providers.lieferando.fetch_deals(city: str, plz: str) -> list[dict]`

- `GET /api/geocode?q=10178 Berlin`
  → `{"ok": true, "lat": 52.52, "lon": 13.40}` (Nominatim, in app.py fertig)

- `POST /api/wolt/scan` Body `{"lat": .., "lon": .., "limit": 150}`
  → `{"ok": true, "scan_id": "..."}` — startet Hintergrund-Scan
  Provider: `providers.wolt.start_scan(lat, lon, limit) -> scan_id` (nicht-blockierend)

- `GET /api/wolt/scan/<scan_id>`
  → `{"ok": true, "done": false, "scanned": 40, "total": 150, "restaurants": [...bisher gefundene...]}`
  Provider: `providers.wolt.get_scan(scan_id) -> dict`

## Datenquellen (verifiziert am 19.07.2026, Beispieldateien im scratchpad)

### Lieferando (1 Request pro Stadt/PLZ)
`GET https://www.lieferando.de/lieferservice/essen/{city}-{plz}` (User-Agent Browser, Accept-Language: de)
→ HTML (~8 MB), darin `<script id="__NEXT_DATA__" type="application/json">…</script>`.
JSON-Pfad: `props.appProps.preloadedState.discovery.restaurantList.restaurantData`
= dict {id: restaurant}. Relevante Felder pro Restaurant:
- `name`, `uniqueName` (Link: `https://www.lieferando.de/speisekarte/{uniqueName}`)
- `rating.starRating`, `rating.count`
- `deliveryEtaMinutes.rangeLower/rangeUpper`
- `deliveryFees.byMinFee.fee` (Cent), `deliveryFees.byMinOrder.minimumAmount` (Cent = Mindestbestellwert)
- `availability.delivery.isOpen` → open_now
- `deals`: Liste `{"description": "2 für 1 Deal", "offerType": "Bogof"}`
  offerType-Mapping: `Bogof`/`BogofMixMatch`→bogof, `ItemLevelDiscount`→item_discount,
  `Percent`/`DiscountAmount`→basket_discount, `FreeItem`→free_item, `StampCard`→stampcard,
  `DeliveryFeeDiscount`→free_delivery, sonst→other.
  Leere description bei StampCard → text "Treuekarte".

### Wolt (1 Request Liste + 1 Request pro Venue)
1. `GET https://restaurant-api.wolt.com/v1/pages/restaurants?lat={lat}&lon={lon}`
   → `sections[]`, Section mit `name == "restaurants-delivering-venues"`, deren `items[]`:
   - `venue.name`, `venue.slug`, `venue.city` (kann leer sein → City-Slug aus Top-Level `city` des Response nehmen),
     `venue.rating.score` (0-10! durch 2 teilen für 5er-Skala), `venue.rating.volume`,
     `venue.estimate_range` ("15-25"), `venue.online`
   - sortiert nach `sorting.sortables[id=="distance"].value` aufsteigend, dann erste `limit` scannen
2. Pro Venue: `GET https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/{slug}/dynamic?language=de`
   (folgt Redirect, ThreadPool max 8 parallel, timeout 10s, Fehler pro Venue still schlucken)
   → `venue_raw.discounts[]` mit `effects`:
   - `item_discount` != null → type item_discount (fraction → "26 % auf ausgewählte Gerichte";
     `free_items` in effects != null ODER (item_discount.fraction == 1.0) → bogof/free_item)
   - `basket_discount` != null → basket_discount (amount in Cent → "10 € Rabatt")
   - `delivery_discount` != null → free_delivery
   Text: falls `venue.banners[]` einen Eintrag mit gleicher `discount.discount_id` hat →
   dessen `formatted_text` verwenden, sonst aus effects generieren.
   Link: `https://wolt.com/de/deu/{city_slug}/restaurant/{slug}`
   `open_now` = `venue.online` aus der Liste.

## V2-Erweiterung: Adresssuche + Deal-Gerichte (verifiziert 19.07.2026)

### Geocode liefert jetzt auch Stadt/PLZ
`GET /api/geocode?q=Torstraße 1, 10119 Berlin`
→ `{"ok": true, "lat": .., "lon": .., "city": "berlin", "plz": "10119"}`
(Nominatim mit `addressdetails=1`; city = address.city|town|village, lowercase,
transliteriert ä→ae ö→oe ü→ue ß→ss, Leerzeichen→"-". Fertig in app.py.)
Frontend nutzt EIN Adressfeld statt Stadt+PLZ; city/plz gehen an /api/lieferando,
lat/lon an den Wolt-Scan.

### Deal-Gerichte pro Restaurant (on demand beim Aufklappen einer Karte)
`GET /api/deal-items?source=lieferando|wolt&slug=<slug>`
→ `{"ok": true, "offers": [{"type": "bogof", "text": "Bestelle 1x 20 Chicken
McNuggets® …", "items": [{"name": "20 Chicken McNuggets®", "price_cents": 1149,
"discount_percentage": 100}]}]}`
Provider-Funktionen: `providers.lieferando.fetch_deal_items(slug)` und
`providers.wolt.fetch_deal_items(slug)` → jeweils `list[dict]` (offers-Liste).
In-Memory-Cache pro slug, TTL 10 Minuten. Fehler → Exception (app.py fängt).

**Lieferando-Quelle:**
1. `GET https://www.lieferando.de/speisekarte/{slug}` (gleiche Header wie Liste)
   → `__NEXT_DATA__` → `props.appProps.preloadedState.menu.restaurant.cdn.items`
   = dict itemId → `{name, variations: [{basePrice: 18.69 (EUR float!), …}]}`
   → price_cents = round(basePrice*100). restaurantId per Regex
   `"restaurantId":"(\d+)"` aus dem HTML (erste nicht-leere Zahl).
2. `GET https://rest.api.eu-central-1.production.jet-external.com/consumeroffers/notifications/de?restaurantIds={rid}&optionalProperties=offerMenuItems`
   (kein Auth nötig) → `offerNotifications[]` mit `description`, `offerType`
   (Mapping wie oben), `offerMenuItems[]: {id, discount: {discountPercentage}}`.
3. Join: offerMenuItems.id in cdn.items nachschlagen; unbekannte IDs überspringen.
   Offers ohne auflösbare Items trotzdem zurückgeben (items: []).

**Wolt-Quelle:**
1. Dynamic-Endpoint wie beim Scan → `venue_raw.discounts[]`; Texte via banners
   (bestehende Parse-Logik wiederverwenden).
2. `GET https://consumer-api.wolt.com/consumer-api/consumer-assortment/v1/venues/slug/{slug}/assortment?language=de`
   → `items[]: {id, name, price (Cent int), original_price (Cent|null)}`
3. Join: `effects.item_discount.include.items` bzw. `effects.free_items` IDs →
   Namen/Preise; discount_percentage = round(fraction*100) falls fraction.
4. Zusätzlich synthetisches Offer `{"type": "item_discount", "text": "Direkt
   reduzierte Gerichte"}` mit allen Assortment-Items, bei denen original_price
   gesetzt und > price ist (discount_percentage aus Differenz, price_cents =
   reduzierter Preis). Nur anhängen, wenn nicht leer.

### Frontend-Erweiterung
- Karten bekommen Button "Gerichte anzeigen ▾" → lädt /api/deal-items
  (Spinner im Button), klappt Bereich auf: pro Offer Deal-Badge + Text,
  darunter Gerichte als "Name — 11,49 €" (bei discount_percentage 100 →
  Zusatz-Badge "2. gratis"; bei <100 → "−26 %"). Ergebnis im JS cachen,
  zweiter Klick toggelt nur. Fehler → kleine rote Zeile im Aufklappbereich.
- slug muss dafür im Restaurant-Objekt stehen: Provider liefern zusätzlich
  `"slug": "<slug>"` in jedem Restaurant (lieferando: uniqueName, wolt: slug).

## Frontend (static/index.html, deutsch, dark-mode-fähig via prefers-color-scheme)

- Kopfzeile: Eingabe Stadt + PLZ, Button "Suchen".
  - Lieferando-Ergebnisse sofort laden und anzeigen.
  - Parallel Geocode → Wolt-Scan starten, Fortschrittsbalken ("Wolt: 40/150 gescannt"),
    Ergebnisse progressiv einsortieren (Polling alle 1,5 s bis done).
- Filter-Chips (Mehrfachauswahl): "2 für 1" (bogof), "% auf Gerichte" (item_discount),
  "Warenkorb-Rabatt" (basket_discount), "Gratis-Lieferung" (free_delivery),
  "Gratis-Artikel" (free_item). Default: "2 für 1" aktiv. Stampcard/other nie anzeigen
  als Chip, Restaurants nur zeigen wenn ≥1 sichtbarer Deal zum aktiven Filter passt.
- Toggle "nur geöffnete" (default an). Quelle-Badges (Lieferando orange, Wolt blau).
- Karte pro Restaurant: Name (Link, target=_blank), Sterne + Anzahl, ETA, Lieferkosten,
  Mindestbestellwert, Deal-Badges (bogof hervorgehoben). Sortierung: bogof-Deals zuerst,
  dann Rating absteigend.
- Kein Framework, Vanilla JS, fetch. Stadt/PLZ in localStorage merken.
