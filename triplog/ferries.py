"""Long ferries on request (map button "Fähren").

Short crossings (≤ wind_range.MAX_FERRY_KM) are part of the normal range
area anyway. Long ones (Travemünde–Trelleborg/Helsinki, Kiel–Oslo, …) only
when the user asks: one search, then shown until switched off.

Valhalla routes by time and never picks a 9-hour ferry where a road exists,
so the ferries come from OpenStreetMap (Overpass): car ferries (route=ferry,
motor vehicles allowed) in a box around the car, rounded outward to whole
degrees, so the service only learns the region. Per terminal in reach a
Valhalla route gives the energy left there; the remaining range is drawn
around the port of arrival.
"""

import logging
import math
import threading
import time

import bms_learn as bl
import config
import wind_range as wr

log = logging.getLogger("triplog.ferries")

MAX_FERRIES = 8
CACHE_S = 3 * 3600

_lock = threading.Lock()
_jobs = {}                       # device -> {"status", "ts", "ferries", "error"}


def status(device):
    with _lock:
        j = _jobs.get(device)
        return dict(j) if j else {"status": "none"}


def start(device, lat, lon, cruise, points):
    """Start the search in the background unless one is running or a fresh
    result for about this position exists."""
    with _lock:
        j = _jobs.get(device)
        if j and j["status"] == "running":
            return dict(j)
        if j and j["status"] == "ok" and time.time() - j["ts"] < CACHE_S \
                and abs(j.get("lat", 0) - lat) < 0.05 and abs(j.get("lon", 0) - lon) < 0.05:
            return dict(j)
        _jobs[device] = {"status": "running", "ts": time.time(), "lat": lat, "lon": lon}
    threading.Thread(target=_run, args=(device, lat, lon, cruise, points),
                     name="ferries", daemon=True).start()
    return status(device)


def _km(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, h)))


def bbox(lat, lon, radius_km):
    """(south, west, north, east) around a position, rounded outward to whole degrees."""
    dlat = radius_km / wr.KY
    dlon = radius_km / (wr.KX0 * math.cos(math.radians(lat)))
    return (math.floor(lat - dlat), math.floor(lon - dlon), math.ceil(lat + dlat), math.ceil(lon + dlon))


def _urls():
    """FERRY_URL may list several Overpass servers, comma separated: the
    public ones are often overloaded (502/504), then the next one is asked."""
    return [u.strip() for u in (config.FERRY_URL or "").split(",") if u.strip()]


