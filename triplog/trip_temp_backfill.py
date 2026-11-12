"""TEST BUILD, one-off — can go again once the old trips have their value.

Outside temperature for trips from before the car reported its own: the
consumption model (bms_learn.speed_model) learns the cold from the trips,
and the coldest months of a car often lie before its telemetry began. The
weather archive (Open-Meteo, same service as the wind) knows the hourly
temperature of those days.

Like wind.py it only ever asks for points of the fixed 50-km raster, never
for a trip's own start. What it finds is stored in trips.temp_c and stays
there; this module only fills it. It runs once after each start, asks
nothing when no trip lacks a temperature, and only where the weather is
switched on (WIND_SOURCE).
"""

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import bms_learn as bl
import config
import wind

log = logging.getLogger("triplog.trip_temp")

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
MIN_AGE_DAYS = 7               # the archive is a few days behind
MAX_AGE_DAYS = 365             # the model looks back one year


def _utc(local_iso, tz):
    try:
        dt = datetime.fromisoformat(str(local_iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).astimezone(timezone.utc)


def _fetch(point, day0, day1):
    """Hourly temperature of one raster point → {"YYYY-MM-DDTHH" (UTC): °C}."""
    q = urllib.parse.urlencode({
        "latitude": f"{point[0]:.3f}", "longitude": f"{point[1]:.3f}",
        "start_date": day0, "end_date": day1, "hourly": "temperature_2m",
        "timeformat": "unixtime", "timezone": "GMT"})
    req = urllib.request.Request(ARCHIVE_URL + "?" + q, headers={"User-Agent": "IDmate"})
    with urllib.request.urlopen(req, timeout=60) as r:
        h = (json.loads(r.read().decode("utf-8")).get("hourly") or {})
    return {datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H"): v
            for t, v in zip(h.get("time") or [], h.get("temperature_2m") or []) if v is not None}


def run(db, sensor_hourly, tz, now=None, fetch=_fetch):
    """Fill trips.temp_c where neither the car's sensor (sensor_hourly:
    device → {"YYYY-MM-DDTHH": °C}) nor an earlier run has a temperature.
    Returns the number of trips filled."""
    if not config.WIND_SOURCE:
        return 0
    now = now or datetime.now(timezone.utc)
    newest, oldest = now - timedelta(days=MIN_AGE_DAYS), now - timedelta(days=MAX_AGE_DAYS)
    todo, sensors = {}, {}
    for r in db.execute("SELECT id, device, start_time, end_time, start_lat, start_lon FROM trips "
                        "WHERE temp_c IS NULL AND start_lat IS NOT NULL AND start_lon IS NOT NULL "
                        "AND device IS NOT NULL").fetchall():
        t0, t1 = _utc(r["start_time"], tz), _utc(r["end_time"], tz)
        if not t0 or not oldest <= t0 <= newest:
            continue
        if r["device"] not in sensors:
            sensors[r["device"]] = sensor_hourly(r["device"]) or {}
        if bl.trip_temp(sensors[r["device"]], t0, t1) is not None:
            continue                                   # the car measured it itself
        point = wind.grid(r["start_lat"], r["start_lon"], 0.0)[0]      # nearest raster point
        todo.setdefault(point, []).append((r["id"], t0, t1))
    filled = 0
    for point, trips in todo.items():
        day0 = min(t0 for _i, t0, _t1 in trips).strftime("%Y-%m-%d")
        day1 = max((t1 or t0) for _i, t0, t1 in trips).strftime("%Y-%m-%d")
        try:
            hourly = fetch(point, day0, day1)
        except Exception as e:      # next start tries again
            log.warning("trip temperatures: weather archive failed: %s", e)
            continue
        for tid, t0, t1 in trips:
            temp = bl.trip_temp(hourly, t0, t1)
            if temp is not None:
                db.execute("UPDATE trips SET temp_c = ? WHERE id = ?", (temp, tid))
                filled += 1
        db.commit()
        time.sleep(1)                                  # a handful of points, no hurry
    if todo:
        log.info("trip temperatures: %d of %d trips without a sensor value filled from the weather archive "
                 "(%d raster points)", filled, sum(len(v) for v in todo.values()), len(todo))
    return filled
