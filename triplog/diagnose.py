"""Diagnostics: health checks, recent log lines and a ZIP export of the
recorded data, the derived figures and the parameters of the current
calculation model — everything needed to find errors in real operation.

Not exported: users, passkeys, guest links and their devices, personal
notes, encrypted settings and anything that looks like a secret.
"""

import csv
import io
import json
import logging
import platform
import sqlite3
import time
import zipfile
from collections import deque
from datetime import datetime, timedelta, timezone

import config
import trip_check

# ── Recent log lines (ring buffer) ─────────────────────────────────────────

LOG_RING = deque(maxlen=3000)
_started = time.time()


class _RingHandler(logging.Handler):
    def emit(self, record):
        try:
            LOG_RING.append({
                "ts": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage()[:2000],
                "exc": self.formatter.formatException(record.exc_info)[-4000:]
                       if record.exc_info and self.formatter else None,
            })
        except Exception:
            pass


def install_log_ring():
    """Keep the last log records (INFO and up) in memory, once per process."""
    root = logging.getLogger()
    if any(isinstance(h, _RingHandler) for h in root.handlers):
        return
    h = _RingHandler(level=logging.INFO)
    h.setFormatter(logging.Formatter())
    root.addHandler(h)


def log_lines(min_level="INFO", limit=500):
    lv = logging.getLevelName(min_level)
    lv = lv if isinstance(lv, int) else logging.INFO
    out = [r for r in LOG_RING if logging.getLevelName(r["level"]) >= lv]
    return out[-limit:]


# ── What goes into the export ──────────────────────────────────────────────

EXCLUDED_TABLES = {
    "users", "passkeys", "journey_shares", "journey_share_devices", "user_notes",
    "user_vehicle_access", "purpose_visibility", "preset_value_visibility",
    "route_rule_visibility", "sqlite_sequence",
    "settings",  # exported redacted as settings.json
}
# Tables cut to the export range: table → SQL condition on ? (from) / ? (to)
RANGE_FILTER = {
    "charge_readings": "timestamp >= ? AND timestamp < ?",
    "charges": "start_time >= ? AND start_time < ?",
    "gpx_waypoints": "trip_id IN (SELECT id FROM trips WHERE start_time >= ? AND start_time < ?)",
}
SECRET_WORDS = ("token", "password", "passwd", "secret", "api_key", "apikey", "aes", "credential")
COORD_COLS = {"lat", "lon", "lat2", "lon2", "start_lat", "start_lon", "end_lat", "end_lon",
              "start_geo_lat", "start_geo_lon", "end_geo_lat", "end_geo_lon"}
ADDRESS_COLS = {"start_address", "end_address", "address", "polygon_coords"}


def _is_secret(name):
    n = name.lower()
    return any(w in n for w in SECRET_WORDS)


def redact_settings(rows, encrypted_keys):
    """settings rows → dict; encrypted and secret-looking values hidden."""
    out = {}
    for r in rows:
        k, v = r["key"], r["value"]
        if k == "secret_key" or k in encrypted_keys or _is_secret(k):
            out[k] = "[redacted]" if v else ""
        else:
            out[k] = v
    return out


def config_snapshot():
    """Upper-case settings of config.py; secrets and URLs only as set/empty."""
    out = {}
    for k in dir(config):
        if not k.isupper() or k.startswith("_"):
            continue
        v = getattr(config, k)
        if not isinstance(v, (str, int, float, bool, list, tuple, dict, set, type(None))):
            continue
        if _is_secret(k) or k.endswith("_URL") or k.endswith("_PASS") or k.endswith("_KEY") or k.endswith("_USER"):
            out[k] = "[set]" if v else "[empty]"
        else:
            out[k] = sorted(v) if isinstance(v, set) else v
    return out


def _vin(v):
    return (v[:3] + "…" + v[-4:]) if v and len(v) > 8 else v


def table_names(db):
    return [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        if r[0] not in EXCLUDED_TABLES]


def table_csv(db, table, since, until, anon=False, full=()):
    """One table as CSV text (range-filtered where RANGE_FILTER says so,
    unless the table is in full)."""
    sql, params = f'SELECT * FROM "{table}"', ()
    if table in RANGE_FILTER and table not in full:
        sql += " WHERE " + RANGE_FILTER[table]
        params = (since, until)
    cur = db.execute(sql, params)
    cols = [d[0] for d in cur.description]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    n = 0
    for row in cur:
        vals = []
        for c, v in zip(cols, row):
            if isinstance(v, (bytes, memoryview)):
                v = f"[{len(v)} bytes]"
            elif table == "vehicles" and c == "vin":
                v = _vin(v)
            elif table == "settings" and c == "value" and _is_secret(row[0] or ""):
                v = "[redacted]" if v else ""
            elif anon and c in COORD_COLS and isinstance(v, (int, float)):
                v = round(v, 2)
            elif anon and c in ADDRESS_COLS and v:
                v = "[anon]"
            vals.append(v)
        w.writerow(vals)
        n += 1
    return buf.getvalue(), n


