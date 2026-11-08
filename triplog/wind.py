"""Wind forecast on a fixed raster around the car (Open-Meteo).

Every GRID_KM a point with wind direction, speed, gusts and temperature for
the next HOURS hours. The raster is fixed on the globe (rows every GRID_KM in
latitude, columns every GRID_KM along each row), so the weather service only
ever sees raster points of the region, never the car's own position.
Refreshed every REFRESH_S: each fetch covers 24 h, so consecutive forecasts
overlap and a 3-hour trip can use the forecast for the time it gets there.

Step 1 only shows the wind on the map; the range does not use it yet.
"""

import json
import logging
import math
import threading
import time
import urllib.parse
import urllib.request

import config

log = logging.getLogger("triplog.wind")

GRID_KM = 50.0
HOURS = 24
REFRESH_S = 6 * 3600
FAIL_S = 30 * 60
BATCH = 100
MAX_POINTS = 400
LAT_STEP = GRID_KM / 111.2

_lock = threading.Lock()
_down_until = 0.0


def _dist_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, a)))


def grid(lat, lon, radius_km):
    """Raster points within radius_km (+ one cell) of a position."""
    r = radius_km + GRID_KM
    pts = []
    i0 = math.floor((lat - r / 111.2) / LAT_STEP)
    i1 = math.ceil((lat + r / 111.2) / LAT_STEP)
    for i in range(i0, i1 + 1):
        la = round(i * LAT_STEP, 3)
        if abs(la) > 85:
            continue
        k = 111.2 * math.cos(math.radians(la))
        step = GRID_KM / k
        j0 = math.floor((lon - r / k) / step)
        j1 = math.ceil((lon + r / k) / step)
        for j in range(j0, j1 + 1):
            lo = round(((j * step + 180) % 360) - 180, 3)
            if _dist_km(lat, lon, la, lo) <= r:
                pts.append((la, lo))
    pts.sort(key=lambda p: _dist_km(lat, lon, p[0], p[1]))
    return pts[:MAX_POINTS]


def _fetch(points):
    """One Open-Meteo request for up to BATCH points → list of hourly dicts."""
    q = urllib.parse.urlencode({
        "latitude": ",".join(f"{p[0]:.3f}" for p in points),
        "longitude": ",".join(f"{p[1]:.3f}" for p in points),
        "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m,temperature_2m",
        "wind_speed_unit": "kmh", "forecast_hours": HOURS,
        "timeformat": "unixtime", "timezone": "GMT"})
    req = urllib.request.Request(config.WIND_URL + "?" + q, headers={"User-Agent": "IDmate"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    data = data if isinstance(data, list) else [data]
    out = []
    for d in data:
        h = d.get("hourly") or {}
        out.append({"t": h.get("time") or [], "kmh": h.get("wind_speed_10m") or [],
                    "dir": h.get("wind_direction_10m") or [], "gust": h.get("wind_gusts_10m") or [],
                    "temp": h.get("temperature_2m") or []})
    return out


def _key(p):
    return f"{p[0]:.3f},{p[1]:.3f}"


def ensure(db, lat, lon, radius_km, now=None):
    """Fetch the raster points around a position that are missing or older
    than REFRESH_S. Returns the number of points fetched."""
    global _down_until
    if not config.WIND_SOURCE:
        return 0
    now = now if now is not None else time.time()
    if now < _down_until:
        return 0
    pts = grid(lat, lon, radius_km)
    have = {r["key"]: r["fetched"] for r in db.execute(
        "SELECT key, fetched FROM wind_points WHERE fetched >= ?", (now - REFRESH_S,))}
    todo = [p for p in pts if _key(p) not in have]
    done = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        try:
            rows = _fetch(batch)
        except Exception as e:
            with _lock:
                _down_until = now + FAIL_S
            log.warning("wind: forecast fetch failed (%s) — next try in 30 min", e)
            break
        for p, d in zip(batch, rows):
            db.execute("INSERT OR REPLACE INTO wind_points (key, lat, lon, fetched, data) "
                       "VALUES (?, ?, ?, ?, ?)", (_key(p), p[0], p[1], now, json.dumps(d)))
        done += len(batch)
    # forget points nobody has needed for two days
    db.execute("DELETE FROM wind_points WHERE fetched < ?", (now - 2 * 86400,))
    db.commit()
    if done:
        log.info("wind: forecast for %d raster points fetched", done)
    return done


def at(d, t):
    """Values of one point at time t (the hour that contains t), or None."""
    ts = d.get("t") or []
    for i in range(len(ts)):
        if ts[i] <= t < ts[i] + 3600:
            try:
                return {"kmh": d["kmh"][i], "dir": d["dir"][i],
                        "gust": d["gust"][i] if i < len(d["gust"]) else None,
                        "temp": d["temp"][i] if i < len(d["temp"]) else None}
            except (IndexError, KeyError):
                return None
    return None


def current(db, lat, lon, radius_km, now=None):
    """Raster points around a position with their wind for the current hour."""
    now = now if now is not None else time.time()
    keys = {_key(p) for p in grid(lat, lon, radius_km)}
    out = []
    for r in db.execute("SELECT key, lat, lon, data FROM wind_points"):
        if r["key"] not in keys:
            continue
        try:
            v = at(json.loads(r["data"]), now)
        except ValueError:
            v = None
        if v and v["kmh"] is not None and v["dir"] is not None:
            out.append({"lat": r["lat"], "lon": r["lon"], **v})
    return out
