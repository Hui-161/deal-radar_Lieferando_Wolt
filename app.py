"""Deal-Radar - lokaler Server. Start: python app.py -> http://localhost:8765"""
import os
import re
import time

import requests
from flask import Flask, jsonify, request, send_from_directory

import area as areas
from providers import food, uber, wolt

app = Flask(__name__, static_folder="static")

_geocode_cache = {}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,120}$")
_PLZ_RE = re.compile(r"^\d{5}$")
_AREA_RE = re.compile(r"^[0-9a-f]{16}$")
_SORTS = {"relevance", "price", "rating", "eta", "discount"}
_SOURCES = ("lieferando", "wolt", "uber")
_IMG_HOSTS = (
    "https://res.cloudinary.com https://just-eat-prod-eu-res.cloudinary.com "
    "https://imageproxy.wolt.com https://wolt-menu-images-cdn.wolt.com https://tb-static.uber.com"
)
_NOMINATIM_HEADERS = {"User-Agent": "deal-radar-local/0.1 (private use)"}


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "geolocation=(self), camera=(), microphone=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        f"img-src 'self' data: {_IMG_HOSTS}; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'none'"
    )
    return resp


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


def _city_slug(name: str) -> str:
    name = name.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        name = name.replace(a, b)
    return re.sub(r"[^a-z0-9-]", "", "-".join(name.split()))


def _place(hit: dict) -> dict:
    addr = hit.get("address", {})
    city = addr.get("city") or addr.get("town") or addr.get("village") or ""
    street = " ".join(x for x in (addr.get("road"), addr.get("house_number")) if x)
    return {
        "lat": float(hit["lat"]),
        "lon": float(hit["lon"]),
        "city": _city_slug(city),
        "plz": addr.get("postcode", ""),
        "label": ", ".join(x for x in (street, f"{addr.get('postcode', '')} {city}".strip()) if x),
    }


def _nominatim(path: str, params: dict):
    r = requests.get(f"https://nominatim.openstreetmap.org/{path}", params=params,
                     headers=_NOMINATIM_HEADERS, timeout=10)
    r.raise_for_status()
    time.sleep(1)  # Nominatim usage policy: max 1 req/s
    return r.json()


@app.get("/api/geocode")
def api_geocode():
    q = (request.args.get("q") or "").strip()
    if not q or len(q) > 200:
        return jsonify(ok=False, error="q erforderlich"), 400
    if q in _geocode_cache:
        return jsonify(ok=True, **_geocode_cache[q])
    try:
        hits = _nominatim("search", {"q": q, "format": "json", "limit": 1, "countrycodes": "de", "addressdetails": 1})
    except requests.RequestException as e:
        return jsonify(ok=False, error=str(e)), 502
    if not hits:
        return jsonify(ok=False, error="Adresse nicht gefunden"), 404
    result = _place(hits[0])
    if len(_geocode_cache) > 500:
        _geocode_cache.clear()
    _geocode_cache[q] = result
    return jsonify(ok=True, **result)


@app.get("/api/reverse")
def api_reverse():
    try:
        lat, lon = float(request.args["lat"]), float(request.args["lon"])
    except (KeyError, ValueError):
        return jsonify(ok=False, error="lat/lon erforderlich"), 400
    if not (47 <= lat <= 56 and 5 <= lon <= 16):
        return jsonify(ok=False, error="Standort liegt nicht in Deutschland"), 400
    try:
        hit = _nominatim("reverse", {"lat": f"{lat:.5f}", "lon": f"{lon:.5f}", "format": "json", "addressdetails": 1})
    except requests.RequestException as e:
        return jsonify(ok=False, error=str(e)), 502
    if not hit or "lat" not in hit:
        return jsonify(ok=False, error="Adresse nicht gefunden"), 404
    return jsonify(ok=True, **_place(hit))