def rows_csv(rows, cols=None):
    buf = io.StringIO()
    rows = list(rows)
    cols = cols or (list(rows[0].keys()) if rows else [])
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


# ── Health checks ──────────────────────────────────────────────────────────

def _check(key, level, value=None, vehicle=None, samples=None, detail=None):
    return {"key": key, "level": level, "value": value, "vehicle": vehicle,
            "samples": samples or [], "detail": detail}


def acked(db, key):
    """Ids the user marked as checked for this finding."""
    try:
        return {r[0] for r in db.execute("SELECT ref_id FROM diag_ack WHERE check_key = ?", (key,))}
    except Exception:           # table missing (before migration 72)
        return set()


def _without_acked(sql, key):
    """The finding's SQL without the ids marked as checked."""
    return sql.replace(" ORDER BY", " AND id NOT IN (SELECT ref_id FROM diag_ack WHERE check_key = "
                       f"'{key}') ORDER BY", 1)


def _ids(db, sql, params=(), limit=20):
    rows = db.execute(sql + f" LIMIT {int(limit) + 1}", params).fetchall()
    return [r[0] for r in rows]


OVERLAP_TOL_MIN = 5   # imported trips carry minute-rounded times


def _ts(v):
    try:
        return datetime.fromisoformat(str(v).replace(" ", "T")[:19])
    except ValueError:
        return None


def overlapping_trips(rows, tol_min=OVERLAP_TOL_MIN):
    """Ids of trips whose time overlaps another trip by more than tol_min,
    newest first. rows: (id, start_time, end_time) sorted by start."""
    tol = timedelta(minutes=tol_min)
    trips = [(r[0], _ts(r[1]), _ts(r[2])) for r in rows]
    trips = [t for t in trips if t[1] and t[2]]
    trips.sort(key=lambda t: t[1])
    hit = {}
    for i, (ida, sa, ea) in enumerate(trips):
        for idb, sb, eb in trips[i + 1:]:
            if sb >= ea - tol:
                break
            if eb > sa + tol:
                hit[ida] = sa
                hit[idb] = sb
    return [i for i, _ in sorted(hit.items(), key=lambda x: x[1], reverse=True)]


