"""Per-address state: restaurant lists of all providers, the Lieferando menu index,
the Wolt deal scan, the Uber Eats list and the dish search across everything that is loaded."""
import logging
import os
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from uuid import uuid4

from providers import food, lieferando, uber, wolt

log = logging.getLogger("deal-radar")

MENU_LIMIT = int(os.environ.get("DEAL_RADAR_MENU_LIMIT", "300"))
WOLT_SCAN_LIMIT = int(os.environ.get("DEAL_RADAR_WOLT_SCAN_LIMIT", "100"))
_INDEX_WORKERS = 12
_MAX_ITEM_BYTES = 4_000_000  # grocery shops have menus of several MB
_AREA_TTL = 1800  # opening hours and deals change; rebuild after 30 minutes
_MAX_AREAS = 4
PER_RESTAURANT = 3  # dishes per restaurant in the result list before "+N more"
_PRIORITY_MAX = 40  # Wolt venues the page may move to the front of the deal check
# Nothing runs in the background without a visitor: the deal check stops once the page has not
# asked for anything for this long (it polls every few seconds while the check runs), and goes on
# when the page is back. Several checks may run at once; Wolt's throttle paces all of them together.
_IDLE_STOP = 90

_VEGAN_WORD = re.compile(r"vegan(e|er|es|en|em)?")
_VEGGIE_WORD = re.compile(r"vegetari(an|sch|sche|scher|sches|schen|schem)|veggie|fleischlos")
DEAL_ORDER = {"bogof": 0, "free_item": 1, "item_discount": 2}
_SIDE = re.compile(r"beilage|\bsides?\b|snack|vorspeise|starter|fingerfood|pommes|fries|frites|nugget|nachspeise|dessert"
                   r"|kinder|\bbites\b")

_areas = {}  # id -> Area
_lock = threading.Lock()
_wolt_search_slot = threading.Semaphore(1)


class Area:
    def __init__(self, plz: str, lat: float, lon: float):
        self.id = uuid4().hex[:16]
        self.key = (plz, round(lat, 3), round(lon, 3))
        self.plz, self.lat, self.lon = plz, lat, lon
        self.created = self.used = time.time()
        self.alive = True
        self.lock = threading.Lock()
        self.lieferando = {"status": "loading", "error": None, "restaurants": []}
        self.wolt = {"status": "loading", "error": None, "restaurants": [], "city": "berlin"}
        self.uber = {"status": "loading", "error": None, "restaurants": [], "pages": 0, "pages_max": uber.MAX_PAGES}
        self.index = {"status": "waiting", "total": 0, "done": 0, "failed": 0}
        self.scan = {"status": "waiting", "total": 0, "done": 0, "failed": 0}
        self.scan_queue = deque()  # Wolt venues still to check for deals, front = next
        self.scan_running = False
        self.rest = {}  # "l:slug" / "w:slug" / "u:uuid" -> restaurant
        self.l_menus = {}  # slug -> Lieferando menu
        self.w_menus = {}  # slug -> Wolt menu (opened by the user)
        self.u_menus = {}  # uuid -> Uber Eats menu (opened by the user)
        self.w_deals = {}  # slug -> parsed Wolt deals
        self.w_preview = {}  # slug -> preview items from the Wolt list
        self.w_searches = {}  # folded query -> search entry

    def count(self, job: dict, failed: bool = False):
        with self.lock:
            job["done"] += 1
            if failed:
                job["failed"] += 1


# --- Lifecycle ---

def _start(area: Area):
    threading.Thread(target=_run_lieferando, args=(area,), daemon=True).start()
    threading.Thread(target=_run_wolt, args=(area,), daemon=True).start()
    threading.Thread(target=_run_uber, args=(area,), daemon=True).start()


def get_or_create(plz: str, lat: float, lon: float) -> Area:
    key = (plz, round(lat, 3), round(lon, 3))
    now = time.time()
    with _lock:
        for area in _areas.values():
            if area.key == key and now - area.created < _AREA_TTL:
                area.used = now
                return area
        area = Area(plz, lat, lon)
        _areas[area.id] = area
        for old in sorted(_areas.values(), key=lambda a: a.used)[: max(0, len(_areas) - _MAX_AREAS)]:
            old.alive = False
            del _areas[old.id]
    _start(area)
    return area


def get(area_id: str) -> Optional[Area]:
    area = _areas.get(area_id)
    if area:
        area.used = time.time()
    return area


def _by_distance(restaurants: list[dict]) -> list[dict]:
    return sorted(restaurants, key=lambda r: r["distance_m"] if r["distance_m"] is not None else 1e9)


