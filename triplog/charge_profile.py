"""Charging power profile over all charges of a vehicle.

Two views for the analysis tab, filled from the stick's rows while charging:

- by SoC: 5-% steps on the x-axis,
- by time: minutes since the charge began (DC 2-min steps, AC 10-min steps).

Each charge is first averaged per step, then the charges are combined per
step: mean, standard deviation, minimum and maximum across the charges. So a
long charge does not outweigh a short one, and min/max show the best and the
worst charge at that point (e.g. a cold battery).

Metrics: power (kW), current (A), voltage (V), battery temperature (°C).
Power and current come as positive values (the stick sends charging negative).
"""

import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone

import config
import detector

log = logging.getLogger(__name__)

METRICS = ("p", "i", "u", "bt")
QUERY_FIELDS = ("p", "i", "u", "bt", "s", "c", "dc", "v", "_eq", "_na")
SOC_STEP = 5
TIME_STEP_MIN = {"dc": 2, "ac": 10}
TIME_MAX_MIN = {"dc": 120, "ac": 24 * 60}
SESSION_GAP_S = 10 * 60      # charging rows further apart = a new charge
MIN_ROWS = 3                 # fewer charging rows = not a usable charge
DC_POWER_KW = 22.0           # above this the charge counts as DC even without the dc flag
WINDOW_PAD = timedelta(minutes=10)
WINDOW_MAX = timedelta(hours=24)
CACHE_TTL_S = 30 * 60

_cache = {}
_cache_lock = threading.Lock()


def is_charging_row(r):
    """The car charges: flag from the stick, or clearly negative power while standing."""
    if (r.get("c") or 0) >= 0.5 or (r.get("dc") or 0) >= 0.5:
        return True
    p = r.get("p")
    return p is not None and p <= -1.0 and (r.get("v") or 0) < 1.0


def split_sessions(rows):
    """Expanded, time-sorted rows -> list of charges (lists of charging rows)."""
    sessions, cur, last_t = [], [], None
    for r in rows:
        t = r.get("_time")
        if t is None:
            continue
        if not is_charging_row(r):
            if cur:
                sessions.append(cur)
            cur, last_t = [], None
            continue
        if last_t is not None and (t - last_t).total_seconds() > SESSION_GAP_S:
            sessions.append(cur)
            cur = []
        cur.append(r)
        last_t = t
    if cur:
        sessions.append(cur)
    return [s for s in sessions if len(s) >= MIN_ROWS]


def session_type(rows):
    if any((r.get("dc") or 0) >= 0.5 for r in rows):
        return "dc"
    if any(abs(r.get("p") or 0) > DC_POWER_KW for r in rows):
        return "dc"
    return "ac"


def _value(r, k):
    v = r.get(k)
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(v):
        return None
    if k in ("p", "i"):
        return abs(v)
    return v


def _session_buckets(rows, key_fn):
    """One charge -> {bucket: {metric: mean}}."""
    acc = {}
    for r in rows:
        b = key_fn(r)
        if b is None:
            continue
        slot = acc.setdefault(b, {m: [] for m in METRICS})
        for m in METRICS:
            v = _value(r, m)
            if v is not None:
                slot[m].append(v)
    return {b: {m: (sum(vs) / len(vs)) for m, vs in slot.items() if vs} for b, slot in acc.items()}


def _combine(per_session):
    """[{bucket: {metric: mean}}] -> sorted [{x, n, metric: {mean, std, min, max}}]."""
    by_bucket = {}
    for sess in per_session:
        for b, vals in sess.items():
            slot = by_bucket.setdefault(b, {"n": 0, **{m: [] for m in METRICS}})
            slot["n"] += 1
            for m, v in vals.items():
                slot[m].append(v)
    out = []
    for b in sorted(by_bucket):
        slot = by_bucket[b]
        item = {"x": b, "n": slot["n"]}
        for m in METRICS:
            vs = slot[m]
            if not vs:
                item[m] = None
                continue
            mean = sum(vs) / len(vs)
            std = math.sqrt(sum((v - mean) ** 2 for v in vs) / len(vs)) if len(vs) > 1 else 0.0
            item[m] = {"mean": round(mean, 1), "std": round(std, 1),
                       "min": round(min(vs), 1), "max": round(max(vs), 1)}
        out.append(item)
    return out


