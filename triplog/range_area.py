"""Road-based range area from an own Valhalla (docker-compose.valhalla.yml).

Valhalla's isochrone endpoint with distance contours gives the area reachable
over roads within N km. The position never leaves the own network. Requests
run in the background (a large contour can take a while); the dashboard shows
the circle until the area is there. Cached per ~2 km position grid and 10 km
range steps; a failure is remembered for a while so the dashboard does not
hammer a Valhalla that is still building or has the 200 km default limit."""

import json
import logging
import threading
import time
import urllib.request

import config

log = logging.getLogger("triplog.range_area")

CACHE_S = 6 * 3600
FAIL_S = 10 * 60
TIMEOUT_S = 120

_lock = threading.Lock()
_cache = {}            # key -> {"t": time, "geojson": … | None, "error": str | None}
_busy = set()


def key(lat, lon, kms):
    return (round(lat * 50) / 50, round(lon * 50) / 50,
            tuple(sorted(int(round(k / 10.0)) * 10 for k in kms)))


def request_body(lat, lon, kms):
    return {"locations": [{"lat": lat, "lon": lon}], "costing": "auto",
            "contours": [{"distance": float(k)} for k in sorted(kms)],
            "polygons": True, "denoise": 0.5, "generalize": 500}


def _fetch(lat, lon, kms):
    req = urllib.request.Request(config.VALHALLA_URL + "/isochrone",
                                 data=json.dumps(request_body(lat, lon, kms)).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8"))


def _run(k, lat, lon, kms):
    gj, err = None, None
    try:
        gj = _fetch(lat, lon, kms)
        if not isinstance(gj, dict) or not gj.get("features"):
            gj, err = None, "empty"
    except Exception as e:                      # HTTP 400 (limit), timeout, not built yet …
        body = getattr(e, "read", None)
        detail = ""
        if body:
            try:
                detail = body().decode("utf-8", "replace")[:200]
            except Exception:
                pass
        err = f"{type(e).__name__}: {e} {detail}".strip()
        log.warning("range area: Valhalla failed (%s) — dashboard keeps the circle", err)
    with _lock:
        _cache[k] = {"t": time.time(), "geojson": gj, "error": err}
        _busy.discard(k)


def area(lat, lon, kms):
    """{"status": "off" | "pending" | "ok" | "error", "geojson"?, "error"?}.
    Starts the computation in the background when nothing usable is cached."""
    if not config.VALHALLA_URL:
        return {"status": "off"}
    k = key(lat, lon, kms)
    now = time.time()
    with _lock:
        c = _cache.get(k)
        if c and c["geojson"] is not None and now - c["t"] < CACHE_S:
            return {"status": "ok", "geojson": c["geojson"]}
        if c and c["geojson"] is None and now - c["t"] < FAIL_S:
            return {"status": "error", "error": c["error"]}
        if k not in _busy:
            _busy.add(k)
            threading.Thread(target=_run, args=(k, lat, lon, kms), name="range-area", daemon=True).start()
        if len(_cache) > 50:                    # old positions
            for old in sorted(_cache, key=lambda x: _cache[x]["t"])[:len(_cache) - 50]:
                _cache.pop(old, None)
    return {"status": "pending"}