def _run_lieferando(area: Area):
    try:
        restaurants = lieferando.fetch_restaurants(area.plz, area.lat, area.lon)
    except Exception as e:
        area.lieferando.update(status="error", error=str(e))
        area.index["status"] = "skipped"
        return
    for r in restaurants:
        area.rest[r["id"]] = r
    area.lieferando.update(status="ok", restaurants=restaurants)

    targets = _by_distance([r for r in restaurants if r["open_now"] and not r["shop"]])[:MENU_LIMIT]
    area.index.update(status="running", total=len(targets))

    def index_one(r: dict):
        if not area.alive:
            return
        try:
            area.l_menus[r["slug"]] = lieferando.fetch_menu(r["slug"], r["name"], max_bytes=_MAX_ITEM_BYTES)
            area.count(area.index)
        except Exception as e:
            log.warning("Speisekarte %s nicht geladen: %s", r["slug"], str(e)[:160])
            area.count(area.index, failed=True)

    with ThreadPoolExecutor(max_workers=_INDEX_WORKERS) as executor:
        list(executor.map(index_one, targets))
    # Current offers for all indexed menus: a few batched requests instead of one per restaurant
    menus = [area.l_menus[r["slug"]] for r in targets if r["slug"] in area.l_menus]
    lieferando.refresh_offers(menus, with_deals={r["rid"] for r in targets if r["deals"]})
    area.index["status"] = "done"


def _run_wolt(area: Area):
    try:
        restaurants, city, previews = wolt.fetch_restaurants(area.lat, area.lon)
    except Exception as e:
        area.wolt.update(status="error", error=str(e))
        area.scan["status"] = "skipped"
        return
    for r in restaurants:
        area.rest[r["id"]] = r
    area.w_preview = previews
    area.wolt.update(status="ok", restaurants=restaurants, city=city)

    targets = _by_distance([r for r in restaurants if r["open_now"] and not r["shop"]])[:WOLT_SCAN_LIMIT]
    with area.lock:
        # The page may already have asked for venues it shows; those stay in front
        queued = set(area.scan_queue)
        area.scan_queue.extend(r["slug"] for r in targets if r["slug"] not in queued)
        area.scan["total"] = len(area.scan_queue)
    _scan(area)


def _scan(area: Area):
    """Check Wolt venues for deals: cached ones at once, the rest one by one at Wolt's pace."""
    with area.lock:
        if area.scan_running:
            return
        area.scan_running = True
        area.scan["status"] = "running"
    try:
        with area.lock:
            pending = list(area.scan_queue)
        for slug in pending:
            cached = wolt.cached_deals(slug)
            if cached is None:
                continue
            with area.lock:
                if slug not in area.scan_queue:
                    continue
                area.scan_queue.remove(slug)
            area.w_deals[slug] = cached
            area.count(area.scan)

        while area.alive and time.time() - area.used < _IDLE_STOP:
            with area.lock:
                if not area.scan_queue:
                    break
                slug = area.scan_queue.popleft()
            if slug in area.w_deals:
                area.count(area.scan)
                continue
            try:
                area.w_deals[slug] = wolt.get_deals(slug, interactive=False)
                area.count(area.scan)
            except Exception:
                area.count(area.scan, failed=True)
    finally:
        with area.lock:
            area.scan_running = False
            if area.scan_queue and (not area.alive or time.time() - area.used >= _IDLE_STOP):
                area.scan["status"] = "stopped"
            else:
                area.scan["status"] = "done"


def prioritize(area: Area, slugs: list[str]) -> int:
    """Move Wolt venues the page is showing to the front of the deal check; returns how many."""
    moved = 0
    with area.lock:
        for slug in reversed(slugs[:_PRIORITY_MAX]):
            if slug in area.w_deals or f"w:{slug}" not in area.rest:
                continue
            if slug in area.scan_queue:
                area.scan_queue.remove(slug)
            else:
                area.scan["total"] += 1
            area.scan_queue.appendleft(slug)
            moved += 1
        restart = moved and not area.scan_running and area.alive \
            and area.wolt["status"] == "ok"
    if restart:
        threading.Thread(target=_scan, args=(area,), daemon=True).start()
    return moved


def _run_uber(area: Area):
    """Uber Eats list page by page; the deals come with the list, so there is no separate check."""
    def on_page(restaurants: list, pages: int):
        for r in restaurants:
            area.rest.setdefault(r["id"], r)
        area.uber.update(restaurants=restaurants, pages=pages)

    try:
        uber.fetch_restaurants(area.lat, area.lon, area.plz, on_page, lambda: area.alive)
        area.uber["status"] = "ok"
    except uber.UberBlocked as e:
        area.uber.update(status="blocked", error=str(e))
    except Exception as e:
        log.warning("Uber-Eats-Liste nicht geladen: %s", str(e)[:160])
        area.uber.update(status="error", error=str(e))


