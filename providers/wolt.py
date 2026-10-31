"""Wolt: venue list per location, rate-limited deal scan, menus and dish search."""
import threading
import time
from typing import Optional

import requests

from . import food

_session = requests.Session()
_session.headers.update({"Accept-Language": "de", "app-language": "de"})

LIST_URL = "https://restaurant-api.wolt.com/v1/pages/restaurants?lat={lat}&lon={lon}"
SEARCH_URL = "https://restaurant-api.wolt.com/v1/pages/search"
DYNAMIC_URL = "https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/{slug}/dynamic?language=de"
ASSORTMENT_URL = "https://consumer-api.wolt.com/consumer-api/consumer-assortment/v1/venues/slug/{slug}/assortment?language=de"

# Wolt allows only about 15-20 requests per minute and IP, shared by all endpoints.
# The background deal scan runs at ~13 per minute so that searches and menus still get through.
_MIN_GAP = 1.0
_SCAN_INTERVAL = 4.5
_SCAN_MAX_INTERVAL = 12.0
_INTERACTIVE_MAX_WAIT = 45.0

_CACHE_TTL = 1800
# Venue deals rarely change within hours; keeping them long makes repeated visits instant
DEALS_TTL = 6 * 3600
_list_cache = {}  # (lat, lon) rounded -> (timestamp, result)
_deals_cache = {}  # slug -> (timestamp, parsed deals)
_menu_cache = {}  # slug -> (timestamp, menu)
_cache_lock = threading.Lock()

DEAL_RANK = {"bogof": 0, "free_item": 1, "item_discount": 2}
_GENERIC_CATEGORIES = ("beliebt", "popular", "angebot", "highlight", "empfehl", "bestseller", "top ", "neu")


class WoltBusy(RuntimeError):
    """Wolt is throttling and the request could not be made in reasonable time."""


