"""Lieferando (Just Eat Takeaway): restaurant list per postcode, full menus from the menu CDN, offers."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

from . import food

OFFER_TYPE_MAPPING = {
    "Bogof": "bogof",
    "BogofMixMatch": "bogof",
    "ItemLevelDiscount": "item_discount",
    "Percent": "basket_discount",
    "DiscountAmount": "basket_discount",
    "TotalOrder": "basket_discount",
    "FreeItem": "free_item",
    "StampCard": "stampcard",
    "DeliveryFeeDiscount": "free_delivery",
}
# Which deal wins when several apply to the same dish
DEAL_RANK = {"bogof": 0, "free_item": 1, "item_discount": 2}

HEADERS = {
    "User-Agent": "deal-radar/1.0 (privat)",
    "Accept-Language": "de",
}
JET_API = "https://rest.api.eu-central-1.production.jet-external.com"
MENU_CDN = "https://globalmenucdn.eu-central-1.production.jet-external.com"
OFFERS_API = f"{JET_API}/consumeroffers/notifications/de"

_session = requests.Session()
_session.headers.update(HEADERS)
# Index workers, item prefetch and offer batches run at the same time
_session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=48))
# Fetches the item list while the manifest is still loading (one round trip instead of two)
_items_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="lief-items")

# Menus change rarely and are kept for hours; offers are refreshed separately and much more often
_menu_cache = {}  # slug -> (timestamp, menu)
_menu_lock = threading.Lock()
MENU_TTL = 6 * 3600
MENU_CACHE_MAX = 600
OFFERS_TTL = 600
_OFFERS_BATCH = 10  # the offers API rejects more than 10 restaurant ids per request
# Generic categories that repeat dishes from elsewhere in the menu
_GENERIC_CATEGORIES = ("beliebt", "popular", "angebot", "highlight", "empfehl", "bestseller", "top ", "neu")


def _classify(offer_type: str, text: str) -> str:
    """Map offerType to a deal type; 'Notification' offers only carry free text."""
    if offer_type in OFFER_TYPE_MAPPING:
        return OFFER_TYPE_MAPPING[offer_type]
    t = text.lower()
    if "2 für 1" in t or "2. zum halben" in t or "das günstigste ist gratis" in t:
        return "bogof"
    if "warenkorb" in t or "bestellung" in t:
        return "basket_discount"
    if "lieferung" in t or "liefergebühr" in t:
        return "free_delivery"
    if "gratis" in t:
        return "free_item"
    return "other"


def _cents(euros) -> Optional[int]:
    return round(euros * 100) if isinstance(euros, (int, float)) else None


def _image(path: Optional[str], transform: str = "f_auto,q_auto,c_fill,w_320,h_240") -> Optional[str]:
    """Cloudinary paths carry a '{transformations}' placeholder."""
    return path.replace("{transformations}", transform) if path else None


def _deal(offer: dict) -> dict:
    text = (offer.get("description") or "").strip()
    deal_type = _classify(offer.get("offerType", ""), text)
    if deal_type == "stampcard" and not text:
        text = "Treuekarte"
    return {"type": deal_type, "text": text}


def _normalize_restaurant(rest_data: dict, lat: Optional[float], lon: Optional[float]) -> dict:
    cuisines = rest_data.get("cuisines") or []
    keys = {c.get("uniqueName") for c in cuisines}
    rating = rest_data.get("rating") or {}
    eta_data = rest_data.get("deliveryEtaMinutes") or {}
    eta = f"{eta_data['rangeLower']}-{eta_data['rangeUpper']}" if "rangeLower" in eta_data and "rangeUpper" in eta_data else ""
    coords = ((rest_data.get("address") or {}).get("location") or {}).get("coordinates") or [None, None]
    unique_name = rest_data.get("uniqueName", "")
    logo = rest_data.get("logoUrl") or None
    if logo:
        logo = logo.replace("c_pad,h_100,w_100", "c_pad,h_160,w_160")
    open_now = bool(rest_data.get("isOpenNowForDelivery"))

    return {
        "id": f"l:{unique_name}",
        "source": "lieferando",
        "slug": unique_name,
        "rid": str(rest_data.get("id", "")),
        "name": rest_data.get("name", ""),
        "link": f"https://www.lieferando.de/speisekarte/{unique_name}" if unique_name else "",
        "logo": logo,
        "img": None,
        "cuisines": food.lieferando_cuisines(list(keys)),
        "tags": food.lieferando_tags(cuisines),
        "diet": [d for d, k in (("vegan", "vegan-essen"), ("vegetarian", "vegetarisches-essen")) if k in keys],
        "halal": bool(keys & {"100-prozent-halal", "halal"}),
        "shop": bool(keys & food.SHOP_KEYS),
        "rating": rating.get("starRating"),
        "rating_count": rating.get("count") or 0,
        "eta": eta,
        "eta_min": eta_data.get("rangeLower"),
        # The discovery API reports fees in euros
        "delivery_fee_cents": _cents(rest_data.get("deliveryCost")),
        "min_order_cents": _cents(rest_data.get("minimumDeliveryValue")),
        "open_now": open_now,
        "opens_at": None if open_now else rest_data.get("deliveryOpeningTimeLocal"),
        "distance_m": rest_data.get("driveDistanceMeters") or food.distance_m(lat, lon, coords[1], coords[0]),
        "is_new": bool(rest_data.get("isNew")),
        "deals": list({(d["type"], d["text"]): d for d in map(_deal, rest_data.get("deals") or [])}.values()),
    }


def fetch_restaurants(plz: str, lat: Optional[float] = None, lon: Optional[float] = None) -> list[dict]:
    """All restaurants delivering to a postcode, normalized (with and without deals)."""
    # The website only sometimes embeds the full list; this API always returns all of it
    response = _session.get(
        f"{JET_API}/discovery/de/restaurants/enriched/bypostcode/{plz}",
        params={"serviceType": "delivery"},
        timeout=30,
    )
    response.raise_for_status()
    return [
        _normalize_restaurant(r, lat, lon)
        for r in response.json().get("restaurants") or []
        if r.get("uniqueName") and not r.get("isTestRestaurant")
    ]


# --- Menus ---

def _pick_menu(manifest: dict) -> dict:
    menus = manifest.get("Menus") or [{}]
    for menu in menus:
        if "delivery" in (menu.get("ServiceTypes") or []):
            return menu
    return menus[0]


def _get(url: str, timeout: int = 30) -> requests.Response:
    """GET from the menu CDN; one retry on connection problems and server errors (brief CDN hiccups)."""
    for attempt in (1, 2):
        try:
            response = _session.get(url, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 2:
                raise
            continue
        if response.status_code >= 500 and attempt == 1:
            continue
        response.raise_for_status()
        return response


def fetch_menu(slug: str, restaurant_name: str = "", max_bytes: Optional[int] = None) -> dict:
    """
    Full menu of a restaurant: categories with item ids and normalized items (incl. diet and kind).
    Offers are not included - see refresh_offers(). Raises on error (callers catch).
    """
    cached = _menu_cache.get(slug)
    if cached and time.time() - cached[0] < MENU_TTL:
        return cached[1]

    # Manifest (restaurant id, categories) and the usual item file at the same time
    guessed_items = f"{slug}_de_items.json"
    items_future = _items_pool.submit(_get, f"{MENU_CDN}/{guessed_items}")
    manifest = _get(f"{MENU_CDN}/{slug}_de_manifest.json", timeout=20).json()
    restaurant_id = manifest.get("RestaurantId")
    if not restaurant_id:
        raise ValueError(f"Keine RestaurantId im Menü-Manifest von {slug}")
    items_file = manifest.get("ItemsUrl") or guessed_items
    try:
        items_resp = items_future.result()
    except requests.RequestException:
        if items_file == guessed_items:
            raise
        items_resp = None
    if items_file != guessed_items or items_resp is None:
        items_resp = _get(f"{MENU_CDN}/{items_file}")
    if max_bytes and len(items_resp.content) > max_bytes:
        raise ValueError(f"Speisekarte von {slug} ist zu groß für den Index")
    raw_items = {i.get("Id"): i for i in items_resp.json().get("Items") or []}

    # Category of each item: the first specific one ("Pizza") rather than "Beliebt"
    categories, item_category = [], {}
    for cat in _pick_menu(manifest).get("Categories") or []:
        name = (cat.get("Name") or "").strip()
        ids = []
        for raw_id in cat.get("ItemIds") or []:
            item_id = raw_id.split("|")[0]
            if item_id in raw_items and item_id not in ids:
                ids.append(item_id)
                generic = any(g in name.lower() for g in _GENERIC_CATEGORIES)
                if item_id not in item_category or (not generic and item_category[item_id][1]):
                    item_category[item_id] = (name, generic)
        if ids:
            categories.append({"name": name, "ids": ids})

    items, variation_to_item = {}, {}
    for item_id in item_category:
        raw = raw_items[item_id]
        variations = [v for v in raw.get("Variations") or [] if not v.get("DealOnly")]
        prices = [v.get("BasePrice") for v in variations if isinstance(v.get("BasePrice"), (int, float))]
        if not prices:
            continue
        image = (raw.get("ImageSources") or [{}])[0].get("Path")
        item = food.make_item(
            item_id, raw.get("Name"), raw.get("Description"), round(min(prices) * 100), None,
            _image(image), item_category[item_id][0], raw.get("Labels") or [], restaurant_name,
        )
        item["price_from"] = len(set(prices)) > 1
        items[item_id] = item
        for v in raw.get("Variations") or []:
            variation_to_item[v.get("Id")] = item_id

    for cat in categories:
        cat["ids"] = [i for i in cat["ids"] if i in items]
    info = manifest.get("RestaurantInfo") or {}
    menu = {
        "slug": slug,
        "rid": str(restaurant_id),
        "banner": _image(info.get("BannerUrl"), "f_auto,q_auto,c_fill,w_960,h_280"),
        "description": (info.get("Description") or "").strip()[:400],
        "categories": [c for c in categories if c["ids"]],
        "items": items,
        "offers": [],
        "offers_ts": 0.0,
        "_variations": variation_to_item,
    }

    with _menu_lock:
        if len(_menu_cache) >= MENU_CACHE_MAX:
            for key, _ in sorted(_menu_cache.items(), key=lambda kv: kv[1][0])[: MENU_CACHE_MAX // 5]:
                _menu_cache.pop(key, None)
        _menu_cache[slug] = (time.time(), menu)
    return menu


# --- Offers ---

def _fetch_offers(restaurant_ids: list[str]) -> dict[str, list[dict]]:
    """Offer notifications for several restaurants in one request (repeated restaurantIds parameter)."""
    params = [("restaurantIds", rid) for rid in restaurant_ids] + [("optionalProperties", "offerMenuItems")]
    response = _session.get(OFFERS_API, params=params, timeout=20)
    response.raise_for_status()
    by_restaurant = {rid: [] for rid in restaurant_ids}
    for notification in response.json().get("offerNotifications") or []:
        by_restaurant.setdefault(str(notification.get("restaurantId")), []).append(notification)
    return by_restaurant


def _apply_offers(menu: dict, notifications: list[dict]):
    """Put the current offers on the menu's dishes (replacing earlier ones)."""
    items, variations = menu["items"], menu["_variations"]
    for item in items.values():
        item["deal"] = item["_deal0"]
    offers = []
    for notification in notifications:
        deal = _deal(notification)
        offers.append(deal)
        for offer_item in notification.get("offerMenuItems") or []:
            item = items.get(variations.get(offer_item.get("id"), offer_item.get("id")))
            if not item:
                continue
            pct = (offer_item.get("discount") or {}).get("discountPercentage")
            current = item["deal"]
            if current is None or DEAL_RANK.get(deal["type"], 9) < DEAL_RANK.get(current["type"], 9):
                item["deal"] = {"type": deal["type"], "text": deal["text"], "pct": pct}
    menu["offers"] = offers
    menu["offers_ts"] = time.time()


def refresh_offers(menus: list[dict], with_deals: Optional[set] = None):
    """
    Bring the offers of several menus up to date with a few batched requests.
    with_deals: restaurant ids that have deals at all; the others get no request.
    """
    now = time.time()
    stale = [m for m in menus if now - m["offers_ts"] > OFFERS_TTL]
    if with_deals is not None:
        for m in stale:
            if m["rid"] not in with_deals:
                _apply_offers(m, [])
        stale = [m for m in stale if m["rid"] in with_deals]
    batches = [stale[i:i + _OFFERS_BATCH] for i in range(0, len(stale), _OFFERS_BATCH)]

    def run(batch: list[dict]):
        try:
            offers = _fetch_offers([m["rid"] for m in batch])
        except requests.RequestException:
            return
        for m in batch:
            _apply_offers(m, offers.get(m["rid"], []))

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(run, batches))


def menu_with_offers(slug: str, restaurant_name: str = "") -> dict:
    """Menu for display: cached menu plus offers that are at most OFFERS_TTL old."""
    menu = fetch_menu(slug, restaurant_name)
    if time.time() - menu["offers_ts"] > OFFERS_TTL:
        _apply_offers(menu, _fetch_offers([menu["rid"]]).get(menu["rid"], []))
    return menu