def sql_checks(db, vehicles, efficiency):
    """Checks that need only SQLite. vehicles: rows with plate/device."""
    out = []
    if not (0.8 <= efficiency <= 0.99):
        out.append(_check("charge_efficiency", "warn", round(efficiency, 3)))
    else:
        out.append(_check("charge_efficiency", "ok", round(efficiency, 3)))
    for v in vehicles:
        plate, dev = v["plate"], v["device"]
        if not dev:
            out.append(_check("vehicle_no_device", "warn", vehicle=plate))
            continue
        q = [
            # Unchanged SoC is the resolution limit of short trips (1 % ≈
            # 0.5-0.9 kWh), not a missing value — only from 20 km a finding.
            ("trips_no_energy", "warn",
             "SELECT id FROM trips WHERE device = ? AND distance_km >= 1 AND energy_kwh IS NULL "
             "AND COALESCE(is_gpx, 0) = 0 AND (soc_start IS NULL OR soc_end IS NULL "
             "OR soc_start > soc_end) ORDER BY start_time DESC"),
            ("trips_soc_unchanged", "info",
             "SELECT id FROM trips WHERE device = ? AND distance_km >= 20 AND soc_start = soc_end "
             "ORDER BY start_time DESC"),
            # Below 20 km a 1 % SoC step alone moves the figure by 4+ kWh/100 km.
            ("trips_consumption_outlier", "warn",
             "SELECT id FROM trips WHERE device = ? AND distance_km >= 20 AND consumption IS NOT NULL "
             f"AND (consumption < {trip_check.CONS_MIN} OR consumption > {trip_check.CONS_MAX}) "
             "ORDER BY start_time DESC"),
            ("trips_soc_rises", "warn",
             "SELECT id FROM trips WHERE device = ? AND distance_km >= 1 AND soc_end > soc_start + 2 "
             "ORDER BY start_time DESC"),
            ("trips_negative", "error",
             "SELECT id FROM trips WHERE device = ? AND (distance_km < 0 OR end_time < start_time) "
             "ORDER BY start_time DESC"),
            ("trips_time_format", "error",
             "SELECT id FROM trips WHERE device = ? AND (start_time LIKE '%+%' OR start_time LIKE '%Z' "
             "OR end_time LIKE '%+%' OR end_time LIKE '%Z') ORDER BY start_time DESC"),
            ("trips_no_purpose", "info",
             "SELECT id FROM trips WHERE device = ? AND (purpose IS NULL OR purpose = '') "
             "ORDER BY start_time DESC"),
        ]
        for key, level, sql in q:
            sql = _without_acked(sql, key)
            ids = _ids(db, sql, (dev,))
            n = db.execute(f"SELECT COUNT(*) FROM ({sql})", (dev,)).fetchone()[0] if ids else 0
            out.append(_check(key, level if n else "ok", n, plate, ids[:20]))
        # Overlapping trips: one sweep over the trips sorted by start (a SQL
        # self-join took 20 s for 5000 trips).
        ov = overlapping_trips(db.execute(
            "SELECT id, start_time, end_time FROM trips WHERE device = ? ORDER BY start_time",
            (dev,)).fetchall())
        ov = [i for i in ov if i not in acked(db, "trips_overlap")]
        out.append(_check("trips_overlap", "warn" if ov else "ok", len(ov), plate, ov[:20]))
        # Odometer running backwards between consecutive trips
        rows = db.execute(
            "SELECT id, odo_start, odo_end FROM trips WHERE device = ? AND odo_start IS NOT NULL "
            "AND odo_end IS NOT NULL ORDER BY start_time", (dev,)).fetchall()
        skip = acked(db, "odometer_backwards")
        back = [b["id"] for a, b in zip(rows, rows[1:])
                if b["odo_start"] < a["odo_end"] - 1 and b["id"] not in skip]
        out.append(_check("odometer_backwards", "error" if back else "ok", len(back), plate, back[-20:]))
        cq = [
            ("charges_no_kwh", "warn",
             "SELECT id FROM charge_sessions WHERE vehicle_plate = ? AND total_kwh IS NULL "
             "ORDER BY start_time DESC"),
            ("charges_no_odometer", "warn",
             "SELECT id FROM charge_sessions WHERE vehicle_plate = ? AND odometer IS NULL "
             "ORDER BY start_time DESC"),
            # Without SoC a charge only drops out of capacity and balance.
            ("charges_no_soc", "info",
             "SELECT id FROM charge_sessions WHERE vehicle_plate = ? AND (soc_start IS NULL "
             "OR soc_end IS NULL) ORDER BY start_time DESC"),
            # Below 1 kWh it is a top-up or standby blip, SoC noise rules there.
            ("charges_soc_drop", "warn",
             "SELECT id FROM charge_sessions WHERE vehicle_plate = ? AND soc_end < soc_start "
             "AND total_kwh >= 1 ORDER BY start_time DESC"),
            ("readings_unassigned", "info",
             "SELECT id FROM charge_readings WHERE vehicle_plate = ? AND session_id IS NULL "
             "ORDER BY timestamp DESC"),
        ]
        for key, level, sql in cq:
            sql = _without_acked(sql, key)
            ids = _ids(db, sql, (plate,))
            n = db.execute(f"SELECT COUNT(*) FROM ({sql})", (plate,)).fetchone()[0] if ids else 0
            out.append(_check(key, level if n else "ok", n, plate, ids[:20]))
    return out


def energy_mismatch_check(db, plate, device, cap_at):
    """Detected trips whose stored energy is off by more than 25 % from SoC
    span × capacity at the time (the extended-trip bug of 1.2.198)."""
    rows = db.execute(
        "SELECT id, start_time, soc_start, soc_end, energy_kwh FROM trips WHERE device = ? "
        "AND COALESCE(is_manual, 0) = 0 AND soc_start - soc_end >= 1 AND energy_kwh IS NOT NULL "
        "ORDER BY start_time DESC", (device,)).fetchall()
    bad = []
    for t in rows:
        cap = cap_at(t["start_time"])
        if cap:
            e = (t["soc_start"] - t["soc_end"]) / 100 * cap
            if abs(t["energy_kwh"] - e) > 0.25 * e:
                bad.append(t["id"])
    return _check("trips_energy_mismatch", "error" if bad else "ok", len(bad), plate, bad[:20])


