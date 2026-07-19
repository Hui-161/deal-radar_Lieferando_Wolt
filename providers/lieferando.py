import json
import requests
import time
import re
from typing import Optional


OFFER_TYPE_MAPPING = {
    "Bogof": "bogof",
    "BogofMixMatch": "bogof",
    "ItemLevelDiscount": "item_discount",
    "Percent": "basket_discount",
    "DiscountAmount": "basket_discount",
    "FreeItem": "free_item",
    "StampCard": "stampcard",
    "DeliveryFeeDiscount": "free_delivery",
}

# In-memory cache for deal items: {slug: (timestamp, offers)}
_deal_items_cache = {}
_CACHE_TTL = 600


def _parse_lieferando_html(html: str) -> dict:
    """Extract and parse __NEXT_DATA__ JSON from Lieferando HTML."""
    start = html.find('<script id="__NEXT_DATA__" type="application/json">')
    if start == -1:
        raise ValueError("__NEXT_DATA__ script tag not found")

    start += len('<script id="__NEXT_DATA__" type="application/json">')
    end = html.find("</script>", start)
    if end == -1:
        raise ValueError("__NEXT_DATA__ closing script tag not found")

    json_str = html[start:end].strip()
    return json.loads(json_str)


def _normalize_restaurant(rest_data: dict, rest_id: str) -> Optional[dict]:
    """Normalize a single restaurant from Lieferando JSON to standard format."""

    # Skip if no deals
    if not rest_data.get("deals"):
        return None

    # Parse deals
    deals = []
    for deal in rest_data.get("deals", []):
        offer_type = deal.get("offerType", "")
        deal_type = OFFER_TYPE_MAPPING.get(offer_type, "other")
        deal_text = deal.get("description", "")

        # Special case: StampCard without description
        if deal_type == "stampcard" and not deal_text:
            deal_text = "Treuekarte"

        deals.append({
            "type": deal_type,
            "text": deal_text
        })

    if not deals:
        return None

    # Extract rating
    rating_data = rest_data.get("rating", {})
    rating = rating_data.get("starRating") if rating_data else None
    rating_count = rating_data.get("count", 0) if rating_data else 0

    # Extract delivery ETA
    eta_data = rest_data.get("deliveryEtaMinutes", {})
    if eta_data and "rangeLower" in eta_data and "rangeUpper" in eta_data:
        eta = f"{eta_data['rangeLower']}-{eta_data['rangeUpper']}"
    else:
        eta = ""

    # Extract delivery fees and minimum order
    delivery_fees_data = rest_data.get("deliveryFees", {})
    delivery_fee_cents = 0
    if delivery_fees_data:
        by_min_fee = delivery_fees_data.get("byMinFee", {})
        if by_min_fee:
            delivery_fee_cents = by_min_fee.get("fee", 0)

    min_order_cents = 0
    if delivery_fees_data:
        by_min_order = delivery_fees_data.get("byMinOrder", {})
        if by_min_order:
            min_order_cents = by_min_order.get("minimumAmount", 0)

    # Extract open status
    availability = rest_data.get("availability", {})
    delivery_status = availability.get("delivery", {})
    open_now = delivery_status.get("isOpen", False) if delivery_status else False

    # Build link
    unique_name = rest_data.get("uniqueName", "")
    link = f"https://www.lieferando.de/speisekarte/{unique_name}" if unique_name else ""

    return {
        "source": "lieferando",
        "name": rest_data.get("name", ""),
        "link": link,
        "slug": unique_name,
        "rating": rating,
        "rating_count": rating_count,
        "eta": eta,
        "delivery_fee_cents": delivery_fee_cents,
        "min_order_cents": min_order_cents,
        "open_now": open_now,
        "deals": deals
    }


def fetch_deals(city: str, plz: str) -> list[dict]:
    """
    Fetch and normalize restaurant deals from Lieferando for a given city/PLZ.

    Args:
        city: City name (e.g., "berlin")
        plz: Postal code (e.g., "10178")

    Returns:
        List of normalized restaurant dicts with deals only.
    """
    url = f"https://www.lieferando.de/lieferservice/essen/{city}-{plz}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept-Language": "de"
    }

    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()

    data = _parse_lieferando_html(response.text)

    # Navigate to restaurant list
    restaurant_data = data.get("props", {}).get("appProps", {}).get("preloadedState", {}).get("discovery", {}).get("restaurantList", {}).get("restaurantData", {})

    restaurants = []
    for rest_id, rest_data in restaurant_data.items():
        normalized = _normalize_restaurant(rest_data, rest_id)
        if normalized:
            restaurants.append(normalized)

    return restaurants


def fetch_deal_items(slug: str) -> list[dict]:
    """
    Fetch deal items for a specific Lieferando restaurant.

    Args:
        slug: Restaurant slug (uniqueName)

    Returns:
        List of offer dicts with items. Raises Exception on error (app.py catches).
    """
    # Check cache
    if slug in _deal_items_cache:
        timestamp, cached_offers = _deal_items_cache[slug]
        if time.time() - timestamp < _CACHE_TTL:
            return cached_offers

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept-Language": "de"
    }

    # 1. Fetch restaurant page to get items and restaurant ID
    menu_url = f"https://www.lieferando.de/speisekarte/{slug}"
    response = requests.get(menu_url, headers=headers, timeout=20)
    response.raise_for_status()

    data = _parse_lieferando_html(response.text)

    # Extract items from menu
    items_dict = data.get("props", {}).get("appProps", {}).get("preloadedState", {}).get("menu", {}).get("restaurant", {}).get("cdn", {}).get("items", {})

    # Extract restaurant ID from HTML (format: "restaurantId":"123")
    match = re.search(r'"restaurantId":"(\d+)"', response.text)
    if not match:
        raise ValueError(f"Could not find restaurantId in {slug} page")
    restaurant_id = match.group(1)

    # 2. Fetch offers
    offers_url = f"https://rest.api.eu-central-1.production.jet-external.com/consumeroffers/notifications/de?restaurantIds={restaurant_id}&optionalProperties=offerMenuItems"
    offers_response = requests.get(offers_url, timeout=20)
    offers_response.raise_for_status()
    offers_data = offers_response.json()

    offers_list = []

    for offer_notif in offers_data.get("offerNotifications", []):
        offer_type = offer_notif.get("offerType", "")
        deal_type = OFFER_TYPE_MAPPING.get(offer_type, "other")
        offer_text = offer_notif.get("description", "")

        # Special case: StampCard without description
        if deal_type == "stampcard" and not offer_text:
            offer_text = "Treuekarte"

        # Build items list by joining with menu items
        items = []
        for offer_item in offer_notif.get("offerMenuItems", []):
            item_id = offer_item.get("id")
            if item_id in items_dict:
                menu_item = items_dict[item_id]
                item_name = menu_item.get("name", "")

                # Extract price from variations
                price_cents = None
                variations = menu_item.get("variations", [])
                if variations:
                    base_price = variations[0].get("basePrice")
                    if base_price is not None:
                        price_cents = round(base_price * 100)

                # Extract discount percentage
                discount_info = offer_item.get("discount", {})
                discount_percentage = discount_info.get("discountPercentage")

                items.append({
                    "name": item_name,
                    "price_cents": price_cents,
                    "discount_percentage": discount_percentage
                })

        offers_list.append({
            "type": deal_type,
            "text": offer_text,
            "items": items
        })

    # Cache result
    _deal_items_cache[slug] = (time.time(), offers_list)

    return offers_list