# --- Status ---

def _diet_counts(menu: dict) -> list[int]:
    """[explicitly vegan dishes, vegetarian dishes incl. likely ones], memoized per menu."""
    counts = menu.get("_diet_counts")
    if counts is None:
        food_items = [i for i in menu["items"].values() if i["kind"] == "food"]
        counts = [
            sum(1 for i in food_items if i["diet"] == "vegan" and i["diet_src"] != "guess"),
            sum(1 for i in food_items if i["diet"]),
        ]
        menu["_diet_counts"] = counts
    return counts


def status(area: Area) -> dict:
    # A deal check that paused because nobody was looking continues when the page is back.
    # Marked as running right away, so the page keeps polling.
    with area.lock:
        resume = area.scan["status"] == "stopped" and area.scan_queue and area.alive and not area.scan_running
        if resume:
            area.scan["status"] = "running"
    if resume:
        threading.Thread(target=_scan, args=(area,), daemon=True).start()
    diet_counts = {f"l:{slug}": _diet_counts(m) for slug, m in list(area.l_menus.items())}
    diet_counts.update({f"w:{slug}": _diet_counts(m) for slug, m in list(area.w_menus.items())})
    diet_counts.update({f"u:{slug}": _diet_counts(m) for slug, m in list(area.u_menus.items())})
    return {
        "lieferando": {"status": area.lieferando["status"], "error": area.lieferando["error"],
                       "count": len(area.lieferando["restaurants"])},
        "wolt": {"status": area.wolt["status"], "error": area.wolt["error"], "count": len(area.wolt["restaurants"])},
        "uber": {"status": area.uber["status"], "error": area.uber["error"], "count": len(area.uber["restaurants"]),
                 "pages": area.uber["pages"], "pages_max": area.uber["pages_max"]},
        "index": dict(area.index),
        "scan": {**area.scan, "throttled": area.scan["status"] == "running" and wolt.throttled()},
        "wolt_deals": {
            slug: {"deals": p["deals"], "min_order_cents": p["min_order_cents"]}
            for slug, p in list(area.w_deals.items())
        },
        "diet_counts": diet_counts,
    }


def restaurants(area: Area, source: str) -> list[dict]:
    return {"lieferando": area.lieferando, "wolt": area.wolt, "uber": area.uber}[source]["restaurants"]


# --- Menu of one restaurant ---

def _public_menu(menu: dict) -> dict:
    items = menu["items"]
    return {
        "banner": menu.get("banner"),
        "description": menu.get("description") or "",
        "offers": menu.get("offers") or [],
        "min_order_cents": menu.get("min_order_cents"),
        "cuisines": menu.get("cuisines"),
        "tags": menu.get("tags"),
        "categories": [
            {"name": c["name"], "items": [food.public_item(items[i]) for i in c["ids"]]}
            for c in menu["categories"]
        ],
    }


def menu(area: Optional[Area], source: str, slug: str) -> dict:
    prefix = {"lieferando": "l", "wolt": "w", "uber": "u"}[source]
    rest = area.rest.get(f"{prefix}:{slug}") if area else None
    name = rest["name"] if rest else ""
    if source == "lieferando":
        m = lieferando.menu_with_offers(slug, name)
        if area:
            area.l_menus.setdefault(slug, m)
    elif source == "wolt":
        m = wolt.fetch_menu(slug, name)
        if area:
            area.w_menus[slug] = m
            area.w_deals[slug] = m["dynamic"]
    else:
        if not area:
            raise ValueError("Suche abgelaufen – bitte neu laden")  # Uber needs the delivery address
        m = uber.fetch_menu(slug, area.lat, area.lon, area.plz, name, rest["deals"] if rest else [])
        area.u_menus[slug] = m
        if rest:
            # The feed has no cuisine per restaurant; the menu does
            rest["cuisines"] = sorted(set(rest["cuisines"]) | set(m["cuisines"]))
            rest["tags"] = rest["tags"] or m["tags"]
    return _public_menu(m)


# --- Dish search ---

