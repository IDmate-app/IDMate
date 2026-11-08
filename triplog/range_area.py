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
import urllib.error
import urllib.request

import config

log = logging.getLogger("triplog.range_area")

CACHE_S = 6 * 3600
FAIL_S = 10 * 60
TIMEOUT_S = 120

_lock = threading.Lock()
_cache = {}            # key -> {"t": time, "geojson": … | None, "error": str | None}
_busy = set()
_down_until = 0.0      # Valhalla unreachable: no new request for anyone until then


def key(lat, lon, kms):
    return (round(lat * 50) / 50, round(lon * 50) / 50,
            tuple(sorted(int(round(k / 10.0)) * 10 for k in kms)))


def request_body(lat, lon, kms):
    return {"locations": [{"lat": lat, "lon": lon}], "costing": "auto",
            "contours": [{"distance": float(k)} for k in sorted(kms)],
            "polygons": True, "denoise": 0.5, "generalize": 1500}


def _fetch(lat, lon, kms):
    req = urllib.request.Request(config.VALHALLA_URL + "/isochrone",
                                 data=json.dumps(request_body(lat, lon, kms)).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8"))


def compute(lat, lon, kms):
    """Synchronous Valhalla request → (geojson, error). Respects the global
    back-off while Valhalla is unreachable."""
    if time.time() < _down_until:
        return None, "Valhalla not reachable"
    k = key(lat, lon, kms)
    with _lock:
        _busy.add(k)
    _run(k, lat, lon, kms)
    with _lock:
        c = _cache.get(k) or {}
    return c.get("geojson"), c.get("error")


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
        # Not reachable at all (still building, container down): back off
        # for every position, not just this one, so the log stays quiet.
        unreachable = isinstance(e, (ConnectionError, TimeoutError)) or (
            isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError))
        global _down_until
        with _lock:
            first = _down_until < time.time()
            if unreachable:
                _down_until = time.time() + FAIL_S
        if first or not unreachable:
            log.warning("range area: Valhalla failed (%s) — dashboard keeps the circle%s", err,
                        " (next try in 10 min)" if unreachable else "")
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
        if now < _down_until:
            return {"status": "error", "error": "Valhalla not reachable"}
        if k not in _busy:
            _busy.add(k)
            threading.Thread(target=_run, args=(k, lat, lon, kms), name="range-area", daemon=True).start()
        if len(_cache) > 50:                    # old positions
            for old in sorted(_cache, key=lambda x: _cache[x]["t"])[:len(_cache) - 50]:
                _cache.pop(old, None)
    return {"status": "pending"}


# ── Pre-rendered area per vehicle (background, persisted) ──────────────────
# The dashboard shows the last stored area at once; the background loop
# renders a new one only when position/range changed, at most every
# RENDER_EVERY_S, so Valhalla is not asked on every page view.

RENDER_EVERY_S = 10 * 60
_fail_at = {}          # device -> time of the last failed render (no retry every minute)


def load_last(db, device):
    """Stored area of a vehicle (own table, not settings: holds the car's
    position, which must stay out of the diagnostic export)."""
    row = db.execute("SELECT data FROM range_area_last WHERE device = ?", (device,)).fetchone()
    if not row or not row["data"]:
        return None
    try:
        return json.loads(row["data"])
    except ValueError:
        return None


def render_if_due(db, device, lat, lon, kms, dirs=None, now=None, refine=None):
    """Render and store when nothing is stored yet, or the inputs changed and
    the stored area is at least RENDER_EVERY_S old. dirs: road km per
    bearing (wind_range) — the area is then composed from several contours.
    Returns "rendered", "unchanged", "wait" or "failed"."""
    import wind_range
    now = now if now is not None else time.time()
    lv = wind_range.levels(dirs) if dirs else None
    if lv and len(lv) == 1:              # wind changes nothing worth a shape
        kms, dirs, lv = [lv[0]], None, None
    req = lv or kms
    k = list(key(lat, lon, req))
    kk = [k[0], k[1], list(k[2])] + ([[int(round(d / 10.0)) * 10 for d in dirs]] if dirs else [])
    last = load_last(db, device)
    if last:
        if last.get("key") == kk:
            return "unchanged"
        # A different kind of range (one value instead of a span, with or
        # without wind) is a new display, not a small change: no waiting.
        same_kind = (len(last.get("kms") or []) == len(kms)) and bool(last.get("dirs")) == bool(dirs)
        if now - (last.get("ts") or 0) < RENDER_EVERY_S and same_kind:
            return "wait"
    if now - _fail_at.get(device, 0) < RENDER_EVERY_S:
        return "wait"
    # Due: now the expensive part — ranges along real routes (speed limits,
    # elevation); the key above stays on the cheap estimate, so this runs
    # only when a new area is rendered anyway.
    if refine and dirs:
        try:
            better = refine()
        except Exception as e:
            log.warning("range area: routes per direction failed: %s", e)
            better = None
        if better:
            dirs = better
            lv = wind_range.levels(dirs)
            if len(lv) == 1:
                kms, dirs, lv = [lv[0]], None, None
            req = lv or [round(x) for x in kms]
    gj, err = compute(lat, lon, req)
    if gj is not None and dirs:
        gj = wind_range.compose(gj, dirs, lat, lon)
    elif gj is not None and len(req) == 1:
        # one contour: same clean outline (no holes, slivers, little loops)
        gj = wind_range.compose(gj, None, lat, lon) or gj
    if gj is None:
        _fail_at[device] = now
        return "failed"
    _fail_at.pop(device, None)
    rec = {"key": kk, "lat": lat, "lon": lon,
           "kms": [round(x) for x in kms], "ts": now, "geojson": gj}
    if dirs:
        rec["dirs"] = dirs
    db.execute("INSERT OR REPLACE INTO range_area_last (device, data, ts) VALUES (?, ?, ?)",
               (device, json.dumps(rec, separators=(",", ":")), now))
    db.commit()
    log.info("range area: rendered for %s (%s km)", device, "/".join(str(x) for x in rec["kms"]))
    return "rendered"
