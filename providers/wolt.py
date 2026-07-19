import json
import requests
import threading
import time
from uuid import uuid4
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock


# Module-level dict to store scans
_scans = {}
_scans_lock = Lock()

# In-memory cache for deal items: {slug: (timestamp, offers)}
_deal_items_cache = {}
# Raw dynamic responses cached during scans: {slug: (timestamp, data)}
_dynamic_cache = {}
_CACHE_TTL = 600


def _get_json_with_retry(url: str, tries: int = 3) -> dict:
    """GET JSON, retrying with backoff on 429 rate limits."""
    for attempt in range(tries):
        response = requests.get(url, timeout=10, allow_redirects=True)
        if response.status_code == 429 and attempt < tries - 1:
            retry_after = response.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else 2 * (attempt + 1))
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError("unreachable")


def _get_dynamic(slug: str) -> dict:
    """Fetch a venue's dynamic data, reusing responses cached during scans."""
    cached = _dynamic_cache.get(slug)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]
    url = f"https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/{slug}/dynamic?language=de"
    data = _get_json_with_retry(url)
    _dynamic_cache[slug] = (time.time(), data)
    return data


def _round_rating(score: float) -> float:
    """Convert Wolt rating (0-10) to 5-star scale and round to 1 decimal place."""
    if score is None:
        return None
    rating_5 = score / 2
    return round(rating_5, 1)


def _parse_venue_list(data: dict, limit: int) -> list[dict]:
    """Extract venues from Wolt venue list response."""
    venues = []

    sections = data.get("sections", [])
    for section in sections:
        if section.get("name") == "restaurants-delivering-venues":
            items = section.get("items", [])
            for item in items:
                venue_data = item.get("venue", {})
                sorting = item.get("sorting", {})

                # Get distance for sorting
                sortables = sorting.get("sortables", [])
                distance = float("inf")
                for sortable in sortables:
                    if sortable.get("id") == "distance":
                        distance = sortable.get("value", float("inf"))
                        break

                venues.append({
                    "data": venue_data,
                    "distance": distance
                })

    # Sort by distance and take first 'limit'
    venues.sort(key=lambda x: x["distance"])
    return venues[:limit]


def _get_banner_texts(data: dict) -> dict:
    """Extract banner texts keyed by discount_id from dynamic response."""
    banner_texts = {}
    for banner in data.get("venue", {}).get("banners", []):
        bd = banner.get("discount") or {}
        if bd.get("discount_id") and bd.get("formatted_text"):
            banner_texts[bd["discount_id"]] = bd["formatted_text"]
    return banner_texts


def _parse_dynamic_response(data: dict, venue_data: dict, city_slug: str) -> Optional[dict]:
    """Parse venue dynamic response and extract deals."""
    if not data:
        return None

    venue_raw = data.get("venue_raw", {})
    deals = []

    # Banner texts are the human-readable descriptions, keyed by discount id
    banner_texts = _get_banner_texts(data)

    for discount in venue_raw.get("discounts", []):
        effects = discount.get("effects", {})
        deal_text = banner_texts.get(discount.get("id", ""), "")

        item_discount = effects.get("item_discount")
        free_items = effects.get("free_items")

        if free_items or (item_discount and item_discount.get("fraction") == 1.0):
            deals.append({
                "type": "bogof",
                "text": deal_text or "2 für 1 / Gratis-Artikel"
            })
            continue

        if item_discount:
            if not deal_text:
                fraction = item_discount.get("fraction")
                if fraction:
                    deal_text = f"{int(fraction * 100)} % auf ausgewählte Gerichte"
                elif item_discount.get("amount_per_item"):
                    deal_text = f"{item_discount['amount_per_item'] / 100:.2f} € Rabatt pro Gericht"
                else:
                    deal_text = "Rabatt auf ausgewählte Gerichte"
            deals.append({
                "type": "item_discount",
                "text": deal_text
            })
            continue

        basket_discount = effects.get("basket_discount")
        if basket_discount:
            if not deal_text:
                if basket_discount.get("amount"):
                    deal_text = f"{basket_discount['amount'] / 100:.0f} € Rabatt"
                elif basket_discount.get("fraction"):
                    deal_text = f"{int(basket_discount['fraction'] * 100)} % auf den Warenkorb"
                else:
                    deal_text = "Warenkorb-Rabatt"
            deals.append({
                "type": "basket_discount",
                "text": deal_text
            })
            continue

        delivery_discount = effects.get("delivery_discount")
        if delivery_discount:
            deals.append({
                "type": "free_delivery",
                "text": deal_text or "Gratis Lieferung"
            })
            continue

    if not deals:
        return None

    # Extract rating
    rating_data = venue_data.get("rating", {})
    rating_score = rating_data.get("score") if rating_data else None
    rating = _round_rating(rating_score)
    rating_count = rating_data.get("volume", 0) if rating_data else 0

    # Extract ETA
    estimate_range = venue_data.get("estimate_range", "")

    # Extract city slug (or use provided fallback)
    city_slug_venue = venue_data.get("city", "")
    if not city_slug_venue:
        city_slug_venue = city_slug

    # Build link
    slug = venue_data.get("slug", "")
    link = f"https://wolt.com/de/deu/{city_slug_venue}/restaurant/{slug}" if slug else ""

    return {
        "source": "wolt",
        "name": venue_data.get("name", ""),
        "link": link,
        "slug": slug,
        "rating": rating,
        "rating_count": rating_count,
        "eta": estimate_range,
        "delivery_fee_cents": 0,  # Wolt doesn't provide this in the dynamic response
        "min_order_cents": 0,      # Wolt doesn't provide this in the dynamic response
        "open_now": venue_data.get("online", False),
        "deals": deals
    }


