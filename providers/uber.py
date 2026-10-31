"""Uber Eats: restaurant list with deals (paged feed) and menus on demand.

Unofficial web API of ubereats.com. Used politely: honest user agent, at least two seconds between
requests, no search (robots.txt disallows search pages), and a long pause as soon as Uber answers
with 403/429 or something that is not JSON (bot protection) - never an attempt to get around it.
"""
import json
import re
import threading
import time
import urllib.parse
from typing import Callable, Optional

import requests

from . import food

API = "https://www.ubereats.com/_p/api"
HEADERS = {
    "User-Agent": "deal-radar/1.0 (privat)",
    "Accept": "application/json",
    "Accept-Language": "de-DE",
    "Content-Type": "application/json",
    "x-csrf-token": "x",  # fixed value the Uber Eats website itself sends
}
MAX_PAGES = 5  # first page ~100 restaurants, every further page 80
_PAGE_SIZE = 80
_GAP = 2.0
_BLOCK_PAUSE = 900
_CACHE_TTL = 600
_MENU_TTL = 1800

_session = requests.Session()
_session.headers.update(HEADERS)
_gate = threading.Lock()
_next_at = 0.0
blocked_until = 0.0
_list_cache = {}  # (lat, lon) rounded -> (timestamp, restaurants)
_menu_cache = {}  # store uuid -> (timestamp, menu)
_GENERIC_CATEGORIES = ("beliebt", "popular", "angebot", "highlight", "empfehl", "bestseller", "top ", "neu", "featured")
DEAL_RANK = {"bogof": 0, "free_item": 1, "item_discount": 2}


class UberBlocked(RuntimeError):
    """Uber answered with a block or rate limit; we stop until the pause is over."""


def blocked() -> bool:
    return time.time() < blocked_until


def _post(path: str, body: dict, lat: float, lon: float, plz: str = "") -> dict:
    global _next_at, blocked_until
    with _gate:
        now = time.time()
        if now < blocked_until:
            raise UberBlocked("Uber Eats lässt gerade keine Abrufe zu – bitte später erneut versuchen")
        wait = max(0.0, _next_at - now)
        _next_at = max(now, _next_at) + _GAP
    if wait:
        time.sleep(wait)
    # The delivery address travels as a cookie, exactly like on the website
    location = {
        "address": {"address1": "Lieferadresse", "address2": plz, "aptOrSuite": "", "eaterFormattedAddress": plz,
                    "subtitle": plz, "title": "Lieferadresse", "uuid": ""},
        "latitude": lat, "longitude": lon, "reference": "", "referenceType": "", "type": "",
        "addressComponents": {"countryCode": "DE", "postalCode": plz},
    }
    response = _session.post(f"{API}/{path}?localeCode=de", json=body, timeout=30,
                             cookies={"uev2.loc": urllib.parse.quote(json.dumps(location))})
    if response.status_code in (403, 429) or "json" not in (response.headers.get("Content-Type") or ""):
        blocked_until = time.time() + _BLOCK_PAUSE
        raise UberBlocked("Uber Eats lässt gerade keine Abrufe zu – bitte später erneut versuchen")
    response.raise_for_status()
    return response.json().get("data") or {}


def _feed_body(offset: Optional[int], cache_key: str) -> dict:
    body = {
        "cacheKey": cache_key, "feedSessionCount": {"announcementCount": 0, "announcementLabel": ""}, "userQuery": "",
        "date": "", "startTime": 0, "endTime": 0, "carouselId": "", "sortAndFilters": [], "billboardUuid": "",
        "feedProvider": "", "promotionUuid": "", "targetingStoreTag": "", "venueUUID": "", "selectedSectionUUID": "",
        "favorites": "", "vertical": "", "searchSource": "", "searchType": "", "keyName": "",
        "serializedRequestContext": "", "isUserInitiatedRefresh": False,
    }
    if offset is not None:
        body["pageInfo"] = {"offset": offset, "pageSize": _PAGE_SIZE}
    return body


def _pick_image(images: list, width: int) -> Optional[str]:
    if not images:
        return None
    fitting = [i for i in images if i.get("width") and i["width"] <= width] or images
    return max(fitting, key=lambda i: i.get("width") or 0).get("url")


