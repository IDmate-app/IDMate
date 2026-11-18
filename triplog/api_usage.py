"""Requests to outside services, counted per provider and day (admin →
Interfaces → "Outside services").

Every place that asks an outside service calls count(). Stored in the own
table api_usage (provider, day, requests, units): requests are the HTTP
requests, units what the service counts against its limit where that
differs (Open-Meteo counts each location of a request, and every two weeks
of an archive request, as one call). The limits shown are the published
free limits (as of November 2026); None where a service names no number.
"""

import logging
import sqlite3
import threading
from datetime import date, timedelta

import config

log = logging.getLogger("triplog.api_usage")

PROVIDERS = {
    "open-meteo": {"name": "Open-Meteo (wind forecast + weather archive)", "day": 10000, "month": 300000,
                   "note": "free for non-commercial use: 10,000 calls a day, 5,000 an hour, 600 a minute, "
                           "300,000 a month; one location and up to two weeks of data count as one call"},
    "dwd": {"name": "Deutscher Wetterdienst (rain radar)", "day": None, "month": None,
            "note": "open data (GeoNutzV), no fixed limit, fair use"},
    "rainviewer": {"name": "RainViewer (rain radar outside Germany)", "day": None, "month": None,
                   "note": "free API for personal, non-commercial use, tiles to be cached; no fixed number published"},
    "overpass": {"name": "Overpass API (ferries)", "day": 10000, "month": None,
                 "note": "public instance: up to about 10,000 queries and 1 GB a day"},
    "nominatim": {"name": "Nominatim (addresses)", "day": None, "month": None,
                  "note": "public OpenStreetMap server: at most one request a second; none for an own server"},
    "github": {"name": "GitHub (stick firmware releases)", "day": 1440, "month": None,
               "note": "without a token 60 requests an hour"},
}

_lock = threading.Lock()


def _db():
    db = sqlite3.connect(config.DB_PATH, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def count(provider, requests=1, units=None):
    """One (or more) requests to a provider today. Never raises: counting
    must not break the request it counts."""
    units = requests if units is None else units
    try:
        with _lock:
            db = _db()
            try:
                db.execute("INSERT INTO api_usage (provider, day, requests, units) VALUES (?, ?, ?, ?) "
                           "ON CONFLICT(provider, day) DO UPDATE SET requests = requests + excluded.requests, "
                           "units = units + excluded.units",
                           (provider, date.today().isoformat(), int(requests), int(units)))
                db.commit()
            finally:
                db.close()
    except Exception as e:
        log.debug("api usage: %s", e)


def summary(db, today=None):
    """Per provider: today and this month (requests, units) with the limits."""
    today = today or date.today()
    month0 = today.replace(day=1).isoformat()
    rows = {r["provider"]: r for r in db.execute(
        "SELECT provider, SUM(CASE WHEN day = ? THEN requests ELSE 0 END) AS rq_day, "
        "SUM(CASE WHEN day = ? THEN units ELSE 0 END) AS u_day, SUM(requests) AS rq_month, SUM(units) AS u_month "
        "FROM api_usage WHERE day >= ? GROUP BY provider", (today.isoformat(), today.isoformat(), month0))}
    out = []
    for key, p in PROVIDERS.items():
        r = rows.get(key)
        out.append({"provider": key, "name": p["name"], "note": p["note"],
                    "limit_day": p["day"], "limit_month": p["month"],
                    "requests_day": (r["rq_day"] or 0) if r else 0, "units_day": (r["u_day"] or 0) if r else 0,
                    "requests_month": (r["rq_month"] or 0) if r else 0, "units_month": (r["u_month"] or 0) if r else 0})
    return out


def history(db, provider, days=60, today=None):
    """Daily counts of one provider, oldest first, days without requests as 0."""
    today = today or date.today()
    start = today - timedelta(days=days - 1)
    got = {r["day"]: r for r in db.execute(
        "SELECT day, requests, units FROM api_usage WHERE provider = ? AND day >= ?", (provider, start.isoformat()))}
    out = []
    for k in range(days):
        d = (start + timedelta(days=k)).isoformat()
        r = got.get(d)
        out.append({"day": d, "requests": r["requests"] if r else 0, "units": r["units"] if r else 0})
    return out
