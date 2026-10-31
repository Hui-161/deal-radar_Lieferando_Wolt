# Deal-Radar — Spezifikation (v4, 01.11.2026)

Webapp für eine Lieferadresse: **Gerichte und Restaurants von Lieferando, Wolt und Uber Eats** gemeinsam
durchsuchen und filtern (vegetarisch/vegan, Küche, Preis, Anbieter, Angebote wie „2 für 1").
Ein Klick öffnet die komplette Speisekarte; bestellt wird auf der echten Website des Anbieters.

## Architektur

- `app.py` — Flask-Server (Port 8765, im Container waitress mit **einem** Prozess), statisches
  Frontend + JSON-API, Sicherheitsheader (CSP mit erlaubten Bild-Hosts), Eingabeprüfung.
- `area.py` — Zustand pro Adresse („Area"): Restaurantlisten beider Anbieter, Speisekarten-Index
  (Lieferando), Wolt-Angebotsprüfung, Wolt-Suchen, Gerichtesuche. Max. 4 Areas, je 30 Min. gültig.
- `providers/food.py` — Küchen-Kategorien beider Anbieter, Diät- und Art-Erkennung
  (Gericht/Getränk/Extra), Textnormalisierung (`fold`: Kleinbuchstaben, ohne Akzente, ä/ae → a).
- `providers/lieferando.py` — Restaurantliste per PLZ, Speisekarten vom Menü-CDN, Angebote.
- `providers/wolt.py` — Restaurantliste per Koordinaten, gedrosselte Angebotsprüfung,
  Speisekarten, Wolts Gerichtesuche; gemeinsame Drossel für alle Wolt-Abfragen.
- `providers/uber.py` — Uber-Eats-Restaurantliste seitenweise (mit Angeboten), Speisekarte beim
  Öffnen; eigene Drossel (≥ 2 s Abstand, 15 Min. Pause nach 403/429/Nicht-JSON), keine Suche.
- `static/index.html` — komplettes Frontend (eine Datei, Vanilla JS, CSS inline, hell/dunkel).
- `betrieb/` — Dockerfile, Compose (nur `127.0.0.1:8765`), `rauchtest.py`.

## Objekte

### Restaurant (beide Anbieter, `GET /api/area/<id>/restaurants`)

```json
{
  "id": "l:burger-vision", "source": "lieferando" | "wolt", "slug": "burger-vision", "rid": "123",
  "name": "Burger Vision", "link": "https://www.lieferando.de/speisekarte/burger-vision",
  "logo": "https://…", "img": "https://…" | null,
  "cuisines": ["burger", "grill"], "tags": ["Burger", "Amerikanisch"],
  "diet": ["vegan", "vegetarian"], "halal": false, "shop": false,
  "rating": 4.3, "rating_count": 3300, "eta": "35-55", "eta_min": 35,
  "delivery_fee_cents": 149, "min_order_cents": 1000,
  "open_now": true, "opens_at": null, "distance_m": 850, "is_new": false,
  "deals": [{"type": "bogof", "text": "2 für 1 Deal"}]
}
```

- `cuisines`: Schlüssel aus `food.CUISINES` (pizza, burger, doener, asiatisch, sushi,
  italienisch, indisch, vietnamesisch, chinesisch, thai, koreanisch, tuerkisch, orientalisch,
  griechisch, mexikanisch, haehnchen, grill, bowls, sandwiches, deutsch, fisch, fruehstueck,
  dessert, shops). `diet` = Restaurant führt vegane/vegetarische Küche laut Anbieter.
- Wolt: `deals` ist `null`, bis die Angebotsprüfung das Restaurant erreicht hat;
  `delivery_fee_cents` immer `null` (gibt Wolt öffentlich nicht heraus), `min_order_cents` erst
  nach der Prüfung. Wolt-Bewertung 0–10 wird halbiert.
- `deals[].type`: `bogof` (2 für 1), `item_discount`, `basket_discount`, `free_delivery`,
  `free_item`, `stampcard`, `other`.

### Gericht (`GET /api/area/<id>/dishes`, `GET /api/menu`)

```json
{
  "id": "…", "name": "Pizza Vegan Margherita", "desc": "…", "price_cents": 1150,
  "orig_price_cents": null, "price_from": true, "img": "https://…", "cat": "Pizza",
  "diet": "vegan" | "vegetarian" | null, "diet_src": "label" | "text" | "option" | "guess" | null,
  "kind": "food" | "drink" | "extra", "deal": {"type": "bogof", "text": "2 für 1", "pct": 100} | null,
  "sold_out": true
}
```

In Suchergebnissen zusätzlich `rest` (Kurzform des Restaurants) und `more_in_rest`.

**Diät-Erkennung** (`food.detect_diet`): 1. Anbieter-Label (`vegan`/`vegetarian`, Wolt
`dietary_preferences`) → `label`. 2. „vegan", „plant-based", „pflanzlich", „vegetarisch",
„veggie", „(v)", „chay", „fleischlos" in Name/Beschreibung/Kategorie → `text`; steht es nur in
der Beschreibung und im Namen ein Fleischwort, zählt es nicht. „vegan möglich/auf Wunsch" →
`option`. 3. Sonst `guess` (vegetarisch), wenn kein Fleisch-/Fisch-Wort vorkommt, eine typische
vegetarische Zutat schon und es kein Burger/Menü/Hot Dog o. Ä. ist; im Restaurant mit „vegan" im
Namen → vegan `guess`. **Nur Hinweise, keine Garantie.**

**Art** (`food.detect_kind`): Kategorie/Label Getränk/Alkohol → `drink`; Dips, Saucen, Extras,
Besteck, Preis < 0,50 € → `extra`. Ohne Suchwort zeigt die Suche nur `food`.

## HTTP-API

- `GET /api/geocode?q=Torstraße 1, 10119 Berlin` → `{ok, lat, lon, city, plz, label}` (Nominatim)
- `GET /api/reverse?lat=..&lon=..` → dasselbe für den Standort-Knopf (nur Deutschland)
- `POST /api/area` Body `{plz, lat, lon}` → `{ok, area, cuisines: [{key, label, emoji}]}` —
  legt die Area an (oder nimmt die vorhandene für dieselbe Adresse) und startet im Hintergrund:
  Lieferando-Liste → Index der 300 nächstgelegenen geöffneten Restaurants (12 parallel, danach
  Angebote gebündelt), und Wolt-Liste → Angebotsprüfung der 100 nächstgelegenen geöffneten
  Restaurants (Warteschlange; was im Zwischenspeicher liegt, sofort), und Uber-Eats-Liste (bis
  5 Seiten). Ohne Seitenabruf pausiert die Wolt-Prüfung nach 90 s und läuft beim nächsten
  Status-Abruf weiter; mehrere Adressen prüfen parallel unter derselben Wolt-Bremse. Kein Vorwärmen.
- `POST /api/area/<id>/prioritize` Body `{slugs: [...]}` (max. 40) → `{ok, queued}` — die Seite
  meldet die Wolt-Restaurants, die sie zeigt; sie rücken in der Angebotsprüfung nach vorn.
- `GET /api/area/<id>/status` → `{lieferando: {status, error, count}, wolt: {…},
  uber: {status (loading/ok/blocked/error), error, count, pages, pages_max},
  index: {status, total, done, failed}, scan: {status, total, done, failed, throttled},
  wolt_deals: {slug: {deals, min_order_cents}}, diet_counts: {"l:slug": [vegan, vegetarisch]}}`;
  `status` ∈ loading/ok/error bzw. waiting/running/done/skipped/stopped. Unbekannte/abgelaufene
  Area → 404 mit `expired: true` (Frontend legt sie neu an).
- `GET /api/area/<id>/restaurants?source=lieferando|wolt|uber` → `{ok, restaurants: [...]}` (Uber-Liste
  wächst seitenweise; das Frontend holt sie neu, wenn sich `uber.pages` ändert)
- `GET /api/area/<id>/dishes?q=&cuisine=&diet=vegan|vegetarian&deals=any|bogof&source=&max_price=1200&open=1&sort=relevance|discount|price|rating|eta&offset=0&limit=48`
  → `{ok, items, total, matches, closed_matches, restaurants, diet, tokens, index, wolt: {state, query, error}}`.
  Diät-Wörter im Suchtext wirken als Filter („vegane pizza" → diet=vegan, Suchwort „pizza").
  Höchstens 3 Gerichte pro Restaurant (`PER_RESTAURANT`), Überzahl in `more_in_rest`.
  Wolt: Suchwort (oder Küche bzw. Diät) startet im Hintergrund Wolts eigene Suche;
  `wolt.state` = none/waiting/loading/done/throttled/error, das Frontend fragt bei `loading` nach.
- `GET /api/menu?source=lieferando|wolt|uber&slug=<slug>&area=<id>` → `{ok, banner, description,
  offers, min_order_cents, cuisines, tags, categories: [{name, items: [...]}]}`; Wolt/Uber bei
  Drosselung oder Sperre 503 mit `busy`. Uber braucht die Area (Lieferadresse); `slug` = Store-UUID.

## Datenquellen (verifiziert am 31.10.2026, alle inoffiziell)

### Lieferando
- **Restaurantliste** (1 Abfrage pro PLZ):
  `GET https://rest.api.eu-central-1.production.jet-external.com/discovery/de/restaurants/enriched/bypostcode/{plz}?serviceType=delivery`
  → `restaurants[]` mit `name`, `uniqueName`, `id`, `logoUrl`, `cuisines[] {name, uniqueName}`,
  `rating.starRating/count`, `deliveryEtaMinutes.rangeLower/rangeUpper`, `deliveryCost` und
  `minimumDeliveryValue` (**Euro**), `isOpenNowForDelivery`, `deliveryOpeningTimeLocal`,
  `driveDistanceMeters`, `address.location.coordinates [lon, lat]`, `deals[] {description, offerType}`.
  offerType: `Bogof`/`BogofMixMatch`→bogof, `ItemLevelDiscount`→item_discount,
  `Percent`/`DiscountAmount`/`TotalOrder`→basket_discount, `FreeItem`→free_item,
  `StampCard`→stampcard, `DeliveryFeeDiscount`→free_delivery; `Notification` am Text eingeordnet.
  Die Website bettet die Liste nur manchmal vollständig ein – deshalb diese Schnittstelle.
- **Speisekarte** (Menü-CDN, keine Drosselung beobachtet):
  1. `GET https://globalmenucdn.eu-central-1.production.jet-external.com/{slug}_de_manifest.json`
     → `RestaurantId`, `ItemsUrl`, `RestaurantInfo` (BannerUrl, Description),
     `Menus[] {ServiceTypes, Categories[] {Name, ItemIds}}` (ItemIds teils `itemId|categoryId`).
  2. `GET …/{ItemsUrl}` → `Items[] {Id, Name, Description, Labels[], ImageSources[].Path,
     Variations[] {Id, BasePrice (EUR), DealOnly}}`. Labels u. a. `vegetarian`, `vegan`, `hot`,
     `alcohol`. Bildpfade enthalten `{transformations}` (Cloudinary).
  3. Angebote: `GET …/consumeroffers/notifications/de?restaurantIds={rid}&optionalProperties=offerMenuItems`
     (bis zu **10** IDs über wiederholten Parameter, mehr → HTTP 400) → `offerNotifications[]
     {restaurantId, description, offerType, offerMenuItems[] {id, discount.discountPercentage}}`;
     id = Item- oder Variations-Id.
  Manifest und Item-Liste werden gleichzeitig geholt (Item-Datei meist `{slug}_de_items.json`),
  je ein Wiederholungsversuch bei Verbindungsfehler/5xx. Speisekarten 6 h im Zwischenspeicher,
  Angebote werden getrennt davon alle 10 Minuten neu aufgetragen (`_deal0` = Deal ohne Angebote).
  Geschäfte (REWE & Co.) haben Item-Listen mit mehreren MB – nicht indiziert.

### Wolt (gemeinsame Drossel: ~15–20 Abfragen/Minute und IP über alle Endpunkte)
- **Restaurantliste** (1 Abfrage): `GET https://restaurant-api.wolt.com/v1/pages/restaurants?lat=..&lon=..`
  → Section `restaurants-delivering-venues`, `items[] {venue, image, filtering, sorting}`;
  `venue.slug/name/tags/rating.score(0–10)/volume/estimate_range/online/delivers/location [lon, lat]/
  brand_image/badges/product_line/venue_preview_items`, `filtering.filters[id=primary].values`
  (englische Küchen-Ids: pizza, burgers, vegan, vegetarian …). `sorting.sortables` sind **Ränge**,
  keine Werte (Entfernung daher selbst aus den Koordinaten berechnet). City-Slug für Links aus
  Top-Level `city`.
- **Gerichtesuche** (1 Abfrage pro Suchbegriff):
  `POST https://restaurant-api.wolt.com/v1/pages/search` Body `{"q": "vegan pizza", "target": "items", "lat": .., "lon": ..}`
  → Section `items` mit bis zu 200 Einträgen `{menu_item {id, name, price, image, is_available},
  link.menu_item_details {description, venue_slug, venue_name, venue_rating, estimate_range,
  city_slug, product_line}}`. Ohne `target` nur je 4 Restaurants/Gerichte.
- **Angebote** (eine Abfrage je Restaurant, geparst 6 h im Zwischenspeicher; eine Sammelquelle gibt
  es nicht – weder Restaurantliste noch `consumer-api.wolt.com/v1/pages/front` enthalten Rabatte):
  `GET https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/{slug}/dynamic?language=de`
  (Weiterleitung folgen, Header `app-language: de`) → `venue_raw.discounts[].effects`
  (`item_discount`/`free_items` mit `include.items`, `basket_discount`, `delivery_discount`),
  Texte aus `venue.banners[].discount.formatted_text`, `order_minimum` (Cent).
- **Speisekarte**: `GET https://consumer-api.wolt.com/consumer-api/consumer-assortment/v1/venues/slug/{slug}/assortment?language=de`
  → `categories[] {name, item_ids, subcategories}`, `items[] {id, name, description, price,
  original_price, images, dietary_preferences, alcohol_permille, disabled_info}`.
  Die Item-Ids der Suche weichen von denen der Speisekarte ab (Frontend sucht dann per Name).
- **Drossel** (`wolt._Throttle`): mind. 1 s zwischen zwei Abfragen; Angebotsprüfung zusätzlich
  4,5 s Abstand (~13/Minute) und lässt wartende Such-/Speisekarten-Abfragen vor; bei 429 Pause
  laut `Retry-After` (sonst 5→60 s), Prüf-Abstand ×1,5 bis 12 s. Interaktive Abfragen geben nach
  45 s Wartezeit mit „Wolt bremst gerade" auf.

### Uber Eats (inoffizielle Web-Schnittstelle von ubereats.com)
- Kopfzeilen: `x-csrf-token: x` (fester Wert der Webseite), `Content-Type: application/json`;
  Lieferadresse als Cookie `uev2.loc` (URL-kodiertes JSON mit `latitude`, `longitude`, Adressfeldern).
- **Liste:** `POST https://www.ubereats.com/_p/api/getFeedV1?localeCode=de` → `data.feedItems[]`, davon
  `type == "REGULAR_STORE"` → `store {storeUuid, title.text, actionUrl, image.items[], rating,
  signposts[].text (Angebote), meta[] (badgeType ETD/FARE/NEW_STORE/EXCLUSIVE_STORE), mapMarker,
  tracking.storePayload {etdInfo.dropoffETARange, ratingInfo, isOrderable, storeAvailablityState}}`.
  Weitere Seiten mit `pageInfo: {offset: data.meta.offset, pageSize: 80}` solange `meta.hasMore`.
  Keine Küche pro Restaurant (nur eine Filterliste) → Küche am Namen erkannt.
- **Speisekarte:** `POST …/_p/api/getStoreV1` Body `{storeUuid, diningMode: "DELIVERY", time: {asap: true}}`
  → `catalogSectionsMap[sectionUuid][].payload.standardItemsPayload {title.text, catalogItems[]}`,
  Reihenfolge über `sections[].uuid`; Gericht `{uuid, title, itemDescription, price (Cent, ggf.
  reduziert), imageUrl, labelPrimary (reduzierter | alter Preis), itemPromotion.buyXGetYItemPromotion,
  promoInfo.promoBadge, endorsement.text ("Vegetarisch."), isSoldOut}`; `cuisineList`, `heroImageUrls`.
- `robots.txt` sperrt für allgemeine Abrufer `*/search?` – die Suche wird nicht benutzt.

### OpenStreetMap / Nominatim
`search` und `reverse` mit `addressdetails=1`, höchstens 1 Abfrage pro Sekunde.

## Frontend (static/index.html)

- Kopf: Adressfeld, Standort-Knopf (⌖, nur über HTTPS), „Suchen"; letzte Adresse in localStorage.
- Reiter **Gerichte** / **Restaurants**, Suchfeld, Küchen-Chips (waagerecht scrollbar, nur Küchen
  mit Restaurants), Filter-Chips (Jetzt geöffnet, Angebote, 2 für 1, Vegetarisch, Vegan; bei
  Restaurants zusätzlich 4,5+, Gratis Lieferung), Auswahl Höchstpreis/Anbieter/Sortierung.
- Unter der Suchleiste angeheftet (sticky): Fortschrittsbalken der laufenden Ladevorgänge
  (Lieferando-Speisekarten, Wolt-Angebote, Uber-Eats-Seiten) mit Kurzhinweis, Knopf
  „ⓘ Einschränkungen" (aufklappbare Erklärung) und eine kurze Meldung, wenn ein Vorgang fertig ist.
  Darunter (nicht angeheftet) die Anzahl Restaurants je Anbieter.
- Gerichte-Karten (Text links, Bild rechts, Deal- und Diät-Badges, Restaurantzeile); Restaurant-
  Karten (Bild/Logo, Anbieter-Badge, Bewertung, Lieferzeit, Entfernung, Gebühren, Angebote,
  Zahl veganer/vegetarischer Gerichte). Endloses Nachladen.
- Seitenpanel: Speisekarte mit Kategorien-Navigation, eigener Suche und Filtern, „Angebote" oben,
  Knopf zum Anbieter. Esc/Hintergrund schließt.
- Polling-Neuzeichnen wartet, bis keine Maustaste gedrückt ist (keine verlorenen Klicks).
- Karten werden per Schlüssel wiederverwendet (`patchGrid`), nicht neu gezeichnet; ist die Seite
  gescrollt, verschieben eintreffende Treffer die Liste nicht, sondern erscheinen über den Knopf
  „↻ Neue Treffer anzeigen". Veraltete Gerichte-Anfragen werden abgebrochen (AbortController).
  Karten außerhalb des Bildschirms mit `content-visibility: auto`.
- Handy (≤ 700 px): Navigation „Gerichte | Restaurants" unten, Adresse als Chip, nur die
  Suchleiste klebt oben, Restaurants als Zeilen, Speisekarte als Vollbild-Blatt mit festem
  Bestell-Knopf; `history.pushState`, damit der Zurück-Knopf die Speisekarte schließt.
- Alle Fremdtexte über `esc()`, Bilder nur von den vier erlaubten Hosts, Links nur zu
  lieferando.de/wolt.com über HTTPS.