def _wolt_search(area: Area, query: str) -> dict:
    """Start (or reuse) Wolt's own dish search for a query; it runs in the background."""
    key = food.fold(query).strip()
    with area.lock:
        entry = area.w_searches.get(key)
        if entry and (entry["status"] in ("loading", "done") or time.time() - entry["ts"] < 60):
            return entry
        entry = {"status": "loading", "error": None, "results": [], "ts": time.time()}
        area.w_searches[key] = entry
        if len(area.w_searches) > 40:
            oldest = min(area.w_searches, key=lambda k: area.w_searches[k]["ts"])
            area.w_searches.pop(oldest, None)

    def run():
        with _wolt_search_slot:
            try:
                entry["results"] = wolt.search_items(query, area.lat, area.lon)
                entry["status"] = "done"
            except wolt.WoltBusy as e:
                entry.update(status="throttled", error=str(e))
            except Exception as e:
                entry.update(status="error", error=str(e))
            entry["ts"] = time.time()

    threading.Thread(target=run, daemon=True).start()
    return entry


def _rest_brief(rest: dict) -> dict:
    keys = ("id", "source", "slug", "name", "link", "logo", "rating", "rating_count", "eta", "eta_min", "open_now",
            "distance_m", "delivery_fee_cents", "min_order_cents")
    return {k: rest.get(k) for k in keys}


def _wolt_rest(area: Area, venue: dict) -> dict:
    """Restaurant for a Wolt search hit; venues missing from the list get a minimal entry."""
    rest = area.rest.get(f"w:{venue['slug']}")
    if rest:
        return rest
    return {
        "id": f"w:{venue['slug']}", "source": "wolt", "slug": venue["slug"], "name": venue["name"],
        "link": f"https://wolt.com/de/deu/{venue['city']}/restaurant/{venue['slug']}", "logo": venue["logo"],
        "rating": venue["rating"], "rating_count": 0, "eta": venue["eta"], "eta_min": food.eta_min(venue["eta"]),
        "open_now": True, "distance_m": None, "cuisines": [], "delivery_fee_cents": None, "min_order_cents": None,
    }


def _candidates(area: Area, source: str, wolt_entry: Optional[dict]):
    """(restaurant, item) pairs of everything loaded, Wolt items deduplicated."""
    if source in ("", "lieferando"):
        for slug, m in list(area.l_menus.items()):
            rest = area.rest.get(f"l:{slug}")
            if rest:
                for item in m["items"].values():
                    yield rest, item
    if source in ("", "uber"):
        # Uber Eats dishes only from menus that were opened (no search, no bulk download)
        for slug, m in list(area.u_menus.items()):
            rest = area.rest.get(f"u:{slug}")
            if rest:
                for item in m["items"].values():
                    yield rest, item
    if source not in ("", "wolt"):
        return
    seen = set()
    for slug, m in list(area.w_menus.items()):
        rest = area.rest.get(f"w:{slug}")
        if rest:
            for item in m["items"].values():
                seen.add((slug, item["id"]))
                yield rest, item
    if wolt_entry and wolt_entry["status"] == "done":
        for venue, item in wolt_entry["results"]:
            if (venue["slug"], item["id"]) not in seen:
                seen.add((venue["slug"], item["id"]))
                yield _wolt_rest(area, venue), item
    for slug, items in list(area.w_preview.items()):
        rest = area.rest.get(f"w:{slug}")
        for item in items:
            if rest and (slug, item["id"]) not in seen:
                seen.add((slug, item["id"]))
                yield rest, item