def meter_overlap_check(db):
    """Wallbox readings whose meter span earlier readings already cover
    (shared meter, any vehicle): energy counted twice."""
    import fahrzeugmodell as fm
    ids = fm.covered_readings([dict(r) for r in db.execute(
        "SELECT id, meter_start, meter_end FROM charge_readings")])
    return _check("readings_meter_overlap", "error" if ids else "ok", len(ids), samples=ids[:20])


def capacity_check(plate, anchor, trend):
    if not trend or not trend.get("ok"):
        return _check("capacity_trend", "info", trend.get("n") if trend else 0, plate)
    cap = trend.get("cap_now")
    if anchor and cap and abs(cap - anchor) / anchor > 0.10:
        return _check("capacity_deviation", "warn", f"{cap} / {anchor} kWh", plate)
    return _check("capacity_trend", "ok", f"{cap} kWh · n={trend.get('n')}", plate)


QUEUE_DEPTH_WARN = 8


def _queue_depth(r):
    """Depth from waitress' "Task queue depth is N", else None."""
    if r["logger"] != "waitress.queue":
        return None
    try:
        return int(r["msg"].rsplit(" ", 1)[-1])
    except ValueError:
        return None


def log_check():
    """Errors / warnings in the log. Waitress reports every request that had
    to wait for a free worker ("Task queue depth is N"); a short queue while
    a map loads its tiles is normal and no finding — only from
    QUEUE_DEPTH_WARN on (workers really exhausted)."""
    err = sum(1 for r in LOG_RING if r["level"] in ("ERROR", "CRITICAL"))
    warn = sum(1 for r in LOG_RING if r["level"] == "WARNING"
               and (_queue_depth(r) is None or _queue_depth(r) >= QUEUE_DEPTH_WARN))
    level = "error" if err else "warn" if warn else "ok"
    return _check("log", level, f"{err} / {warn}")


def detector_tz():
    import detector
    return detector.LOCAL_TZ


def influx_checks(client, bucket, org, devices):
    """Last telemetry point and points in 24 h per device."""
    out = []
    if client is None:
        return [_check("influx", "info", "not configured")]
    try:
        # Vehicle data (SoC) and the stick's own battery (bd) only flow while
        # the car is awake; a car that was not driven is no finding.
        q = f'''
        from(bucket: "{bucket}")
          |> range(start: -30d)
          |> filter(fn: (r) => r._measurement == "v" and (r._field == "s" or r._field == "bd"))
          |> group(columns: ["d", "_field"])
          |> last()
        '''
        last = {}
        for t in client.query_api().query(q, org=org):
            for rec in t.records:
                last[(rec.values.get("d"), rec.get_field())] = rec.get_time()
        now = datetime.now(timezone.utc)
        for dev in devices:
            ts = last.get((dev, "s"))
            if ts is None:
                out.append(_check("telemetry_age", "warn", None, dev))
            else:
                age_h = (now - ts).total_seconds() / 3600
                out.append(_check("telemetry_age", "ok" if age_h < 48 else "info", round(age_h, 1), dev,
                                  detail=ts.astimezone(detector_tz()).strftime("%Y-%m-%d %H:%M")))
            # The stick sleeps with the parked car too (real data: 206 pauses
            # over 6 h in 7 months, up to 5 days) — its age is information.
            bd = last.get((dev, "bd"))
            if bd is not None:
                age_h = (now - bd).total_seconds() / 3600
                level = "ok" if age_h < 48 else "info"
                out.append(_check("stick_age", level, round(age_h, 1), dev,
                                  detail=bd.astimezone(detector_tz()).strftime("%Y-%m-%d %H:%M")))
    except Exception as e:
        out.append(_check("influx", "error", str(e)[:200]))
    return out


# ── Telemetry export ───────────────────────────────────────────────────────

def telemetry_csv(client, bucket, org, device, since_utc, until_utc, window_s, anon=False):
    """All fields of one device, one row per time (window_s 0 = raw)."""
    agg = f"|> aggregateWindow(every: {int(window_s)}s, fn: last, createEmpty: false)" if window_s else ""
    q = f'''
    from(bucket: "{bucket}")
      |> range(start: {since_utc.strftime("%Y-%m-%dT%H:%M:%SZ")}, stop: {until_utc.strftime("%Y-%m-%dT%H:%M:%SZ")})
      |> filter(fn: (r) => r._measurement == "v" and r.d == "{device}")
      {agg}
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
      |> sort(columns: ["_time"])
    '''
    rows, fields = [], set()
    for t in client.query_api().query(q, org=org):
        for rec in t.records:
            r = {k: v for k, v in rec.values.items()
                 if not k.startswith("_") and k not in ("result", "table", "d")}
            if anon:
                for k in ("la", "lo"):
                    if isinstance(r.get(k), (int, float)):
                        r[k] = round(r[k], 2)
            r["time"] = rec.get_time().strftime("%Y-%m-%dT%H:%M:%SZ")
            fields.update(r)
            rows.append(r)
    rows.sort(key=lambda r: r["time"])
    cols = ["time"] + sorted(f for f in fields if f != "time")
    return rows_csv(rows, cols), len(rows)


