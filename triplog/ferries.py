"""Long ferries on request (map button "Fähren").

Short crossings (≤ wind_range.MAX_FERRY_KM) are part of the normal range
area anyway. Long ones (Travemünde–Trelleborg/Helsinki, Kiel–Oslo, …) only
when the user asks: one search, then shown until switched off.

Valhalla has no "list the ferries" call, so they are found by routing: far
targets in all directions (across the Baltic, the North Sea, Scandinavia);
every route Valhalla sends over a long ferry reveals terminal, destination,
length and duration. For each reachable terminal the energy left on arrival
gives a remaining range, and Valhalla draws the area around the port of
arrival with it.
"""

import logging
import math
import threading
import time

import bms_learn as bl
import config
import wind_range as wr

log = logging.getLogger("triplog.ferries")

TARGET_KM = (500, 900, 1300)     # straight-line distances of the search targets
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


def _target(lat, lon, th, km):
    return (lat + km * math.cos(math.radians(th)) / wr.KY,
            lon + km * math.sin(math.radians(th)) / (wr.KX0 * math.cos(math.radians(lat))))


def discover(lat, lon, route_fn=None):
    """Long ferries Valhalla uses from here: [{"a", "b", "km", "h", "name",
    "line", "prof", "via_km"}], one per terminal pair (the nearest way to it)."""
    route_fn = route_fn or wr._route
    found = {}
    for th in wr.bearings():
        for km in TARGET_KM:
            la, lo = _target(lat, lon, th, km)
            if abs(la) > 80:
                continue
            try:
                prof = wr.route_profile(route_fn(lat, lon, la, lo))
            except Exception:
                prof = None
            if not prof:
                continue
            for c in prof.get("crossings") or []:
                sea = c["km_to"] - c["km_from"]
                if sea <= wr.MAX_FERRY_KM:
                    continue                      # short ones are in the normal area
                a, b = prof["pts"][c["b0"]], prof["pts"][c["b1"]]
                key = (round(a[0], 2), round(a[1], 2), round(b[0], 2), round(b[1], 2))
                if key in found and found[key]["via_km"] <= c["km_from"]:
                    continue
                found[key] = {"a": a, "b": b, "km": round(sea), "h": round(c["h"], 1),
                              "name": c["name"], "via_km": c["km_from"],
                              "line": prof["pts"][c["b0"]:c["b1"] + 1], "prof": prof}
    return sorted(found.values(), key=lambda f: f["via_km"])[:MAX_FERRIES]


def _run(device, lat, lon, cruise, points, route_fn=None, area_fn=None):
    import range_area
    try:
        ferries = []
        for f in discover(lat, lon, route_fn=route_fn):
            left = wr.range_along_route(cruise["left"], cruise["model"], cruise.get("temp"), f["prof"],
                                        points, time.time(), until_km=f["via_km"])
            if left is None or left <= 0:
                continue                          # terminal out of reach
            ride = bl.cruise_range(left, cruise["model"], cruise.get("temp"), v=bl.CRUISE_AVG_KMH)
            km_after = ride["km"] if ride else 0
            item = {"from": [round(f["a"][0], 5), round(f["a"][1], 5)],
                    "to": [round(f["b"][0], 5), round(f["b"][1], 5)],
                    "sea_km": f["km"], "hours": f["h"], "name": f["name"],
                    "to_terminal_km": round(f["via_km"]), "kwh_left": round(left, 1),
                    "km_after": km_after,
                    "line": [[round(la, 5), round(lo, 5)] for la, lo in f["line"][::max(1, len(f["line"]) // 200)]],
                    "area": None}
            if km_after >= 20:
                k = min(int(km_after), 800)
                res_area = (area_fn or range_area.compute)(f["b"][0], f["b"][1], [k])
                gj = res_area[0] if isinstance(res_area, tuple) else res_area
                if gj:
                    item["area"] = wr.compose(gj, None, f["b"][0], f["b"][1]) or gj
            ferries.append(item)
        res = {"status": "ok", "ts": time.time(), "lat": lat, "lon": lon, "ferries": ferries}
        log.info("ferries: %d long crossings reachable", len(ferries))
    except Exception as e:
        log.warning("ferries: search failed: %s", e, exc_info=True)
        res = {"status": "error", "ts": time.time(), "lat": lat, "lon": lon, "error": str(e)}
    with _lock:
        _jobs[device] = res