@app.post("/api/area")
def api_area():
    body = request.get_json(silent=True) or {}
    plz = str(body.get("plz") or "").strip()
    try:
        lat, lon = float(body["lat"]), float(body["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify(ok=False, error="lat/lon erforderlich"), 400
    if not _PLZ_RE.match(plz):
        return jsonify(ok=False, error="Postleitzahl fehlt – bitte die Adresse mit PLZ eingeben"), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify(ok=False, error="lat/lon ungültig"), 400
    a = areas.get_or_create(plz, lat, lon)
    return jsonify(ok=True, area=a.id, cuisines=[
        {"key": c[0], "label": c[1], "emoji": c[2]} for c in food.CUISINES
    ])


def _area_or_404(area_id: str):
    a = areas.get(area_id) if _AREA_RE.match(area_id) else None
    if a is None:
        return None, (jsonify(ok=False, error="Suche abgelaufen – bitte neu laden", expired=True), 404)
    return a, None


@app.get("/api/area/<area_id>/status")
def api_area_status(area_id):
    a, err = _area_or_404(area_id)
    if err:
        return err
    return jsonify(ok=True, **areas.status(a))


@app.get("/api/area/<area_id>/restaurants")
def api_area_restaurants(area_id):
    a, err = _area_or_404(area_id)
    if err:
        return err
    source = request.args.get("source", "")
    if source not in _SOURCES:
        return jsonify(ok=False, error="source (lieferando|wolt|uber) erforderlich"), 400
    return jsonify(ok=True, restaurants=areas.restaurants(a, source))


@app.post("/api/area/<area_id>/prioritize")
def api_area_prioritize(area_id):
    """The page reports the Wolt venues it shows; their deals are checked first."""
    a, err = _area_or_404(area_id)
    if err:
        return err
    slugs = (request.get_json(silent=True) or {}).get("slugs")
    if not isinstance(slugs, list) or not all(isinstance(s, str) and _SLUG_RE.match(s) for s in slugs[:40]):
        return jsonify(ok=False, error="slugs erforderlich"), 400
    return jsonify(ok=True, queued=areas.prioritize(a, slugs[:40]))


@app.get("/api/area/<area_id>/dishes")
def api_area_dishes(area_id):
    a, err = _area_or_404(area_id)
    if err:
        return err
    args = request.args
    try:
        max_price = int(args["max_price"]) if args.get("max_price") else None
        offset = max(0, min(int(args.get("offset", 0)), 5000))
        limit = max(1, min(int(args.get("limit", 48)), 96))
    except ValueError:
        return jsonify(ok=False, error="ungültige Zahl"), 400
    diet = args.get("diet", "")
    cuisine = args.get("cuisine", "")
    source = args.get("source", "")
    deals = args.get("deals", "")
    sort = args.get("sort", "relevance")
    if diet not in ("", "vegan", "vegetarian") or source not in ("", *_SOURCES) or sort not in _SORTS \
            or deals not in ("", "any", "bogof") or (cuisine and cuisine not in food.CUISINE_BY_KEY):
        return jsonify(ok=False, error="ungültiger Filter"), 400
    result = areas.search_dishes(
        a, q=(args.get("q") or "")[:100], diet=diet, cuisine=cuisine, deals=deals,
        max_price=max_price, source=source, open_only=args.get("open", "1") == "1", sort=sort,
        offset=offset, limit=limit,
    )
    return jsonify(ok=True, **result)


@app.get("/api/menu")
def api_menu():
    source = request.args.get("source", "")
    slug = (request.args.get("slug") or "").strip()
    if source not in _SOURCES or not _SLUG_RE.match(slug):
        return jsonify(ok=False, error="source (lieferando|wolt|uber) und slug erforderlich"), 400
    area_id = request.args.get("area", "")
    a = areas.get(area_id) if _AREA_RE.match(area_id) else None
    try:
        return jsonify(ok=True, **areas.menu(a, source, slug))
    except (wolt.WoltBusy, uber.UberBlocked) as e:
        return jsonify(ok=False, error=str(e), busy=True), 503
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8765")), debug=False,
            threaded=True)