# Long-term telemetry: hourly means over the whole history, the input of
# the vehicle model (BMS capacity, energy counter, temperatures, speed)
# without the size of the raw data. No positions.
LONGTERM_FIELDS = ("s", "od", "v", "p", "et", "bt", "ca", "kw", "al", "r", "c", "dc")
LONGTERM_START = "2019-01-01T00:00:00Z"


def telemetry_hourly_csv(client, bucket, org, device):
    fields = " or ".join(f'r._field == "{f}"' for f in LONGTERM_FIELDS)
    q = f'''
    from(bucket: "{bucket}")
      |> range(start: {LONGTERM_START})
      |> filter(fn: (r) => r._measurement == "v" and r.d == "{device}")
      |> filter(fn: (r) => {fields})
      |> aggregateWindow(every: 1h, fn: mean, createEmpty: false, timeSrc: "_start")
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''
    rows = []
    for t in client.query_api().query(q, org=org):
        for rec in t.records:
            r = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in rec.values.items()
                 if k in LONGTERM_FIELDS}
            r["hour"] = rec.get_time().strftime("%Y-%m-%dT%H:00Z")
            rows.append(r)
    rows.sort(key=lambda r: r["hour"])
    return rows_csv(rows, ["hour"] + list(LONGTERM_FIELDS)), len(rows)


# ── ZIP ────────────────────────────────────────────────────────────────────

class ZipOut:
    """ZIP in memory, or straight into a file (path) for large exports.
    progress(name) is told each part as it starts."""
    def __init__(self, path=None, progress=None):
        self.path = path
        self.buf = None if path else io.BytesIO()
        self.zf = zipfile.ZipFile(path or self.buf, "w", zipfile.ZIP_DEFLATED)
        self.files = []
        self.errors = []
        self.progress = progress or (lambda name: None)

    def text(self, name, content, rows=None):
        self.zf.writestr(name, content)
        self.files.append({"file": name, "rows": rows, "bytes": len(content.encode("utf-8"))})

    def json(self, name, obj):
        self.text(name, json.dumps(obj, ensure_ascii=False, indent=1, default=str))

    def safe(self, name, fn):
        """Run fn() → content; an error lands in errors.json, not in a 500."""
        self.progress(name)
        try:
            return fn()
        except Exception as e:  # diagnostics must never fail as a whole
            logging.getLogger("triplog.diagnose").warning("diagnose: %s failed: %s", name, e)
            self.errors.append({"part": name, "error": f"{type(e).__name__}: {e}"})
            return None

    def close(self):
        if self.errors:
            self.json("errors.json", self.errors)
        self.zf.close()
        return self.path if self.path else self.buf.getvalue()


def environment():
    return {
        "version": config.VERSION,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "uptime_h": round((time.time() - _started) / 3600, 1),
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


README = """IDmate diagnostics export
========================

manifest.json      version, environment, range, file list, migration level
checks.json        health checks at export time (level ok/info/warn/error)
config.json        config.py parameters (secrets only as [set]/[empty])
settings.json      settings table (encrypted and secret values redacted)
model.json         parameters the calculations use right now: battery
                   capacity anchor and trend per vehicle, charge efficiency,
                   detector thresholds, plausibility limits
data/*.csv         SQLite tables; charge_readings (unless "all readings"),
                   charges, gpx_waypoints only for the range, everything
                   else complete
calc/              derived figures as the app computes them today:
  trips_derived.csv        per trip: km, SoC delta, energy, stored and
                           recomputed consumption, capacity used, checks
  capacity_<plate>.csv     every capacity data point of the trend
  real_consumption_<plate>.json   energy balance between charges
  gaps_<plate>.json        odometer gaps with context
  api/*.json               snapshot of the analysis endpoints (active vehicle)
telemetry/<device>.csv     Influx measurement "v", one row per time window;
                           field map in manifest.json ("influx_fields")
telemetry_hourly/<device>.csv   whole history, hourly means of SoC, odometer,
                           speed, power, temperatures, BMS capacity (ca),
                           energy counter (kw), charging flags; no positions
logs/recent.jsonl          last log records of the running process

Not included: users, passkeys, guest links and devices, personal notes,
encrypted settings, secrets.
"""