class _Throttle:
    """
    Shared pacing for all Wolt requests: a minimum gap between any two requests, a slower lane for
    the background scan (which yields to waiting interactive requests) and a global pause after HTTP 429.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._next_any = 0.0
        self._next_scan = 0.0
        self._interactive_waiting = 0
        self.paused_until = 0.0
        self.scan_interval = _SCAN_INTERVAL

    def wait(self, interactive: bool):
        deadline = time.time() + _INTERACTIVE_MAX_WAIT
        if interactive:
            with self._lock:
                self._interactive_waiting += 1
        try:
            while True:
                with self._lock:
                    now = time.time()
                    blocked_until = max(self.paused_until, self._next_any)
                    if not interactive:
                        blocked_until = max(blocked_until, self._next_scan)
                        if self._interactive_waiting:
                            blocked_until = max(blocked_until, now + 0.25)
                    if now >= blocked_until:
                        self._next_any = now + _MIN_GAP
                        if not interactive:
                            self._next_scan = now + self.scan_interval
                        return
                    delay = blocked_until - now
                if interactive and now + delay > deadline:
                    raise WoltBusy("Wolt bremst gerade – bitte in einer Minute erneut versuchen")
                time.sleep(min(delay, 0.5))
        finally:
            if interactive:
                with self._lock:
                    self._interactive_waiting -= 1

    def back_off(self, seconds: float):
        with self._lock:
            self.paused_until = max(self.paused_until, time.time() + seconds)
            self.scan_interval = min(self.scan_interval * 1.5, _SCAN_MAX_INTERVAL)

    def success(self):
        with self._lock:
            self.scan_interval = max(_SCAN_INTERVAL, self.scan_interval * 0.97)

    @property
    def paused(self) -> bool:
        return time.time() < self.paused_until


_throttle = _Throttle()


def throttled() -> bool:
    return _throttle.paused


def _request_json(url: str, interactive: bool, body: Optional[dict] = None, tries: Optional[int] = None) -> dict:
    """GET (or POST with body) through the shared throttle; on 429 honour Retry-After and retry."""
    backoff = 5.0
    for _ in range(tries or (3 if interactive else 6)):
        _throttle.wait(interactive)
        if body is None:
            response = _session.get(url, timeout=15)
        else:
            response = _session.post(url, json=body, timeout=15)
        if response.status_code == 429:
            try:
                wait = float(response.headers.get("Retry-After"))
            except (TypeError, ValueError):
                wait = backoff
            _throttle.back_off(min(wait, 60.0))
            backoff = min(backoff * 2, 60.0)
            continue
        response.raise_for_status()
        _throttle.success()
        return response.json()
    raise WoltBusy("Wolt drosselt die Anfragen weiterhin (HTTP 429)")


def _cached(cache: dict, key, ttl: float = _CACHE_TTL):
    entry = cache.get(key)
    return entry[1] if entry and time.time() - entry[0] < ttl else None


def _store(cache: dict, key, value, max_entries: int = 500, ttl: float = _CACHE_TTL):
    with _cache_lock:
        if len(cache) >= max_entries:
            cutoff = time.time() - ttl
            for k in [k for k, (ts, _) in cache.items() if ts < cutoff] or list(cache)[: max_entries // 5]:
                cache.pop(k, None)
        cache[key] = (time.time(), value)


def _image(url: Optional[str], width: int) -> Optional[str]:
    return f"{url}?w={width}" if url and "?" not in url else url


# --- Venue list ---

def _normalize_venue(entry: dict, lat: float, lon: float, city: str) -> Optional[dict]:
    venue = entry.get("venue") or {}
    slug = venue.get("slug")
    if not slug:
        return None
    primary = [
        value
        for f in (entry.get("filtering") or {}).get("filters") or []
        if f.get("id") == "primary"
        for value in f.get("values") or []
    ]
    rating = venue.get("rating") or {}
    score = rating.get("score")
    location = venue.get("location") or [None, None]
    badges = [b.get("text") for b in venue.get("badges") or [] if b.get("text")]
    return {
        "id": f"w:{slug}",
        "source": "wolt",
        "slug": slug,
        "rid": venue.get("id"),
        "name": venue.get("name", ""),
        "link": f"https://wolt.com/de/deu/{city}/restaurant/{slug}",
        "logo": _image((venue.get("brand_image") or {}).get("url"), 160),
        "img": _image((entry.get("image") or {}).get("url"), 640),
        "cuisines": food.wolt_cuisines(primary),
        "tags": [t.strip() for t in venue.get("tags") or [] if t.strip()][:4],
        "diet": [d for d in ("vegan", "vegetarian") if d in primary],
        "halal": "halal" in primary,
        "shop": venue.get("product_line") not in (None, "restaurant"),
        "rating": round(score / 2, 1) if isinstance(score, (int, float)) else None,
        "rating_count": rating.get("volume") or 0,
        "eta": venue.get("estimate_range") or "",
        "eta_min": food.eta_min(venue.get("estimate_range")),
        "delivery_fee_cents": None,  # not part of the public venue data
        "min_order_cents": None,  # comes with the deal scan
        "open_now": bool(venue.get("online")) and venue.get("delivers") is not False,
        "opens_at": None,
        "distance_m": food.distance_m(lat, lon, location[1], location[0]),
        "is_new": "Neu" in badges,
        "badges": [b for b in badges if b != "Neu"][:2],
        "deals": None,  # unknown until the deal scan reached the venue
    }


def fetch_restaurants(lat: float, lon: float) -> tuple[list[dict], str, dict]:
    """All venues delivering to a location: (restaurants, city slug, preview items per slug)."""
    key = (round(lat, 3), round(lon, 3))
    cached = _cached(_list_cache, key, 600)
    if cached:
        return cached

    data = _request_json(LIST_URL.format(lat=lat, lon=lon), interactive=True)
    city = data.get("city") or "berlin"
    restaurants, previews = [], {}
    for section in data.get("sections") or []:
        if section.get("name") != "restaurants-delivering-venues":
            continue
        for entry in section.get("items") or []:
            rest = _normalize_venue(entry, lat, lon, city)
            if not rest:
                continue
            restaurants.append(rest)
            items = []
            for p in (entry.get("venue") or {}).get("venue_preview_items") or []:
                if p.get("id") and p.get("name"):
                    items.append(food.make_item(
                        p["id"], p["name"], "", p.get("price"), None,
                        _image((p.get("image") or {}).get("url"), 400), "", (), rest["name"],
                    ))
            if items:
                previews[rest["slug"]] = items
    result = (restaurants, city, previews)
    _store(_list_cache, key, result, 50)
    return result


# --- Deals (venue "dynamic" data) ---

def cached_deals(slug: str) -> Optional[dict]:
    """Deals of a venue if they are still in the cache (no request)."""
    return _cached(_deals_cache, slug, DEALS_TTL)


def get_deals(slug: str, interactive: bool) -> dict:
    """Parsed deals of a venue (see parse_dynamic), from the cache or one throttled request."""
    cached = cached_deals(slug)
    if cached is not None:
        return cached
    parsed = parse_dynamic(_request_json(DYNAMIC_URL.format(slug=slug), interactive=interactive))
    _store(_deals_cache, slug, parsed, 3000, DEALS_TTL)
    return parsed


def _banner_texts(data: dict) -> dict:
    """Human-readable discount descriptions, keyed by discount id."""
    texts = {}
    for banner in (data.get("venue") or {}).get("banners") or []:
        bd = banner.get("discount") or {}
        if bd.get("discount_id") and bd.get("formatted_text"):
            texts[bd["discount_id"]] = bd["formatted_text"]
    return texts


def _discount_deal(discount: dict, texts: dict) -> Optional[tuple[dict, list]]:
    effects = discount.get("effects") or {}
    text = texts.get(discount.get("id", ""), "")
    item_discount = effects.get("item_discount")
    free_items = effects.get("free_items")
    item_ids = []
    for effect in (free_items, item_discount):
        include = (effect or {}).get("include") or {}
        item_ids.extend(include.get("items") or [])

    if free_items or (item_discount and item_discount.get("fraction") == 1.0):
        return {"type": "bogof", "text": text or "2 für 1 / Gratis-Artikel", "pct": 100}, item_ids
    if item_discount:
        fraction = item_discount.get("fraction")
        if not text:
            if fraction:
                text = f"{int(fraction * 100)} % auf ausgewählte Gerichte"
            elif item_discount.get("amount_per_item"):
                text = f"{item_discount['amount_per_item'] / 100:.2f} € Rabatt pro Gericht".replace(".", ",")
            else:
                text = "Rabatt auf ausgewählte Gerichte"
        return {"type": "item_discount", "text": text, "pct": round(fraction * 100) if fraction else None}, item_ids
    basket = effects.get("basket_discount")
    if basket:
        if not text:
            if basket.get("amount"):
                text = f"{basket['amount'] / 100:.0f} € Rabatt"
            elif basket.get("fraction"):
                text = f"{int(basket['fraction'] * 100)} % auf den Warenkorb"
            else:
                text = "Warenkorb-Rabatt"
        return {"type": "basket_discount", "text": text, "pct": None}, []
    if effects.get("delivery_discount"):
        return {"type": "free_delivery", "text": text or "Gratis Lieferung", "pct": None}, []
    return None


def parse_dynamic(data: dict) -> dict:
    """Venue deals, the deal per item id and the minimum order value."""
    texts = _banner_texts(data)
    deals, item_deals = [], {}
    for discount in (data.get("venue_raw") or {}).get("discounts") or []:
        parsed = _discount_deal(discount, texts)
        if not parsed:
            continue
        deal, item_ids = parsed
        deals.append({"type": deal["type"], "text": deal["text"]})
        for item_id in item_ids:
            current = item_deals.get(item_id)
            if current is None or DEAL_RANK.get(deal["type"], 9) < DEAL_RANK.get(current["type"], 9):
                item_deals[item_id] = deal
    order_minimum = data.get("order_minimum")
    return {
        "deals": deals,
        "item_deals": item_deals,
        "min_order_cents": order_minimum if isinstance(order_minimum, int) else None,
    }


# --- Menu ---

def fetch_menu(slug: str, restaurant_name: str = "") -> dict:
    """Full menu of a venue incl. diet, kind and deals per item. Raises on error (callers catch)."""
    cached = _cached(_menu_cache, slug)
    if cached is not None:
        return cached

    dynamic = get_deals(slug, interactive=True)
    data = _request_json(ASSORTMENT_URL.format(slug=slug), interactive=True)

    # Flatten categories (they may contain subcategories) and pick a specific category per item
    categories, item_category = [], {}

    def add_category(cat: dict, prefix: str = ""):
        name = (cat.get("name") or "").strip()
        full_name = f"{prefix} · {name}" if prefix and name else (name or prefix)
        ids = [i for i in cat.get("item_ids") or [] if i]
        if ids:
            categories.append({"name": full_name, "ids": ids})
            generic = any(g in full_name.lower() for g in _GENERIC_CATEGORIES)
            for item_id in ids:
                if item_id not in item_category or (not generic and item_category[item_id][1]):
                    item_category[item_id] = (full_name, generic)
        for sub in cat.get("subcategories") or []:
            add_category(sub, name)

    for cat in data.get("categories") or []:
        add_category(cat)

    items = {}
    for raw in data.get("items") or []:
        item_id = raw.get("id")
        if not item_id or not isinstance(raw.get("price"), int):
            continue
        labels = [str(p) for p in raw.get("dietary_preferences") or []]
        if raw.get("alcohol_permille"):
            labels.append("alcohol")
        image = (raw.get("images") or [{}])[0].get("url")
        item = food.make_item(
            item_id, raw.get("name"), raw.get("description"), raw["price"], raw.get("original_price"),
            _image(image, 400), item_category.get(item_id, ("", False))[0], labels, restaurant_name,
        )
        deal = dynamic["item_deals"].get(item_id)
        if deal:
            item["deal"] = dict(deal)
        elif item["orig_price_cents"]:
            pct = round((1 - item["price_cents"] / item["orig_price_cents"]) * 100)
            item["deal"] = {"type": "item_discount", "text": "Direkt reduziert", "pct": pct}
        if raw.get("disabled_info"):
            item["sold_out"] = True
        items[item_id] = item

    for cat in categories:
        cat["ids"] = [i for i in dict.fromkeys(cat["ids"]) if i in items]
    menu = {
        "slug": slug,
        "banner": None,
        "description": "",
        "categories": [c for c in categories if c["ids"]],
        "items": items,
        "offers": dynamic["deals"],
        "min_order_cents": dynamic["min_order_cents"],
        "dynamic": dynamic,
    }
    _store(_menu_cache, slug, menu, 200)
    return menu


# --- Dish search ---

def search_items(query: str, lat: float, lon: float) -> list[tuple[dict, dict]]:
    """Wolt's own dish search around a location (up to ~200 hits): [(venue, item)]."""
    data = _request_json(SEARCH_URL, interactive=True, body={"q": query, "target": "items", "lat": lat, "lon": lon})
    results = []
    for section in data.get("sections") or []:
        for entry in section.get("items") or []:
            menu_item = entry.get("menu_item")
            details = (entry.get("link") or {}).get("menu_item_details") or {}
            slug = details.get("venue_slug")
            if not menu_item or not slug or details.get("product_line", "restaurant") != "restaurant":
                continue
            item = food.make_item(
                menu_item.get("id"), menu_item.get("name"), details.get("description"), menu_item.get("price"),
                menu_item.get("original_price"), _image((menu_item.get("image") or {}).get("url"), 400), "", (),
                details.get("venue_name", ""),
            )
            if menu_item.get("is_available") is False:
                item["sold_out"] = True
            rating = (details.get("venue_rating") or {}).get("score")
            venue = {
                "slug": slug,
                "name": details.get("venue_name") or menu_item.get("venue_name") or "",
                "city": details.get("city_slug") or "berlin",
                "rating": round(rating / 2, 1) if isinstance(rating, (int, float)) else None,
                "eta": details.get("estimate_range") or menu_item.get("estimate_range") or "",
                "logo": _image((details.get("venue_image") or {}).get("url"), 160),
            }
            results.append((venue, item))
    return results
