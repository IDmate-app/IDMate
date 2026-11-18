"""Rain on a trip, graded by strength (L-003, step 1: recording).

The range model shall later learn how much rain costs. It needs to know, for
each trip, how wet it was where the car drove. This module fills that in from
the weather archive (Open-Meteo, same service as the wind): the hourly
precipitation (mm) at the start and at the end of the trip, for the hours the
trip touches, averaged to mm/h and graded:

    0 dry (< 0.1 mm/h) · 1 light (< 1) · 2 moderate (1–5) · 3 heavy (> 5)

Like wind.py it only ever asks for points of the fixed 50-km raster, never
for a trip's own position. The archive is a few days behind, so a trip gets
its value about a week later.

Asked once per trip: every trip it has looked at gets trips.rain_checked
(time of the look), whether the archive had a value or not, and is never
asked for again. So the first run is a one-off back-fill of the last
RAIN_BACKFILL_DAYS (default 365) and every later run (once a day) only asks
for the trips that became old enough since. Network errors leave the trip
unchecked for the next day.

Open-Meteo is free for non-commercial use up to 10,000 calls a day (5,000
an hour, 600 a minute); a request over more than two weeks counts as one
call per two weeks. One request per raster point and at most CHUNK_DAYS,
PAUSE_S between them: a year back for a car that drove in 30 raster cells is
about 400 requests (some 900 counted calls), once.

What it finds is stored in trips.rain_mmh / rain_level and stays there. The
consumption model learns from it what rain costs (bms_learn.rain_effect).
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
CHUNK_DAYS = 31                # one request covers at most this many days (a month: small answers)
PAUSE_S = 2.0                  # between requests: no hurry, stay far below the limits
RETRIES = 2                    # a dropped connection is tried again after 5 × PAUSE_S
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


def run(db, tz, now=None, fetch=_fetch, pause=PAUSE_S, days=None):
    """Fill trips.rain_mmh / rain_level for trips old enough for the archive
    and not looked at before. Returns the number of trips filled."""
    if not config.WIND_SOURCE:
        return 0
    now = now or datetime.now(timezone.utc)
    days = days if days is not None else config.RAIN_BACKFILL_DAYS
    newest, oldest = now - timedelta(days=MIN_AGE_DAYS), now - timedelta(days=days)
    trips, points = [], {}
    for r in db.execute("SELECT id, start_time, end_time, start_lat, start_lon, end_lat, end_lon FROM trips "
                        "WHERE rain_checked IS NULL AND start_lat IS NOT NULL AND start_lon IS NOT NULL").fetchall():
        t0, t1 = _utc(r["start_time"], tz), _utc(r["end_time"], tz)
        if not t0 or not oldest <= t0 <= newest:
            continue
        ps = {wind.grid(r["start_lat"], r["start_lon"], 0.0)[0]}           # nearest raster points
        if r["end_lat"] is not None and r["end_lon"] is not None:
            ps.add(wind.grid(r["end_lat"], r["end_lon"], 0.0)[0])
        for p in ps:
            points.setdefault(p, set()).update({t0.date(), (t1 or t0).date()})
        trips.append((r["id"], t0, t1, ps))
    if not trips:
        return 0
    # per raster point: the days its trips touch, in chunks of at most CHUNK_DAYS
    hourly, failed, asked = {}, set(), 0
    for p, ds in points.items():
        ds = sorted(ds)
        chunks, start = [], ds[0]
        for a, b in zip(ds, ds[1:] + [None]):
            if b is None or (b - start).days >= CHUNK_DAYS:
                chunks.append((start, a))
                start = b
        got = {}
        for d0, d1 in chunks:
            for attempt in range(RETRIES + 1):
                try:
                    got.update(fetch(p, d0.strftime("%Y-%m-%d"), d1.strftime("%Y-%m-%d")))
                    asked += 1
                    break
                except Exception as e:
                    if attempt == RETRIES:      # this point again tomorrow
                        log.warning("trip rain: weather archive failed: %s", e)
                        failed.add(p)
                    elif pause:
                        time.sleep(pause * 5)
            if p in failed:
                break
            if pause:
                time.sleep(pause)
        hourly[p] = got
    filled = checked = 0
    stamp = int(now.timestamp())
    for tid, t0, t1, ps in trips:
        if ps & failed:
            continue
        mmh = trip_mmh([hourly[p] for p in ps], t0, t1)
        db.execute("UPDATE trips SET rain_mmh = ?, rain_level = ?, rain_checked = ? WHERE id = ?",
                   (mmh, level(mmh), stamp, tid))
        checked += 1
        filled += mmh is not None
    db.commit()
    log.info("trip rain: %d trips looked at, %d with a value (%d requests to the weather archive, %d raster points)",
             checked, filled, asked, len(points))
    return filled