def _count(text) -> int:
    digits = re.sub(r"\D", "", str(text or ""))
    return int(digits) if digits else 0


def _normalize_store(store: dict, lat: float, lon: float) -> Optional[dict]:
    uuid = store.get("storeUuid")
    action = store.get("actionUrl") or ""
    payload = ((store.get("tracking") or {}).get("storePayload")) or {}
    state = payload.get("storeAvailablityState") or ""
    if not uuid or state == "TOO_FAR_TO_DELIVER" or "diningMode=PICKUP" in action:
        return None
    name = ((store.get("title") or {}).get("text") or "").strip()
    meta = store.get("meta") or []
    eta_range = (payload.get("etdInfo") or {}).get("dropoffETARange") or {}
    eta = f"{eta_range['min']}-{eta_range['max']}" if eta_range.get("min") and eta_range.get("max") else ""
    fee_text = next((m.get("text") for m in meta if m.get("badgeType") == "FARE"), "")
    rating = (payload.get("ratingInfo") or {}).get("storeRatingScore")
    marker = store.get("mapMarker") or {}
    deals = []
    for sp in store.get("signposts") or []:
        text = (sp.get("text") or "").strip()
        if text:
            deals.append({"type": food.deal_type_from_text(text), "text": text})
    return {
        "id": f"u:{uuid}",
        "source": "uber",
        "slug": uuid,
        "rid": uuid,
        "name": name,
        "link": f"https://www.ubereats.com{action.split('?')[0]}" if action.startswith("/") else "",
        "logo": None,
        "img": _pick_image((store.get("image") or {}).get("items") or [], 640),
        "cuisines": food.guess_cuisines(name),  # the feed has no cuisine per restaurant
        "tags": [],
        "diet": [],
        "halal": False,
        "shop": food.looks_like_shop(name),
        "rating": round(rating, 1) if isinstance(rating, (int, float)) else None,
        "rating_count": _count((payload.get("ratingInfo") or {}).get("ratingCount")),
        "eta": eta,
        "eta_min": eta_range.get("min"),
        "delivery_fee_cents": food.euro_cents(fee_text) if fee_text else None,
        "min_order_cents": None,
        "open_now": bool(payload.get("isOrderable", True)) and state not in ("NOT_ACCEPTING_ORDERS", "CLOSED", "UNAVAILABLE"),
        "opens_at": None,
        "distance_m": food.distance_m(lat, lon, marker.get("latitude"), marker.get("longitude")),
        "is_new": any(m.get("badgeType") == "NEW_STORE" for m in meta),
        "badges": [m.get("text") for m in meta if m.get("badgeType") == "EXCLUSIVE_STORE" and m.get("text")][:1],
        "deals": list({(d["type"], d["text"]): d for d in deals}.values()),
    }


