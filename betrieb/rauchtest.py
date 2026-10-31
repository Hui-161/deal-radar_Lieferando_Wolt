"""Rauchtest für Deal-Radar nach einem Update. Läuft auf dem Host gegen 127.0.0.1:8765:

    sudo python3 /opt/deal-radar/betrieb/rauchtest.py

Lädt eine Testadresse (Berlin 10119), wartet auf den Speisekarten-Index und die Uber-Eats-Liste und
prüft Suche, Speisekarte und Eingabeprüfung. Kostet bei Wolt drei Abfragen (Liste, Suche, ggf.
Wiederholung) und bei Uber Eats bis zu fünf (Listenseiten).
"""
import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=60) as r:
            body = r.read()
            return r.status, dict(r.headers), json.loads(body) if "json" in r.headers.get("Content-Type", "") else None
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read() or b"null")


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


status, headers, _ = get("/")
print("Startseite", status, "| CSP mit Bild-Hosts:", "imageproxy.wolt.com" in headers.get("Content-Security-Policy", ""))
area = post("/api/area", {"plz": "10119", "lat": 52.5305, "lon": 13.4053})["area"]
t0 = time.time()
while True:
    _, _, st = get(f"/api/area/{area}/status")
    if st["index"]["status"] in ("done", "skipped") and st["wolt"]["status"] != "loading" \
            and st["uber"]["status"] != "loading" or time.time() - t0 > 120:
        break
    time.sleep(3)
print("Lieferando", st["lieferando"], "| Wolt", st["wolt"])
print("Uber Eats", st["uber"])
print("Index", st["index"], "nach", round(time.time() - t0), "s | Wolt-Angebote", st["scan"])

_, _, d = get(f"/api/area/{area}/dishes?q=pizza&diet=vegan&open=0")
for _ in range(10):
    if d["wolt"]["state"] != "loading":
        break
    time.sleep(3)
    _, _, d = get(f"/api/area/{area}/dishes?q=pizza&diet=vegan&open=0")
print("Pizza + vegan:", d["matches"], "Gerichte in", d["restaurants"], "Restaurants | Wolt-Suche:", d["wolt"]["state"])
print("Erste Treffer:", [(i["name"], i["rest"]["source"]) for i in d["items"][:5]])

lief = next((i for i in d["items"] if i["rest"]["source"] == "lieferando"), None)
if lief:
    _, _, m = get(f"/api/menu?source=lieferando&slug={lief['rest']['slug']}&area={area}")
    print("Speisekarte", lief["rest"]["slug"], "ok" if m["ok"] else m, len(m.get("categories", [])), "Kategorien")
print("Eingabeprüfung (erwartet 400/400/404):", get(f"/api/area/{area}/dishes?diet=xyz")[0],
      get("/api/menu?source=wolt&slug=../x")[0], get("/api/area/0123456789abcdef/status")[0])