def _fetch_venue_dynamic(slug: str, city_slug: str, venue_data: dict) -> Optional[dict]:
    """Fetch and parse dynamic data for a single venue."""
    try:
        data = _get_dynamic(slug)
        return _parse_dynamic_response(data, venue_data, city_slug)
    except Exception:
        # Silently skip this venue if there's any error
        return None


def _scan_venues_background(scan_id: str, lat: float, lon: float, limit: int, city_slug: str):
    """Background task to scan venues and fetch their dynamic data."""
    try:
        # Fetch venue list
        list_url = f"https://restaurant-api.wolt.com/v1/pages/restaurants?lat={lat}&lon={lon}"
        list_response = requests.get(list_url, timeout=10)
        list_response.raise_for_status()
        list_data = list_response.json()

        # Actual city slug for venue links comes from the list response
        city_slug = list_data.get("city") or city_slug

        # Parse venue list
        venues = _parse_venue_list(list_data, limit)
        total = len(venues)

        # Update scan with total
        with _scans_lock:
            if scan_id in _scans:
                _scans[scan_id]["total"] = total

        # Use ThreadPoolExecutor to fetch dynamic data for all venues
        processed = 0
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(
                    _fetch_venue_dynamic,
                    venue_info["data"].get("slug", ""),
                    city_slug,
                    venue_info["data"],
                )
                for venue_info in venues
            ]

            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception:
                    result = None
                processed += 1
                with _scans_lock:
                    if scan_id in _scans:
                        _scans[scan_id]["scanned"] = processed
                        if result:
                            _scans[scan_id]["restaurants"].append(result)

        # Mark scan as done
        with _scans_lock:
            if scan_id in _scans:
                _scans[scan_id]["done"] = True

    except Exception:
        # Mark scan as done even if there's an error
        with _scans_lock:
            if scan_id in _scans:
                _scans[scan_id]["done"] = True


def start_scan(lat: float, lon: float, limit: int, city_slug: str = "berlin") -> str:
    """
    Start a non-blocking scan of Wolt venues in the given area.

    Args:
        lat: Latitude
        lon: Longitude
        limit: Maximum number of venues to scan
        city_slug: City slug for link generation (default: "berlin")

    Returns:
        Scan ID (UUID hex)
    """
    scan_id = uuid4().hex

    # Initialize scan data
    with _scans_lock:
        _scans[scan_id] = {
            "done": False,
            "scanned": 0,
            "total": 0,
            "restaurants": []
        }

    # Start background thread
    thread = threading.Thread(
        target=_scan_venues_background,
        args=(scan_id, lat, lon, limit, city_slug),
        daemon=True
    )
    thread.start()

    return scan_id


def get_scan(scan_id: str) -> Optional[dict]:
    """
    Get the current status and results of a scan.

    Args:
        scan_id: Scan ID from start_scan()

    Returns:
        Dict with keys: done, scanned, total, restaurants
        Returns None if scan_id not found.
    """
    with _scans_lock:
        if scan_id not in _scans:
            return None

        # Return a copy to avoid race conditions
        scan_data = _scans[scan_id]
        return {
            "done": scan_data["done"],
            "scanned": scan_data["scanned"],
            "total": scan_data["total"],
            "restaurants": list(scan_data["restaurants"])  # Make a copy of the list
        }


