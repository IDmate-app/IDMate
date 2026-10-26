"""Plausibility of trips that were filled in by hand (telemetry gap) or whose
start/end was corrected afterwards.

Pure functions, no database: the caller passes the trip row (dict-like) and
whether it overlaps another trip. Used by the gap editor (admin → gaps) and by
the red asterisk + tooltip in the trips list.
"""

import re
from datetime import datetime

import fahrzeugmodell as fm

# Average speed over the trip's own window (start → end, parking included).
# Below 3 km/h the window is far too long for the km (or the km too few),
# above 130 km/h the car cannot have covered the km in that time.
SPEED_MIN_KMH = 3.0
SPEED_MAX_KMH = 130.0
# Below SLOW_CHECK_MIN_KM a slow average says nothing (short hop, long stop).
SLOW_CHECK_MIN_KM = 5.0
# kWh/100 km an electric car can plausibly show over a whole trip.
CONS_MIN = 8.0
CONS_MAX = 40.0
# Consumption over very short trips is noise (SoC resolution 1 %) — the
# one rule of the vehicle model.
CONS_MIN_KM = float(fm.MIN_CONS_KM)
# SoC points tolerated between the end of one trip and the start of the next
SOC_STEP_MAX = 3

_GPS_RE = re.compile(r"^\s*\(?\s*(-?\d{1,3}(?:\.\d+)?)\s*[,; ]\s*(-?\d{1,3}(?:\.\d+)?)\s*\)?\s*$")


def parse_gps(value):
    """'53.60308082178861, 9.888429999859472' (Google Maps "copy") → (lat, lon).

    Also accepts ';' or a blank as separator and optional parentheses.
    Returns None for empty or invalid input (out of range included)."""
    if value is None:
        return None
    m = _GPS_RE.match(str(value))
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def _parse_ts(value):
    if not value:
        return None
    s = str(value).strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s[:19] if len(s) >= 19 else s, fmt)
        except ValueError:
            continue
    return None


def _get(trip, key):
    try:
        return trip[key]
    except (KeyError, IndexError, TypeError):
        return None


def check_trip(trip, overlaps=False):
    """Plausibility of one trip.

    Returns a dict:
      time:  'ok' | 'missing' | 'order' | 'overlap' | 'fast' | 'slow'
      speed: average km/h over the trip window (None without times/km)
      cons_state: 'ok' | 'bad' | 'none'
      cons:  kWh/100 km (None when not derivable)
      ok:    True when neither times nor consumption are implausible
    """
    t0, t1 = _parse_ts(_get(trip, "start_time")), _parse_ts(_get(trip, "end_time"))
    km = _get(trip, "distance_km")
    speed = None
    if t0 is None or t1 is None:
        time_state = "missing"
    elif t1 <= t0:
        time_state = "order"
    else:
        hours = (t1 - t0).total_seconds() / 3600.0
        speed = round(km / hours, 1) if km else None
        if overlaps:
            time_state = "overlap"
        elif speed is not None and speed > SPEED_MAX_KMH:
            time_state = "fast"
        elif speed is not None and speed < SPEED_MIN_KMH and (km or 0) >= SLOW_CHECK_MIN_KM:
            time_state = "slow"
        else:
            time_state = "ok"

    cons = _get(trip, "consumption")
    if cons is None:
        energy = _get(trip, "energy_kwh")
        if energy and km and km >= CONS_MIN_KM:
            cons = round(energy / km * 100.0, 1)
    if cons is None:
        cons_state = "none"
    elif CONS_MIN <= cons <= CONS_MAX:
        cons_state = "ok"
    else:
        cons_state = "bad"

    return {"time": time_state, "speed": speed, "cons_state": cons_state,
            "cons": cons, "ok": time_state == "ok" and cons_state != "bad"}


def flag_title(trip, chk, t):
    """Tooltip text for the red asterisk in the trips list (one line each)."""
    lines = [t["trips_flag_manual"] if _get(trip, "is_manual") else t["trips_flag_corrected"]]
    speed = "–" if chk["speed"] is None else f"{chk['speed']:.0f}"
    lines.append({
        "ok": t["trips_check_time_ok"],
        "missing": t["trips_check_time_missing"],
        "order": t["trips_check_time_order"],
        "overlap": t["trips_check_time_overlap"],
        "fast": t["trips_check_time_fast"],
        "slow": t["trips_check_time_slow"],
    }[chk["time"]].replace("{0}", speed))
    cons = "–" if chk["cons"] is None else f"{chk['cons']:.1f}"
    lines.append({
        "ok": t["trips_check_cons_ok"],
        "bad": t["trips_check_cons_bad"],
        "none": t["trips_check_cons_none"],
    }[chk["cons_state"]].replace("{0}", cons))
    return "\n".join(lines)


def is_flagged(trip):
    """Filled in by hand or start/end corrected afterwards."""
    return bool(_get(trip, "is_manual") or _get(trip, "manual_fields"))