def fetch_restaurants(lat: float, lon: float, plz: str, on_page: Callable[[list, int], None],
                      keep_going: Callable[[], bool], max_pages: int = MAX_PAGES) -> list[dict]:
    """Restaurant list page by page; on_page(restaurants so far, pages done) after every page."""
    key = (round(lat, 3), round(lon, 3))
    cached = _list_cache.get(key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        on_page(cached[1], max_pages)
        return cached[1]

    restaurants, seen, offset, cache_key = [], set(), None, ""
    for page in range(1, max_pages + 1):
        if not keep_going():
            break
        data = _post("getFeedV1", _feed_body(offset, cache_key), lat, lon, plz)
        cache_key = data.get("cacheKey") or cache_key
        for entry in data.get("feedItems") or []:
            if entry.get("type") != "REGULAR_STORE":
                continue
            rest = _normalize_store(entry.get("store") or {}, lat, lon)
            if rest and rest["slug"] not in seen:
                seen.add(rest["slug"])
                restaurants.append(rest)
        on_page(list(restaurants), page)
        meta = data.get("meta") or {}
        if not meta.get("hasMore") or meta.get("offset") is None:
            break
        offset = meta["offset"]
    if len(_list_cache) > 50:
        _list_cache.clear()
    _list_cache[key] = (time.time(), restaurants)
    return restaurants


def _label_texts(label: Optional[dict]) -> list[str]:
    return [e["text"]["text"]["text"] for e in (label or {}).get("richTextElements") or []
            if e.get("type") == "text" and isinstance(((e.get("text") or {}).get("text") or {}).get("text"), str)]


def fetch_menu(uuid: str, lat: float, lon: float, plz: str, restaurant_name: str = "", offers=None) -> dict:
    """Full menu of a store incl. 2-for-1 and reduced prices per dish. Raises on error (callers catch)."""
    cached = _menu_cache.get(uuid)
    if cached and time.time() - cached[0] < _MENU_TTL:
        return cached[1]
    data = _post("getStoreV1", {"storeUuid": uuid, "diningMode": "DELIVERY", "time": {"asap": True},
                                "cbType": "EATER_ENDORSED"}, lat, lon, plz)
    csm = data.get("catalogSectionsMap") or {}
    order = [s.get("uuid") for s in data.get("sections") or [] if s.get("uuid") in csm] or list(csm)

    # First collect every dish with its most specific category ("Burger" rather than "Beliebt")
    categories, raw_items, item_category = [], {}, {}
    for section_id in order:
        for block in csm.get(section_id) or []:
            payload = (block.get("payload") or {}).get("standardItemsPayload") or {}
            cat_name = ((payload.get("title") or {}).get("text") or "").strip()
            generic = any(g in cat_name.lower() for g in _GENERIC_CATEGORIES)
            ids = []
            for raw in payload.get("catalogItems") or []:
                item_id = raw.get("uuid")
                if not item_id or not isinstance(raw.get("price"), int):
                    continue
                ids.append(item_id)
                raw_items.setdefault(item_id, raw)
                if item_id not in item_category or (not generic and item_category[item_id][1]):
                    item_category[item_id] = (cat_name, generic)
            if ids:
                categories.append({"name": cat_name, "ids": list(dict.fromkeys(ids))})

    items = {}
    for item_id, raw in raw_items.items():
        endorsement = ((raw.get("endorsement") or {}).get("text") or "").strip(" .")
        labels = [endorsement] if re.search(r"vegan|vegetar", endorsement, re.I) else []
        if raw.get("numAlcoholicItems"):
            labels.append("alcohol")
        # labelPrimary shows "reduced price | original price" for discounted dishes
        prices = [c for c in (food.euro_cents(t) for t in _label_texts(raw.get("labelPrimary"))[:2]) if c]
        original = prices[1] if len(prices) > 1 and prices[1] > raw["price"] else None
        item = food.make_item(
            item_id, raw.get("title"), raw.get("itemDescription"), raw["price"], original,
            raw.get("imageUrl"), item_category[item_id][0], labels, restaurant_name,
        )
        bxgy = (raw.get("itemPromotion") or {}).get("buyXGetYItemPromotion") or {}
        badge = " ".join(_label_texts((((raw.get("promoInfo") or {}).get("promoBadge") or {}).get("content") or {}).get("richText")))
        if bxgy.get("buyQuantity") == 1 and bxgy.get("getQuantity") == 1:
            item["deal"] = {"type": "bogof", "text": badge or "Kaufe 1, erhalte 1 gratis", "pct": 100}
        elif original:
            item["deal"] = {"type": "item_discount", "text": "Reduziert", "pct": round((1 - raw["price"] / original) * 100)}
        if raw.get("isSoldOut") or raw.get("isAvailable") is False:
            item["sold_out"] = True
        items[item_id] = item

    cuisine_list = [c for c in data.get("cuisineList") or [] if isinstance(c, str)]
    menu = {
        "slug": uuid,
        "banner": _pick_image(data.get("heroImageUrls") or [], 1100),
        "description": "",
        "categories": categories,
        "items": items,
        "offers": list(offers or []),
        "min_order_cents": None,
        "cuisines": food.guess_cuisines(" ".join(cuisine_list + [restaurant_name])),
        "tags": cuisine_list[:4],
    }
    if len(_menu_cache) > 200:
        _menu_cache.clear()
    _menu_cache[uuid] = (time.time(), menu)
    return menu