def fetch_deal_items(slug: str) -> list[dict]:
    """
    Fetch deal items for a specific Wolt restaurant.

    Args:
        slug: Restaurant slug

    Returns:
        List of offer dicts with items. Raises Exception on error (app.py catches).
    """
    # Check cache
    if slug in _deal_items_cache:
        timestamp, cached_offers = _deal_items_cache[slug]
        if time.time() - timestamp < _CACHE_TTL:
            return cached_offers

    # 1. Dynamic data (discounts + banner texts), reused from scan cache if fresh
    dynamic_data = _get_dynamic(slug)

    # 2. Assortment data (item names and prices)
    assortment_url = f"https://consumer-api.wolt.com/consumer-api/consumer-assortment/v1/venues/slug/{slug}/assortment?language=de"
    assortment_data = _get_json_with_retry(assortment_url)

    # Build item lookup dict: {item_id: {name, price, original_price}}
    items_dict = {}
    for item in assortment_data.get("items", []):
        item_id = item.get("id")
        if item_id:
            items_dict[item_id] = {
                "name": item.get("name", ""),
                "price": item.get("price"),
                "original_price": item.get("original_price")
            }

    # Extract banner texts
    banner_texts = _get_banner_texts(dynamic_data)

    # Extract discounts and build offers
    venue_raw = dynamic_data.get("venue_raw", {})
    offers_list = []

    for discount in venue_raw.get("discounts", []):
        effects = discount.get("effects") or {}
        deal_text = banner_texts.get(discount.get("id", ""), "")

        item_discount = effects.get("item_discount")
        free_items = effects.get("free_items")
        basket_discount = effects.get("basket_discount")
        delivery_discount = effects.get("delivery_discount")

        # Collect item IDs this offer applies to (bogof/item_discount only)
        item_ids_to_include = []
        for effect in (free_items, item_discount):
            include = (effect or {}).get("include") or {}
            item_ids_to_include.extend(include.get("items") or [])

        # Same type precedence as _parse_dynamic_response
        if free_items or (item_discount and item_discount.get("fraction") == 1.0):
            offer_type = "bogof"
            discount_percentage = 100
            deal_text = deal_text or "2 für 1 / Gratis-Artikel"
        elif item_discount:
            offer_type = "item_discount"
            fraction = item_discount.get("fraction")
            discount_percentage = round(fraction * 100) if fraction else None
            if not deal_text:
                if fraction:
                    deal_text = f"{int(fraction * 100)} % auf ausgewählte Gerichte"
                elif item_discount.get("amount_per_item"):
                    deal_text = f"{item_discount['amount_per_item'] / 100:.2f} € Rabatt pro Gericht"
                else:
                    deal_text = "Rabatt auf ausgewählte Gerichte"
        elif basket_discount:
            offer_type = "basket_discount"
            discount_percentage = None
            if not deal_text:
                if basket_discount.get("amount"):
                    deal_text = f"{basket_discount['amount'] / 100:.0f} € Rabatt"
                elif basket_discount.get("fraction"):
                    deal_text = f"{int(basket_discount['fraction'] * 100)} % auf den Warenkorb"
                else:
                    deal_text = "Warenkorb-Rabatt"
        elif delivery_discount:
            offer_type = "free_delivery"
            discount_percentage = None
            deal_text = deal_text or "Gratis Lieferung"
        else:
            continue

        # Build items for this offer (dedupe variants sharing name + price)
        items = []
        seen = set()
        for item_id in item_ids_to_include:
            if item_id in items_dict:
                item_info = items_dict[item_id]
                key = (item_info.get("name", ""), item_info.get("price"))
                if key in seen:
                    continue
                seen.add(key)
                item_dict = {
                    "name": item_info.get("name", ""),
                    "price_cents": item_info.get("price")
                }
                if discount_percentage is not None:
                    item_dict["discount_percentage"] = discount_percentage
                items.append(item_dict)

        offers_list.append({
            "type": offer_type,
            "text": deal_text,
            "items": items
        })

    # Add synthetic offer for directly reduced items (original_price > price)
    reduced_items = []
    for item_id, item_info in items_dict.items():
        price = item_info.get("price")
        original_price = item_info.get("original_price")
        if original_price and price and original_price > price:
            discount_pct = round((1 - price / original_price) * 100)
            reduced_items.append({
                "name": item_info.get("name", ""),
                "price_cents": price,
                "discount_percentage": discount_pct
            })

    if reduced_items:
        offers_list.append({
            "type": "item_discount",
            "text": "Direkt reduzierte Gerichte",
            "items": reduced_items
        })

    # Cache result
    _deal_items_cache[slug] = (time.time(), offers_list)

    return offers_list