def _overpass(box, opener=None):
    import json
    import urllib.error
    import urllib.parse
    import urllib.request
    s, w, n, e = box
    q = ("[out:json][timeout:60];("
         f'way["route"="ferry"]["motor_vehicle"="yes"]({s},{w},{n},{e});'
         f'way["route"="ferry"]["motorcar"="yes"]({s},{w},{n},{e});'
         ");out tags geom;")
    body = urllib.parse.urlencode({"data": q}).encode()
    opener = opener or urllib.request.urlopen
    errors = []
    for url in _urls():
        req = urllib.request.Request(url, data=body, headers={"User-Agent": "IDmate"})
        try:
            with opener(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            if ex.code not in (429, 500, 502, 503, 504):
                raise
            errors.append(f"{urllib.parse.urlsplit(url).hostname}: HTTP {ex.code}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as ex:
            errors.append(f"{urllib.parse.urlsplit(url).hostname}: {ex}")
        log.info("ferries: overpass %s", errors[-1])
    raise OSError("overpass busy (" + "; ".join(errors) + ")")


def crossings(osm):
    """Overpass answer → [{"pts": [(lat, lon)], "km", "name", "h"}]: ways
    joined at shared ends (a long ferry is often split), only > MAX_FERRY_KM."""
    ways = []
    for el in (osm or {}).get("elements") or []:
        g = [(p["lat"], p["lon"]) for p in el.get("geometry") or [] if p]
        if len(g) >= 2:
            ways.append({"pts": g, "tags": el.get("tags") or {}})
    key = lambda p: (round(p[0], 5), round(p[1], 5))
    chains = []
    while ways:
        cur = ways.pop()
        pts, tags = list(cur["pts"]), dict(cur["tags"])
        grown = True
        while grown:
            grown = False
            for w in ways:
                a, b = w["pts"][0], w["pts"][-1]
                if key(a) == key(pts[-1]):
                    pts += w["pts"][1:]
                elif key(b) == key(pts[-1]):
                    pts += w["pts"][::-1][1:]
                elif key(b) == key(pts[0]):
                    pts = w["pts"][:-1] + pts
                elif key(a) == key(pts[0]):
                    pts = w["pts"][::-1][:-1] + pts
                else:
                    continue
                for k, v in w["tags"].items():
                    tags.setdefault(k, v)
                ways.remove(w)
                grown = True
                break
        km = sum(_km(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        if km <= wr.MAX_FERRY_KM:
            continue
        h = None
        dur = tags.get("duration") or ""
        try:
            parts = [int(x) for x in dur.split(":")]
            h = parts[0] + (parts[1] / 60 if len(parts) > 1 else 0)
        except ValueError:
            h = None
        chains.append({"pts": pts, "km": round(km), "name": tags.get("name") or tags.get("ref") or "",
                       "h": round(h if h else km / 30.0, 1)})
    return chains


def _run(device, lat, lon, cruise, points, route_fn=None, area_fn=None, osm_fn=None):
    import range_area
    try:
        route_fn = route_fn or wr._route
        reach = cruise["km"] if cruise.get("km") else 300
        osm = (osm_fn or _overpass)(bbox(lat, lon, reach))
        cands = []
        for c in crossings(osm):
            a, b = c["pts"][0], c["pts"][-1]
            if _km((lat, lon), b) < _km((lat, lon), a):
                c["pts"] = c["pts"][::-1]                 # terminal = the end nearer to the car
                a, b = b, a
            if _km((lat, lon), a) <= reach:
                cands.append(c)
        cands.sort(key=lambda c: _km((lat, lon), c["pts"][0]))
        energy_at = {}                                    # terminal → kWh left (one route each)
        ferries = []
        for c in cands:
            if len(ferries) >= MAX_FERRIES:
                break
            a, b = c["pts"][0], c["pts"][-1]
            tk = (round(a[0], 3), round(a[1], 3))
            if tk not in energy_at:
                left, via = None, None
                try:
                    prof = wr.route_profile(route_fn(lat, lon, a[0], a[1]))
                    if prof:
                        via = prof["km"]
                        left = wr.range_along_route(cruise["left"], cruise["model"], cruise.get("temp"),
                                                    prof, points, time.time(), until_km=via)
                except Exception:
                    left = None
                energy_at[tk] = (left, via)
            left, via = energy_at[tk]
            if left is None or left <= 0:
                continue                                  # terminal out of reach
            ride = bl.cruise_range(left, cruise["model"], cruise.get("temp"), v=bl.CRUISE_AVG_KMH)
            km_after = ride["km"] if ride else 0
            item = {"from": [round(a[0], 5), round(a[1], 5)], "to": [round(b[0], 5), round(b[1], 5)],
                    "sea_km": c["km"], "hours": c["h"], "name": c["name"],
                    "to_terminal_km": round(via), "kwh_left": round(left, 1), "km_after": km_after,
                    "line": [[round(la, 5), round(lo, 5)] for la, lo in c["pts"][::max(1, len(c["pts"]) // 200)]],
                    "area": None}
            if km_after >= 20:
                res_area = (area_fn or range_area.compute)(b[0], b[1], [min(int(km_after), 800)])
                gj = res_area[0] if isinstance(res_area, tuple) else res_area
                if gj:
                    item["area"] = wr.compose(gj, None, b[0], b[1]) or gj
            ferries.append(item)
        res = {"status": "ok", "ts": time.time(), "lat": lat, "lon": lon, "ferries": ferries}
        log.info("ferries: %d long crossings found, %d reachable", len(cands), len(ferries))
    except Exception as e:
        log.warning("ferries: search failed: %s", e, exc_info=True)
        res = {"status": "error", "ts": time.time(), "lat": lat, "lon": lon, "error": str(e)}
    with _lock:
        _jobs[device] = res