def search_dishes(area: Area, q: str = "", diet: str = "", cuisine: str = "", deals: str = "",
                  max_price: Optional[int] = None, source: str = "", open_only: bool = True,
                  sort: str = "relevance", offset: int = 0, limit: int = 48) -> dict:
    tokens = [t for t in re.split(r"[^\w-]+", food.fold(q)) if t]
    # Diet words in the query act as filters ("vegane pizza" -> diet=vegan, tokens=["pizza"])
    for t in list(tokens):
        if _VEGAN_WORD.fullmatch(t):
            tokens.remove(t)
            diet = "vegan"
        elif _VEGGIE_WORD.fullmatch(t):
            tokens.remove(t)
            diet = diet or "vegetarian"
    patterns = [re.compile(r"\b" + re.escape(t)) for t in tokens]
    rest_names = {}
    cuisine_def = food.CUISINE_BY_KEY.get(cuisine)
    cuisine_term = food.fold(cuisine_def[5]) if cuisine_def and cuisine_def[5] else ""

    # Wolt only returns its top hits, so the query names the diet as well ("vegan pizza")
    diet_word = {"vegan": "vegan", "vegetarian": "vegetarisch"}.get(diet, "")
    words = [w for w in q.split() if not (_VEGAN_WORD.fullmatch(food.fold(w)) or _VEGGIE_WORD.fullmatch(food.fold(w)))]
    base_query = " ".join(words)[:80] or (cuisine_def[5] if cuisine_def and cuisine_def[5] else "")
    wolt_query = f"{diet_word} {base_query}".strip()
    wolt_entry, wolt_state = None, "none"
    if source in ("", "wolt"):
        if area.wolt["status"] == "ok" and wolt_query:
            wolt_entry = _wolt_search(area, wolt_query)
            wolt_state = wolt_entry["status"]
        elif area.wolt["status"] == "loading":
            wolt_state = "waiting"

    results, closed = [], 0
    for rest, item in _candidates(area, source, wolt_entry):
        if item.get("sold_out"):
            continue
        if cuisine and cuisine not in (rest.get("cuisines") or []):
            continue
        if diet == "vegan" and item["diet"] != "vegan":
            continue
        if diet == "vegetarian" and not item["diet"]:
            continue
        price = item["price_cents"]
        if max_price is not None and (price is None or price > max_price):
            continue
        deal = item["deal"]
        if deal is None and rest["source"] == "wolt":
            deal = (area.w_deals.get(rest["slug"]) or {}).get("item_deals", {}).get(item["id"])
        if deals and (not deal or (deals == "bogof" and deal["type"] not in ("bogof", "free_item"))):
            continue

        score = 0.0
        if tokens:
            rest_name = rest_names.get(rest["id"])
            if rest_name is None:
                rest_name = rest_names[rest["id"]] = food.fold(rest.get("name"))
            # Every word must match the dish or the restaurant name, at least one the dish itself
            matched, hit_in_item = True, False
            name = item["_n"]
            for token, pattern in zip(tokens, patterns):
                if name.startswith(token):
                    score += 6
                elif pattern.search(name):
                    score += 4
                elif token in name:
                    score += 3
                elif token in item["_h"]:
                    score += 1.2
                elif token in rest_name:
                    score += 0.3
                    continue
                else:
                    matched = False
                    break
                hit_in_item = True
            if not (matched and hit_in_item):
                continue
        if item["kind"] != "food" and (not tokens or score < 3):
            continue
        if open_only and rest.get("open_now") is False:
            closed += 1  # reported so the page can suggest switching off "open now"
            continue
        if item["kind"] != "food":
            score -= 5
        if not tokens:
            # Browsing without a search word: main dishes before sides, snacks and dips
            if cuisine_term and cuisine_term in item["_h"]:
                score += 3
            if _SIDE.search(f"{item['_n']} {food.fold(item['cat'])}") or (price is not None and price < 600):
                score -= 2.5
        if deal:
            score += {"bogof": 3.0, "free_item": 2.0}.get(deal["type"], 1.5) + (deal.get("pct") or 0) / 50
        if diet and item["diet_src"] in ("label", "text"):
            score += 1.0
        score += (rest.get("rating") or 3.8) * 0.4
        score -= ((rest.get("distance_m") or 3000) / 1000) * 0.3
        results.append((score, rest, item, deal))

    sorters = {
        "price": lambda r: (r[2]["price_cents"] if r[2]["price_cents"] is not None else 1e9, -r[0]),
        "rating": lambda r: (-(r[1].get("rating") or 0), -r[0]),
        "eta": lambda r: (r[1].get("eta_min") or 999, -r[0]),
        "discount": lambda r: (DEAL_ORDER.get((r[3] or {}).get("type"), 9), -((r[3] or {}).get("pct") or 0), -r[0]),
    }
    results.sort(key=sorters.get(sort, lambda r: -r[0]))

    # At most a few dishes per restaurant; the rest is counted on its first dish
    capped, shown, first = [], {}, {}
    for score, rest, item, deal in results:
        rid = rest["id"]
        n = shown.get(rid, 0)
        shown[rid] = n + 1
        if n < PER_RESTAURANT:
            if n == 0:
                first[rid] = len(capped)
            capped.append([rest, item, deal, 0])
        else:
            capped[first[rid]][3] += 1

    page = []
    for rest, item, deal, more in capped[offset: offset + limit]:
        out = food.public_item(item)
        out["deal"] = deal
        out["rest"] = _rest_brief(rest)
        out["more_in_rest"] = more
        page.append(out)
    return {
        "items": page,
        "total": len(capped),
        "matches": len(results),
        "closed_matches": closed,
        "restaurants": len(shown),
        "diet": diet,
        "tokens": tokens,
        "index": dict(area.index),
        "wolt": {"state": wolt_state, "query": wolt_query,
                 "error": wolt_entry["error"] if wolt_entry else None},
    }
