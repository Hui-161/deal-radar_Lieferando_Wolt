"""Deal-Radar - lokaler Server. Start: python app.py -> http://localhost:8765"""
import time

import requests
from flask import Flask, jsonify, request, send_from_directory

from providers import lieferando, wolt

app = Flask(__name__, static_folder="static")

_geocode_cache = {}


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/lieferando")
def api_lieferando():
    city = (request.args.get("city") or "").strip().lower()
    plz = (request.args.get("plz") or "").strip()
    if not city or not plz:
        return jsonify(ok=False, error="city und plz erforderlich"), 400
    try:
        return jsonify(ok=True, restaurants=lieferando.fetch_deals(city, plz))
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502


def _city_slug(name: str) -> str:
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        name = name.replace(a, b)
    return "-".join(name.lower().split())


@app.get("/api/geocode")
def api_geocode():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify(ok=False, error="q erforderlich"), 400
    if q in _geocode_cache:
        return jsonify(ok=True, **_geocode_cache[q])
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": q,
                "format": "json",
                "limit": 1,
                "countrycodes": "de",
                "addressdetails": 1,
            },
            headers={"User-Agent": "deal-radar-local/0.1 (private use)"},
            timeout=10,
        )
        r.raise_for_status()
        hits = r.json()
        if not hits:
            return jsonify(ok=False, error="Adresse nicht gefunden"), 404
        hit = hits[0]
        addr = hit.get("address", {})
        city = addr.get("city") or addr.get("town") or addr.get("village") or ""
        result = {
            "lat": float(hit["lat"]),
            "lon": float(hit["lon"]),
            "city": _city_slug(city),
            "plz": addr.get("postcode", ""),
        }
        _geocode_cache[q] = result
        time.sleep(1)  # Nominatim usage policy: max 1 req/s
        return jsonify(ok=True, **result)
    except requests.RequestException as e:
        return jsonify(ok=False, error=str(e)), 502


@app.get("/api/deal-items")
def api_deal_items():
    source = request.args.get("source", "")
    slug = (request.args.get("slug") or "").strip()
    if source not in ("lieferando", "wolt") or not slug:
        return jsonify(ok=False, error="source (lieferando|wolt) und slug erforderlich"), 400
    provider = lieferando if source == "lieferando" else wolt
    try:
        return jsonify(ok=True, offers=provider.fetch_deal_items(slug))
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502


@app.post("/api/wolt/scan")
def api_wolt_scan_start():
    body = request.get_json(silent=True) or {}
    try:
        lat, lon = float(body["lat"]), float(body["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify(ok=False, error="lat/lon erforderlich"), 400
    limit = int(body.get("limit", 150))
    scan_id = wolt.start_scan(lat, lon, limit)
    return jsonify(ok=True, scan_id=scan_id)


@app.get("/api/wolt/scan/<scan_id>")
def api_wolt_scan_status(scan_id):
    state = wolt.get_scan(scan_id)
    if state is None:
        return jsonify(ok=False, error="unbekannte scan_id"), 404
    return jsonify(ok=True, **state)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765, debug=False)
