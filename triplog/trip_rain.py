"""Rain on a trip, graded by strength (L-003, step 1: recording).

The range model shall later learn how much rain costs. It needs to know, for
each trip, how wet it was where the car drove. This module fills that in from
the weather archive (Open-Meteo, same service as the wind): the hourly
precipitation (mm) at the start and at the end of the trip, for the hours the
trip touches, averaged to mm/h and graded:

    0 dry (< 0.1 mm/h) · 1 light (< 1) · 2 moderate (1–5) · 3 heavy (> 5)

Like wind.py it only ever asks for points of the fixed 50-km raster, never
for a trip's own position. The archive is a few days behind, so a trip gets
its value about a week later. Runs a few minutes after each start and then
once a day, only where the weather is switched on (WIND_SOURCE). What it
finds is stored in trips.rain_mmh / trips.rain_level and stays there; the
model does not use it yet (that needs a few hundred trips, see the
requirements).
"""

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import config
import wind

log = logging.getLogger("triplog.trip_rain")

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
MIN_AGE_DAYS = 7               # the archive is a few days behind
MAX_AGE_DAYS = 365             # the model looks back one year
LEVELS = (0.1, 1.0, 5.0)       # mm/h: below → dry, light, moderate; above the last → heavy
_HOUR = timedelta(hours=1)


def level(mmh):
    """Grade 0–3 for a precipitation rate in mm/h (None stays None)."""
    if mmh is None:
        return None
    return sum(1 for edge in LEVELS if mmh >= edge)


def _utc(local_iso, tz):
    try:
        dt = datetime.fromisoformat(str(local_iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).astimezone(timezone.utc)


def _fetch(point, day0, day1):
    """Hourly precipitation of one raster point → {"YYYY-MM-DDTHH" (UTC): mm}."""
    q = urllib.parse.urlencode({
        "latitude": f"{point[0]:.3f}", "longitude": f"{point[1]:.3f}",
        "start_date": day0, "end_date": day1, "hourly": "precipitation",
        "timeformat": "unixtime", "timezone": "GMT"})
    req = urllib.request.Request(ARCHIVE_URL + "?" + q, headers={"User-Agent": "IDmate"})
    with urllib.request.urlopen(req, timeout=60) as r:
        h = (json.loads(r.read().decode("utf-8")).get("hourly") or {})
    return {datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H"): v
            for t, v in zip(h.get("time") or [], h.get("precipitation") or []) if v is not None}


def trip_mmh(hourlies, start, end):
    """Mean precipitation (mm/h) over the hours a trip touches, across the
    raster points given (start, end). None when no point knows those hours."""
    if start is None:
        return None
    end = end if end and end >= start else start
    vals = []
    for hourly in hourlies:
        t = start.replace(minute=0, second=0, microsecond=0)
        n = 0
        while t <= end and n < 48:
            v = (hourly or {}).get(t.strftime("%Y-%m-%dT%H"))
            if v is not None:
                vals.append(v)
            t += _HOUR
            n += 1
    return round(sum(vals) / len(vals), 2) if vals else None


def run(db, tz, now=None, fetch=_fetch, pause=1.0):
    """Fill trips.rain_mmh / rain_level for trips old enough for the archive.
    Returns the number of trips filled."""
    if not config.WIND_SOURCE:
        return 0
    now = now or datetime.now(timezone.utc)
    newest, oldest = now - timedelta(days=MIN_AGE_DAYS), now - timedelta(days=MAX_AGE_DAYS)
    trips, points = [], {}
    for r in db.execute("SELECT id, start_time, end_time, start_lat, start_lon, end_lat, end_lon FROM trips "
                        "WHERE rain_mmh IS NULL AND start_lat IS NOT NULL AND start_lon IS NOT NULL").fetchall():
        t0, t1 = _utc(r["start_time"], tz), _utc(r["end_time"], tz)
        if not t0 or not oldest <= t0 <= newest:
            continue
        ps = {wind.grid(r["start_lat"], r["start_lon"], 0.0)[0]}           # nearest raster points
        if r["end_lat"] is not None and r["end_lon"] is not None:
            ps.add(wind.grid(r["end_lat"], r["end_lon"], 0.0)[0])
        for p in ps:
            d0, d1 = points.get(p, (t0, t1 or t0))
            points[p] = (min(d0, t0), max(d1, t1 or t0))
        trips.append((r["id"], t0, t1, ps))
    hourly = {}
    for p, (d0, d1) in points.items():
        try:
            hourly[p] = fetch(p, d0.strftime("%Y-%m-%d"), d1.strftime("%Y-%m-%d"))
        except Exception as e:      # next run tries again
            log.warning("trip rain: weather archive failed: %s", e)
        if pause:
            time.sleep(pause)                          # a handful of points, no hurry
    filled = 0
    for tid, t0, t1, ps in trips:
        if not all(p in hourly for p in ps):
            continue
        mmh = trip_mmh([hourly[p] for p in ps], t0, t1)
        if mmh is None:
            continue
        db.execute("UPDATE trips SET rain_mmh = ?, rain_level = ? WHERE id = ?", (mmh, level(mmh), tid))
        filled += 1
    db.commit()
    if trips:
        log.info("trip rain: %d of %d trips filled from the weather archive (%d raster points)",
                 filled, len(trips), len(points))
    return filled