def build_profile(rows, kind):
    """Expanded rows of a vehicle -> profile for 'dc' or 'ac' charges."""
    sessions = split_sessions(rows)
    counts = {"dc": 0, "ac": 0}
    chosen = []
    for s in sessions:
        t = session_type(s)
        counts[t] += 1
        if t == kind:
            chosen.append(s)

    step = TIME_STEP_MIN[kind]
    tmax = TIME_MAX_MIN[kind]
    by_soc, by_time = [], []
    for s in chosen:
        t0 = s[0]["_time"]

        def soc_key(r):
            v = _value(r, "s")
            if v is None or v < 0 or v > 100:
                return None
            return min(int(v // SOC_STEP) * SOC_STEP, 100 - SOC_STEP)

        def time_key(r, t0=t0):
            mins = (r["_time"] - t0).total_seconds() / 60.0
            if mins < 0 or mins >= tmax:
                return None
            return int(mins // step) * step

        by_soc.append(_session_buckets(s, soc_key))
        by_time.append(_session_buckets(s, time_key))

    return {
        "type": kind,
        "sessions": len(chosen),
        "counts": counts,
        "soc_step": SOC_STEP,
        "time_step_min": step,
        "by_soc": _combine(by_soc),
        "by_time": _combine(by_time),
    }


def merge_windows(windows):
    """[(start, end)] aware datetimes -> padded, clipped, merged, sorted."""
    ws = []
    for s, e in windows:
        if s is None:
            continue
        e = e or s
        if e < s:
            s, e = e, s
        if e - s > WINDOW_MAX:
            e = s + WINDOW_MAX
        ws.append((s - WINDOW_PAD, e + WINDOW_PAD))
    ws.sort()
    merged = []
    for s, e in ws:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _local(ts):
    if not ts:
        return None
    s = str(ts).strip().replace(" ", "T")
    try:
        if s.endswith("Z") or "+" in s[10:]:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s).replace(tzinfo=config.LOCAL_TZ)
    except ValueError:
        return None


def charge_windows(db, device, plate):
    """Time windows of all known charges of the vehicle: charges seen in the
    telemetry (table charges) and charge sessions (wallbox, external)."""
    rows = db.execute("SELECT start_time, end_time FROM charges WHERE device = ?", (device,)).fetchall()
    wins = [(_local(r["start_time"]), _local(r["end_time"])) for r in rows]
    if plate:
        rows = db.execute("SELECT start_time, end_time FROM charge_sessions WHERE vehicle_plate = ?",
                          (plate,)).fetchall()
        wins += [(_local(r["start_time"]), _local(r["end_time"])) for r in rows]
    return merge_windows(wins)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_rows(device, windows, client=None):
    """Stick rows inside the windows, expanded (delta compression resolved)."""
    own = client is None
    client = client or detector.get_influx(timeout_ms=15000)
    if not client:
        return []
    fields = " or ".join(f'r._field == "{f}"' for f in QUERY_FIELDS)
    out = []
    try:
        for s, e in windows:
            q = (f'from(bucket: "{config.INFLUX_BUCKET}")\n'
                 f'  |> range(start: {_iso(s)}, stop: {_iso(e)})\n'
                 f'  |> filter(fn: (r) => r._measurement == "v" and r.d == "{device}")\n'
                 f'  |> filter(fn: (r) => {fields})\n'
                 f'  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
                 f'  |> sort(columns: ["_time"])\n')
            raw = []
            try:
                for table in client.query_api().query(q, org=config.INFLUX_ORG):
                    for rec in table.records:
                        row = {k: rec.values.get(k) for k in QUERY_FIELDS}
                        row["_time"] = rec.get_time()
                        raw.append(row)
            except Exception:
                log.warning("charge profile: window %s..%s failed", s, e, exc_info=True)
                continue
            # each window on its own: a value must not carry over from a far earlier charge
            out.extend(detector.expand_rows(raw))
    finally:
        if own:
            client.close()
    return out


def profile_for(db, device, plate, kind):
    """Cached profile of one vehicle (30 min, or until a new charge is known)."""
    windows = charge_windows(db, device, plate)
    sig = (len(windows), windows[-1][1].isoformat() if windows else "")
    key = (device, kind)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[1] == sig and time.time() - hit[0] < CACHE_TTL_S:
            return hit[2]
    rows = fetch_rows(device, windows) if windows else []
    result = build_profile(rows, kind)
    result["windows"] = len(windows)
    with _cache_lock:
        _cache[key] = (time.time(), sig, result)
    return result
