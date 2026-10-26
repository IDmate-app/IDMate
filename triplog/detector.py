"""Trip detection — reads InfluxDB, detects trips + charging sessions, writes to SQLite."""

import time
import logging
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config

LOCAL_TZ = config.LOCAL_TZ    # TIMEZONE / TZ, default Europe/Berlin

def _local_iso(dt):
    """UTC datetime → local time ISO string without offset suffix."""
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%dT%H:%M:%S")

from influxdb_client import InfluxDBClient

import math

import config
import fahrzeugmodell as fm
import journey_sync

log = logging.getLogger("triplog.detector")

# Serialize scans: four unsynchronized triggers (MQTT-silence timer, admin
# rescan, import job, 10-min loop) can run run_once() concurrently. save_trips/
# save_charges dedup is SELECT-then-INSERT, so two parallel scans over the same
# window both see "no overlap" and insert the same trip/charge twice. Only one
# scan may run at a time; a second trigger skips instead of duplicating.
_scan_lock = threading.Lock()


def _round_nonneg(v, ndigits):
    """Round v to ndigits, clamp negative to 0, pass None through unchanged.

    Replaces the `round(x, n) if x else None` falsy-pattern that silently
    converted real zeros (battery empty, regen exactly cancelling) to None.
    Rule: real measurement (incl. 0) → store as-is; <0 → 0; no measurement → None.
    """
    if v is None:
        return None
    if v < 0:
        return 0.0
    return round(v, ndigits)


def sanitize_soc(v):
    """Repair SoC values corrupted by firmware u16-underflow, then clamp to [0, 100].

    Bug: the telemetry stick packed SoC as `(uint16_t)(soc * 10)`. Negative BMS
    readings (the BMS occasionally reports -1..-5 % near empty) wrapped to
    65486..65535 raw → ~6548..6553 % after the server's /10 division.

    Recovery: any value > 110 % is treated as a wraparound — reinterpret the
    raw u16 as signed int16 to recover the original negative reading, then
    clamp to [0, 100] (sub-zero SoC is physically impossible; the BMS error
    is unknown so we conservatively clip to 0).

    Returns None for None / non-numeric input. Returns a float in [0, 100] otherwise.
    """
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    if v > 110.0:
        raw_u16 = int(round(v * 10.0)) & 0xFFFF
        signed = raw_u16 - 0x10000 if raw_u16 >= 0x8000 else raw_u16
        v = signed / 10.0
    if v < 0.0:
        return 0.0
    if v > 100.0:
        return 100.0
    return v


def haversine_m(lat1, lon1, lat2, lon2):
    """Distance in meters between two GPS coordinates."""
    R = 6371000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


# Bit index → field name (must match ESP32 firmware)
FIELDS = {
    0:  's',   # SOC
    1:  'u',   # Voltage
    2:  'i',   # Current
    3:  'p',   # Power
    4:  'v',   # Speed
    5:  'c',   # IsCharging
    6:  'dc',  # IsDCFC
    7:  'bt',  # BattTemp
    8:  'et',  # ExtTemp
    9:  'r',   # Range
    10: 'la',  # Latitude
    11: 'lo',  # Longitude
    12: 'hd',  # Heading
    15: 'ls',  # LTESignal
    17: 'ca',  # Capacity
    18: 'kw',  # kWhCharged
    19: 'pk',  # IsParked
    20: 'od',  # Odometer
    21: 'bd',  # BattDevice
    22: 'ig',  # Ignition (0=off, 1=on)
}


def expand_rows(rows):
    """Resolves delta compression: copy _eq fields from predecessor, _na fields → None."""
    expanded = []
    prev = {}
    for row in rows:
        eq_mask = int(row.get('_eq') or 0)
        na_mask = int(row.get('_na') or 0)

        result = {k: v for k, v in row.items() if k not in ('_eq', '_na')}

        for bit, key in FIELDS.items():
            if key in row and row[key] is not None:
                continue  # Value explicitly present
            if eq_mask & (1 << bit):
                result[key] = prev.get(key)
            elif na_mask & (1 << bit):
                result[key] = None

        prev = result
        expanded.append(result)
    return expanded


def get_db():
    """Open a DB connection. Delegates to app.get_db so that schema init,
    migrations and PRAGMAs live in exactly one place. Lazy-import avoids the
    circular load at module init (app imports detector before its own get_db
    is defined). At call time app is always fully loaded."""
    from app import get_db as _app_get_db
    return _app_get_db()


def get_influx(timeout_ms=None):
    """InfluxDB client. timeout_ms bounds each request (default ~10 s) — pass a
    short value for paths that must never hang (e.g. the charge rebuild's SoC
    refinement, which otherwise blocks the SQLite write lock 'forever' when
    Influx is slow/unreachable)."""
    if not config.INFLUX_TOKEN:
        return None
    kwargs = dict(url=config.INFLUX_URL, token=config.INFLUX_TOKEN, org=config.INFLUX_ORG)
    if timeout_ms is not None:
        kwargs["timeout"] = timeout_ms
    return InfluxDBClient(**kwargs)


def last_trip_end(db, device):
    """Timestamp of the last detected trip end, at least 24h back."""
    row = db.execute(
        "SELECT end_time FROM trips WHERE device = ? ORDER BY end_time DESC LIMIT 1",
        (device,),
    ).fetchone()
    min_lookback = datetime.now(timezone.utc) - timedelta(hours=24)
    if row:
        ts = row["end_time"].strip().replace("Z", "+00:00")
        last = datetime.fromisoformat(ts)
        if last.tzinfo is None:
            # Stored times are local time (Europe/Berlin) without offset
            last = last.replace(tzinfo=LOCAL_TZ)
        last_utc = last.astimezone(timezone.utc)
        # Always look back at least 24h to close gaps
        return min(last_utc, min_lookback)
    return datetime.now(timezone.utc) - timedelta(days=7)


def _merge_rows(rows):
    """Merges rows with the same _time into a single row.
    InfluxDB pivot produces multiple tables with different fields —
    without merging, data gets incorrectly interleaved."""
    if not rows:
        return rows
    merged = []
    current = dict(rows[0])
    for row in rows[1:]:
        if row.get("_time") == current.get("_time"):
            # Same timestamp: merge fields (prefer existing values)
            for k, v in row.items():
                if v is not None and (k not in current or current[k] is None):
                    current[k] = v
        else:
            merged.append(current)
            current = dict(row)
    merged.append(current)
    return merged


def query_drive_data(client, since, device, until=None):
    """Fetches all vehicle fields incl. _eq/_na bitmasks from `since` to `until`."""
    # Flux requires RFC3339 with Z (no +00:00)
    ts = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stop_clause = f", stop: {until.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}" if until else ""
    query = f'''
    from(bucket: "{config.INFLUX_BUCKET}")
      |> range(start: {ts}{stop_clause})
      |> filter(fn: (r) => r._measurement == "v" and r.d == "{device}")
      |> filter(fn: (r) => r._field == "v" or r._field == "s" or r._field == "p" or r._field == "od" or r._field == "la" or r._field == "lo" or r._field == "pk" or r._field == "c" or r._field == "dc" or r._field == "ca" or r._field == "ig" or r._field == "kw" or r._field == "_eq" or r._field == "_na")
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
      |> sort(columns: ["_time"])
    '''
    tables = client.query_api().query(query, org=config.INFLUX_ORG)
    rows = []
    for table in tables:
        for record in table.records:
            rows.append(record.values)
    # Pivot produces multiple tables with different fields —
    # merge rows with the same _time into one row
    rows.sort(key=lambda r: r.get("_time") or datetime.min.replace(tzinfo=timezone.utc))
    return _merge_rows(rows)


def _driven(points, trip):
    """Vehicle data proves the car drove: a positive speed sample or an
    odometer increase. Every source sends speed while driving (tracker: CAN,
    Home Assistant: required entity). GPS progress alone means the tracker was
    awake while the car was off and carried — ferry, car train, tow truck."""
    if any((p.get("v") or 0) > 0 for p in points):
        return True
    return (trip["odo_start"] is not None and trip["odo_end"] is not None
            and trip["odo_end"] > trip["odo_start"])


def _save_trip(trips, trip_start, trip_points, bat_kwh, device):
    """Helper: builds trip and appends if valid."""
    if not trip_points:
        return
    trip = build_trip(trip_start, trip_points[-1], trip_points, bat_kwh, device=device)
    if not trip or (trip["distance_km"] or 0) < config.TRIP_MIN_DISTANCE_KM:
        return
    if not _driven(trip_points, trip):
        log.info("GPS-only movement %s – %s (%.1f km) without speed or odometer skipped "
                 "(car carried: ferry, car train, tow truck)",
                 trip["start_time"], trip["end_time"], trip["distance_km"] or 0)
        return
    trips.append(trip)


# Charge inside a trip: SoC up SOC_JUMP_MIN over the low of this window,
# speed since the low missing or median below CHARGE_STILL_KMH.
CHARGE_RISE_WINDOW_MIN = 30
CHARGE_STILL_KMH = 5.0


def detect_trips(rows, bat_kwh=fm.DEFAULT_BAT_KWH, device=None):
    """Detects trips from pivoted InfluxDB rows.

    Purely speed-based detection (ig is ignored):

    - Trip starts when v > 0
    - Trip ends on:
      - Data gap >= config.DATA_GAP_MINUTES (5 min)
      - v == 0 for >= config.TRIP_STOP_MINUTES (5 min)
      - SoC jump >= 5% upward while avg speed < 1 km/h in between (charging)
      - SoC up >= 5% over the low of the last 30 min, no driving since (charge
        inside the trip; cut at the low)
    - Open trip: save if last point is older than config.DATA_GAP_MINUTES
    """
    # Ensure rows are sorted chronologically
    rows = sorted(rows, key=lambda r: r.get("_time") or datetime.min.replace(tzinfo=timezone.utc))

    trips = []
    in_trip = False
    trip_start = None
    trip_points = []
    last_time = None
    last_moving_time = None   # last timestamp with v > 0
    _last_soc = None
    _soc_low = None            # (soc, time, index in trip_points) — lowest SoC of the window
    _v_since_low = []
    _after_charge = False      # cut at a charge, car not driving again yet
    _v_sum_since_soc = 0.0     # speed samples since last SoC reading (avg-speed standstill check)
    _v_cnt_since_soc = 0
    _prev_lat = None
    _prev_lon = None

    for row in rows:
        t = row.get("_time")
        if t is None:
            continue

        v = row.get("v")
        soc = sanitize_soc(row.get("s"))
        gap = (t - last_time).total_seconds() / 60 if last_time and t else 0

        # Detect movement: v > 0 OR GPS progress as fallback when v is missing
        la = row.get("la")
        lo = row.get("lo")
        if v is not None:
            moving = v > 0
        elif la and lo and _prev_lat and _prev_lon:
            dt_sec = (t - last_time).total_seconds() if last_time and t else 0
            gps_dist = haversine_m(_prev_lat, _prev_lon, la, lo)
            # > 50m in one interval = clearly moving (any pace: in a traffic
            # jam the car sends no speed for minutes and crawls at 2-5 km/h)
            moving = gps_dist > 50 and dt_sec > 0
        else:
            moving = False
        if la and lo:
            _prev_lat = la
            _prev_lon = lo

        # Accumulate speed samples since the last SoC reading, so the SoC jump
        # is judged against the AVERAGE speed in between (not a strict v==0).
        if v is not None:
            _v_sum_since_soc += v
            _v_cnt_since_soc += 1

        # Detect SoC jump: SoC rose >= config.SOC_JUMP_MIN while the car was, on average,
        # standing still (avg speed < config.SOC_JUMP_STILL_KMH) → charging happened, so
        # split the trip here. Average instead of strict v==0 because some sources
        # never report exactly 0 (GPS/sensor jitter, sparse TM speed) — otherwise
        # the trip runs straight through the charge and is never split.
        soc_jump = False
        if soc is not None and _last_soc is not None and soc - _last_soc >= config.SOC_JUMP_MIN:
            avg_v = (_v_sum_since_soc / _v_cnt_since_soc) if _v_cnt_since_soc else None
            if avg_v is not None and avg_v < config.SOC_JUMP_STILL_KMH:
                soc_jump = True
        if soc is not None:
            _last_soc = soc
            _v_sum_since_soc = 0.0
            _v_cnt_since_soc = 0

        # Charging inside a running trip, from the SoC alone: the telemetry
        # has no reliable "parked" signal (speed missing or jittering while
        # the car sleeps at a charger). Driving never adds SOC_JUMP_MIN %
        # within CHARGE_RISE_WINDOW_MIN (recuperation: 1-2 %, and only at
        # speed), so a rise of that size over the lowest SoC of the window,
        # with no speed samples since that low or a median below
        # CHARGE_STILL_KMH, is a charge — also when the SoC climbs in 1 %
        # steps (10 % in 5 min at a fast charger). The trip ends at the low.
        charge_cut = None
        new_low = False
        if in_trip and soc is not None:
            if _soc_low is None or soc <= _soc_low[0] or (t - _soc_low[1]).total_seconds() > CHARGE_RISE_WINDOW_MIN * 60:
                _soc_low = (soc, t, len(trip_points))
                _v_since_low = []
                new_low = True
            elif soc - _soc_low[0] >= config.SOC_JUMP_MIN:
                # speed AFTER the low only — and not this row's (it may
                # already be driving off with the charged SoC)
                vs = sorted(_v_since_low)
                med = vs[len(vs) // 2] if vs else None
                if med is None or med < CHARGE_STILL_KMH:
                    charge_cut = _soc_low[2]
        if v is not None and _soc_low is not None and not new_low:
            _v_since_low.append(v)

        # How long has the car been stationary?
        standstill_min = 0
        if not moving and last_moving_time and t:
            standstill_min = (t - last_moving_time).total_seconds() / 60

        # Right after a charge cut the car is still at the charger: speed
        # jitter (1-2 km/h) must not start the next trip before it drives.
        if _after_charge and moving and v is not None and v < CHARGE_STILL_KMH:
            moving = False
        if not in_trip:
            if moving:
                _after_charge = False
                in_trip = True
                trip_start = row
                trip_points = [row]
                last_moving_time = t
                _soc_low = (soc, t, 0) if soc is not None else None
                _v_since_low = []
        elif charge_cut is not None:
            # Trip ends where the charge began (lowest SoC); the charging
            # rows belong to no trip. Driving off starts the next one.
            _save_trip(trips, trip_start, trip_points[:charge_cut + 1], bat_kwh, device)
            _soc_low = None
            _v_since_low = []
            if moving and (v is None or v >= CHARGE_STILL_KMH):
                # this row already drives off with the charged SoC
                trip_start = row
                trip_points = [row]
                last_moving_time = t
                _soc_low = (soc, t, 0)
            else:
                _after_charge = True
                in_trip = False
                trip_points = []
                trip_start = None
        else:
            end_trip = False

            if gap >= config.DATA_GAP_MINUTES:
                end_trip = True
            elif standstill_min >= config.TRIP_STOP_MINUTES:
                end_trip = True
            elif soc_jump:
                end_trip = True

            if end_trip:
                _save_trip(trips, trip_start, trip_points, bat_kwh, device)
                in_trip = False
                trip_points = []
                trip_start = None
                if moving:
                    in_trip = True
                    trip_start = row
                    trip_points = [row]
                    last_moving_time = t
                    _soc_low = (soc, t, 0) if soc is not None else None
                    _v_since_low = []
            else:
                trip_points.append(row)
                if moving:
                    last_moving_time = t

        last_time = t

    # Open trip at end (still in_trip = end_trip never triggered)
    if in_trip and trip_points and last_time is not None:
        age_min = (datetime.now(timezone.utc) - last_time).total_seconds() / 60
        if age_min >= config.DATA_GAP_MINUTES:
            _save_trip(trips, trip_start, trip_points, bat_kwh, device)
        else:
            log.debug("Open trip still active (last point %.1f min ago), skipping", age_min)

    # Only return the last completed trip when it is definitely over:
    # Only when the last data point is >= config.DATA_GAP_MINUTES ago,
    # we can be sure no more data is coming.
    if trips and last_time is not None:
        age_min = (datetime.now(timezone.utc) - last_time).total_seconds() / 60
        if age_min < config.DATA_GAP_MINUTES:
            removed = trips.pop()
            log.debug("Last trip (%s) withheld — data still too fresh (%.1f min)",
                       removed.get("end_time"), age_min)

    return trips


def _filter_gps_jumps(points):
    """Filters GPS jumps: removes points with implied speed > config.GPS_MAX_KMH."""
    filtered = []
    for pt in points:
        la, lo, t = pt.get("la"), pt.get("lo"), pt.get("_time")
        if la is None or lo is None or t is None:
            filtered.append(pt)
            continue
        if not filtered:
            filtered.append(pt)
            continue
        # Find last point with valid GPS data
        prev = None
        for p in reversed(filtered):
            if p.get("la") is not None and p.get("lo") is not None and p.get("_time") is not None:
                prev = p
                break
        if prev is None:
            filtered.append(pt)
            continue
        dist_m = haversine_m(prev["la"], prev["lo"], la, lo)
        dt_h = (t - prev["_time"]).total_seconds() / 3600
        if dt_h > 0 and dist_m / 1000 / dt_h > config.GPS_MAX_KMH:
            continue  # GPS jump → discard point
        filtered.append(pt)
    return filtered


def build_trip(start_row, end_row, points, bat_kwh=fm.DEFAULT_BAT_KWH, device=None):
    """Builds a trip dict from start/end/intermediate points."""
    # Filter GPS jumps
    points = _filter_gps_jumps(points)
    if not points:
        return None

    # Start/end from filtered GPS points
    gps_start = next((p for p in points if p.get("la") and p.get("lo")), start_row)
    gps_end = next((p for p in reversed(points) if p.get("la") and p.get("lo")), end_row)

    # Odometer: find first and last valid value from all points
    od_start = None
    od_end = None
    for pt in points:
        v = pt.get("od")
        if v is not None:
            if od_start is None:
                od_start = v
            od_end = v
    distance = (od_end - od_start) if od_start is not None and od_end is not None else None

    # GPS distance as fallback when no odometer available
    if distance is None:
        gps_pts = [(pt.get("la"), pt.get("lo")) for pt in points if pt.get("la") and pt.get("lo")]
        if len(gps_pts) >= 2:
            dist_gps = sum(
                haversine_m(gps_pts[i][0], gps_pts[i][1], gps_pts[i+1][0], gps_pts[i+1][1])
                for i in range(len(gps_pts) - 1)
            )
            distance = round(dist_gps / 1000, 2)

    # SoC: first/last non-None value from all points (delta-compressed data)
    soc_start = None
    soc_end = None
    for pt in points:
        s = sanitize_soc(pt.get("s"))
        if s is not None:
            if soc_start is None:
                soc_start = s
            soc_end = s

    # kWh counter (cumulative): first/last valid value
    kw_start = None
    kw_end = None
    for pt in points:
        k = pt.get("kw")
        if k is not None:
            if kw_start is None:
                kw_start = k
            kw_end = k

    # Battery capacity: the own one at trip time (bat_kwh is a float OR a
    # callable(start_time)->float, the scan passes the capacity trend), the
    # same value the recalculation uses. The BMS estimate (Influx "ca") only
    # when there is none: it is a comparison, not the reference.
    cap = bat_kwh(start_row.get("_time")) if callable(bat_kwh) else bat_kwh
    if not cap:
        for pt in reversed(points):
            ca = pt.get("ca")
            if ca and ca > 0:
                cap = ca
                break

    # Energy from SoC difference × battery capacity (ca from InfluxDB)
    # kw_start/kw_end are only stored for charge-to-charge analysis
    energy = None
    consumption = None
    if soc_start is not None and soc_end is not None and soc_start > soc_end:
        energy = round((soc_start - soc_end) / 100 * cap, 2)
        consumption = fm.trip_consumption(energy, distance)

    return {
        "device": device or config.INFLUX_DEVICE,
        "start_time": _local_iso(start_row["_time"]),
        "end_time": _local_iso(end_row["_time"]),
        "start_lat": gps_start.get("la"),
        "start_lon": gps_start.get("lo"),
        "end_lat": gps_end.get("la"),
        "end_lon": gps_end.get("lo"),
        "odo_start": round(od_start, 1) if od_start is not None else None,
        "odo_end": round(od_end, 1) if od_end is not None else None,
        "distance_km": round(distance, 1) if distance is not None else None,
        "soc_start": round(soc_start, 1) if soc_start is not None else None,
        "soc_end": _round_nonneg(soc_end, 1),
        "energy_kwh": _round_nonneg(energy, 2),
        "consumption": _round_nonneg(consumption, 1),
        "kw_start": round(kw_start, 2) if kw_start is not None else None,
        "kw_end": round(kw_end, 2) if kw_end is not None else None,
        # not a column: capacity used, so save_trips can recompute the energy
        # when it extends an existing trip with this one
        "cap_kwh": cap,
    }


def _span_energy(soc_start, soc_end, cap, distance):
    """(energy, consumption) over a whole trip from its SoC span — used when
    a trip is extended, so the energy covers the full trip and not only the
    newly scanned piece."""
    if soc_start is None or soc_end is None or not cap or soc_start <= soc_end:
        return None, None
    energy = round((soc_start - soc_end) / 100 * cap, 2)
    cons = fm.trip_consumption(energy, distance)
    return energy, cons


def detect_charges(rows, device=None):
    """Detects charging sessions from pivoted rows."""
    charges = []
    charging = False
    charge_start = None
    charge_points = []

    for row in rows:
        c = row.get("c", 0) or 0
        dc = row.get("dc", 0) or 0
        is_charging = c == 1 or dc == 1

        if not charging and is_charging:
            charging = True
            charge_start = row
            charge_points = [row]
        elif charging and is_charging:
            charge_points.append(row)
        elif charging and not is_charging:
            charge = build_charge(charge_start, row, charge_points, device=device)
            if charge:
                charges.append(charge)
            charging = False
            charge_points = []

    return charges


def build_charge(start_row, end_row, points, device=None):
    """Builds a charge dict."""
    # Sanitize SoC at the source — raw start/end_row carry the u16-underflow
    # garbage from the firmware (negative BMS reading wraps to ~6553%). Without
    # this, every rebuild rewrites the bad value back into charge_sessions even
    # after the underflow-repair migration cleaned the table once.
    soc_start = sanitize_soc(start_row.get("s"))
    soc_end = sanitize_soc(end_row.get("s"))

    charge_type = "dc" if any((pt.get("dc", 0) or 0) == 1 for pt in points) else "ac"

    max_power = 0.0
    energy = 0.0
    # Integrate energy over the real time between samples instead of assuming a
    # fixed 60-s cadence: 10-s telegrams used to overstate kWh ~6×, 5-min gaps
    # understated it. Cap each interval at DATA_GAP_MINUTES so a telemetry gap
    # (no readings) does not inflate the integral. The first point has no prev_t
    # and therefore contributes nothing.
    gap_cap_h = config.DATA_GAP_MINUTES / 60.0
    prev_t = None
    for pt in points:
        p = abs(pt.get("p", 0) or 0)
        if p > max_power:
            max_power = p
        t = pt.get("_time")
        if prev_t is not None and t is not None:
            dt_h = (t - prev_t).total_seconds() / 3600
            if dt_h > gap_cap_h:
                dt_h = gap_cap_h
            if dt_h > 0:
                energy += p * dt_h
        if t is not None:
            prev_t = t

    return {
        "device": device or config.INFLUX_DEVICE,
        "start_time": _local_iso(start_row["_time"]),
        "end_time": _local_iso(end_row["_time"]),
        "lat": start_row.get("la"),
        "lon": start_row.get("lo"),
        "type": charge_type,
        "soc_start": _round_nonneg(soc_start, 1),
        "soc_end": _round_nonneg(soc_end, 1),
        "energy_kwh": _round_nonneg(energy, 2),
        "max_power_kw": round(max_power, 1),
    }


def match_location(db, lat, lon, locations=None):
    """Finds the nearest saved location within its radius.

    Pass `locations` (a pre-fetched list of location rows) to avoid one
    `SELECT * FROM locations` per call — used by save_trips, which matches up
    to 4× per trip in a loop. When None, the list is fetched from the DB.
    """
    matches = matching_locations(lat, lon, locations, db=db)
    return matches[0] if matches else None


def matching_locations(lat, lon, locations=None, db=None):
    """EVERY saved geofence containing (lat, lon), nearest centre first, with
    alias rows resolved to their parent (each parent at most once). Several
    geofences may overlap on purpose — one tags the destination, another the
    purpose, a third the reason — so callers merge over the whole list."""
    if not lat or not lon:
        return []
    if locations is None:
        locations = db.execute("SELECT * FROM locations").fetchall()
    hits = []
    for loc in locations:
        dist = haversine_m(lat, lon, loc["lat"], loc["lon"])
        if _contains(loc, lat, lon, dist):
            hits.append((dist, loc))
    hits.sort(key=lambda h: h[0])
    out, seen = [], set()
    for _, loc in hits:
        res = resolve_alias(loc, locations)
        if res["id"] in seen:
            continue
        seen.add(res["id"])
        out.append(res)
    return out


def _keeps_address(loc):
    keys = loc.keys() if hasattr(loc, "keys") else ()
    return bool(loc["keep_address"]) if "keep_address" in keys else False


def _point_in_polygon(lat, lon, coords):
    """Ray-casting: True if (lat,lon) is inside polygon coords=[[lat,lon],...]."""
    n = len(coords)
    inside = False
    j = n - 1
    for i in range(n):
        yi, xi = coords[i][0], coords[i][1]
        yj, xj = coords[j][0], coords[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _contains(loc, lat, lon, dist):
    """Does the geofence row contain the point? A polygon is tested as drawn
    (the stored centre/radius is only its label position) — a circle by radius.
    Same rule as the app's backfill and API matcher, so live detection and
    'apply to all trips' agree."""
    keys = loc.keys() if hasattr(loc, "keys") else ()
    if ("shape" in keys and loc["shape"] == "polygon"
            and "polygon_coords" in keys and loc["polygon_coords"]):
        try:
            import json
            coords = json.loads(loc["polygon_coords"])
        except (TypeError, ValueError):
            return False
        return len(coords) >= 3 and _point_in_polygon(lat, lon, coords)
    return dist <= (loc["radius_m"] or 200)


def match_address_location(db, lat, lon, locations=None):
    """The nearest containing geofence that NAMES the address (keep_address
    off) — a simple tagging geofence nearer to the point must not hide it."""
    for loc in matching_locations(lat, lon, locations, db=db):
        if not _keeps_address(loc):
            return loc
    return None


def rule_anchor(matches):
    """The location route rules are keyed on: the nearest address-naming
    place, else the nearest simple geofence."""
    for loc in matches:
        if not _keeps_address(loc):
            return loc
    return matches[0] if matches else None


DIR_ARRIVAL = 1     # the tag applies to trips ENDING in the geofence
DIR_DEPARTURE = 2   # the tag applies to trips STARTING in the geofence


def merged_defaults(matches, direction=DIR_ARRIVAL):
    """(purpose, destination, visit_reason) merged over all containing
    geofences, nearest first: each field takes the first non-empty value
    whose direction bits include ``direction``. The destination falls back to
    the name of the first address-naming place on arrival only — a simple
    tagging geofence never lends its name, and a departure never does."""
    purpose = destination = reason = ""
    for loc in matches:
        keys = loc.keys() if hasattr(loc, "keys") else ()
        g = lambda k: (loc[k] if k in keys else None) or ""
        d = lambda k: (loc[k] if (k in keys and loc[k] is not None) else DIR_ARRIVAL)
        if not purpose and d("purpose_dirs") & direction:
            purpose = g("default_purpose")
        if not destination and d("destination_dirs") & direction:
            destination = g("default_destination") or (
                "" if (_keeps_address(loc) or direction != DIR_ARRIVAL) else (loc["name"] or ""))
        if not reason and d("reason_dirs") & direction:
            reason = g("default_reason")
    return purpose, destination, reason


DEPARTURE_ANCHOR_M = 500


def departure_matches(db, device, start_time, start_lat, start_lon, locations):
    """Geofences a trip departs FROM: those containing its start point — or,
    when the first GPS fix has already drifted out of every fence (the car
    reports movement a few hundred metres down the road), those containing
    the END of the previous trip of the same device, provided that end lies
    within DEPARTURE_ANCHOR_M of this start: you leave from where you last
    arrived."""
    matches = matching_locations(start_lat, start_lon, locations)
    if matches or not device or not start_time or start_lat is None or start_lon is None:
        return matches
    prev = db.execute(
        """SELECT end_lat, end_lon FROM trips
           WHERE device = ? AND end_time IS NOT NULL AND end_lat IS NOT NULL
             AND datetime(end_time) <= datetime(?)
           ORDER BY datetime(end_time) DESC LIMIT 1""",
        (device, start_time),
    ).fetchone()
    if prev and haversine_m(start_lat, start_lon, prev["end_lat"], prev["end_lon"]) <= DEPARTURE_ANCHOR_M:
        return matching_locations(prev["end_lat"], prev["end_lon"], locations)
    return matches


def auto_tags(db, device, start_time, start_lat, start_lon, end_lat, end_lon, locations):
    """(purpose, destination, visit_reason, tagged) the detector derives for a
    trip geometry: arrival tags of the geofences at the end point, departure
    tags at the start point (with the previous-arrival anchor) for whatever
    is still empty, and an explicit route rule on top of both."""
    start_matches = departure_matches(db, device, start_time, start_lat, start_lon, locations)
    end_matches = matching_locations(end_lat, end_lon, locations)
    start_loc, end_loc = rule_anchor(start_matches), rule_anchor(end_matches)
    purp, dest, vr = trip_defaults(start_matches, end_matches)
    rule = match_route_rule(db, start_loc, end_loc)
    if rule:
        purp = rule["purpose"]
        dest = rule.get("destination") or dest
        vr = rule.get("visit_reason") or vr
        log.info("Route rule: %s -> %s = %s",
                 start_loc["name"] if start_loc else "*",
                 end_loc["name"] if end_loc else "*",
                 rule["purpose"])
    return purp, dest, vr, bool(rule or end_matches)


def _keep_or_auto(current, old_auto, new_auto):
    """A value the user typed stays; a value that is still the OLD automatic
    one (or empty) follows the new geometry."""
    if new_auto and (not current or current == old_auto):
        return new_auto
    return current


_TAG_COLS = ("purpose", "destination", "visit_reason")


def retag_trip(db, trip_id, locations, before):
    """A trip's start or end moved (extension / merge): re-derive the tags for
    the NEW geometry and apply them wherever the stored value is empty or was
    the automatic value of the OLD geometry — a manual edit is never touched.
    ``before`` is the trip row as it was before the geometry changed."""
    row = db.execute(
        "SELECT device, start_time, start_lat, start_lon, end_lat, end_lon, "
        "purpose, destination, visit_reason FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if not row:
        return
    old = auto_tags(db, before["device"], before["start_time"], before["start_lat"], before["start_lon"],
                    before["end_lat"], before["end_lon"], locations)[:3]
    new = auto_tags(db, row["device"], row["start_time"], row["start_lat"], row["start_lon"],
                    row["end_lat"], row["end_lon"], locations)[:3]
    sets = {}
    for col, o, n in zip(_TAG_COLS, old, new):
        v = _keep_or_auto(row[col] or "", o, n)
        if v != (row[col] or ""):
            sets[col] = v
    if sets:
        db.execute(f"UPDATE trips SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                   (*sets.values(), trip_id))
        log.info("Trip %d re-tagged after geometry change: %s", trip_id, sets)


def trip_defaults(start_matches, end_matches):
    """Arrival tags at the end point first, then departure tags at the start
    point — per field, so a departure purpose can join an arrival destination."""
    purp, dest, vr = merged_defaults(end_matches, DIR_ARRIVAL)
    dp, dd, dr = merged_defaults(start_matches, DIR_DEPARTURE)
    return purp or dp, dest or dd, vr or dr


def resolve_alias(loc, locations):
    """An alias geofence (alias_of set) stands for its parent location: name,
    route rules and trip defaults all live on the parent, the alias only
    widens where the parent is recognised. Returns the parent row when the
    match is an alias and the parent is in ``locations``; otherwise ``loc``."""
    if loc is None:
        return None
    keys = loc.keys() if hasattr(loc, "keys") else ()
    parent_id = loc["alias_of"] if "alias_of" in keys else None
    if not parent_id:
        return loc
    for cand in locations:
        if cand["id"] == parent_id:
            return cand
    return loc


def match_route_rule(db, start_loc, end_loc):
    """Finds a route rule for start and destination location.

    Matching priority:
    1. Exact rule (from + to)
    2. Wildcard rule (from=location, to=any OR from=any, to=location)
    """
    if not start_loc and not end_loc:
        return None
    start_id = start_loc["id"] if start_loc else None
    end_id = end_loc["id"] if end_loc else None

    # 1. Exact rule
    if start_id and end_id:
        row = db.execute(
            "SELECT * FROM route_rules WHERE from_location_id = ? AND to_location_id = ?",
            (start_id, end_id),
        ).fetchone()
        if row:
            return dict(row)

    # 2. Wildcard rules (NULL = any)
    row = db.execute(
        """SELECT * FROM route_rules
           WHERE (from_location_id = ? OR from_location_id IS NULL)
             AND (to_location_id = ? OR to_location_id IS NULL)
             AND (from_location_id IS NOT NULL OR to_location_id IS NOT NULL)
           ORDER BY
             (CASE WHEN from_location_id IS NOT NULL THEN 1 ELSE 0 END
              + CASE WHEN to_location_id IS NOT NULL THEN 1 ELSE 0 END) DESC
           LIMIT 1""",
        (start_id, end_id),
    ).fetchone()
    return dict(row) if row else None


def location_defaults(loc):
    """(purpose, destination, visit_reason) a trip ending at ``loc`` gets when
    no route rule matches: the location's own defaults, the visited party
    falling back to the location name. Tolerates rows from a DB that has not
    run the m049 migration yet (missing keys read as empty)."""
    keys = loc.keys() if hasattr(loc, "keys") else ()

    def _g(k):
        return loc[k] if k in keys else None

    return (_g("default_purpose") or "",
            _g("default_destination") or ("" if _keeps_address(loc) else (loc["name"] or "")),
            _g("default_reason") or "")


MERGE_LOOKBACK_HOURS = 2  # only re-examine trips this recent (perf cap)


def save_trips(db, trips):
    # Load saved locations once — match_location() would otherwise re-query
    # them on every call (up to 4× per trip). save_trips never writes the
    # locations table, so the snapshot stays valid for the whole loop.
    locations = db.execute("SELECT * FROM locations").fetchall()
    for t in trips:
        # Duplicate check: overlap with existing trip in same time window
        # (exact start_time match is not enough — timestamps may slightly differ)
        existing = db.execute(
            """SELECT id, start_time, end_time, odo_start, odo_end, soc_start, soc_end,
                      distance_km, is_manual, manual_fields
               FROM trips WHERE device = ?
               AND datetime(start_time) < datetime(?)
               AND datetime(end_time)   > datetime(?)""",
            (t["device"], t["end_time"], t["start_time"]),
        ).fetchone()
        if existing:
            # Start/end set by hand in the gap editor (or a trip filled in by
            # hand) is authoritative: the scan must not move that side again.
            locked = set((existing["manual_fields"] or "").split(","))
            if existing["is_manual"]:
                locked |= {"start", "end"}
            before = db.execute(
                "SELECT device, start_time, start_lat, start_lon, end_lat, end_lon, "
                "purpose, destination, visit_reason FROM trips WHERE id = ?", (existing["id"],)).fetchone()
            extended = False
            # Extend end if new data reaches further into the future
            if t["end_time"] > existing["end_time"] and "end" not in locked:
                # distance_km from t covers only t's own window. If the scan/import
                # window clipped the trip start, t's odo_start is later than the
                # existing trip's → writing t["distance_km"] would shrink the
                # stored distance below the real odo_end−odo_start span. Recompute
                # over the existing odo_start when both odometer values exist;
                # otherwise fall back to t's value (previous behaviour).
                end_dist = t.get("distance_km")
                if t.get("odo_end") is not None and existing["odo_start"] is not None:
                    end_dist = round(t["odo_end"] - existing["odo_start"], 1)
                # Energy over the whole trip: existing start SoC → new end SoC.
                # t's own energy covers only the newly scanned piece (a trip
                # extended at 10:35 kept 0.33 kWh for 82 km).
                end_energy, end_cons = _span_energy(
                    existing["soc_start"], t.get("soc_end"), t.get("cap_kwh"), end_dist)
                if end_energy is None and existing["soc_start"] is None:
                    end_energy, end_cons = t.get("energy_kwh"), t.get("consumption")
                db.execute(
                    """UPDATE trips SET
                       end_time = :end_time, end_lat = :end_lat, end_lon = :end_lon,
                       odo_end = :odo_end, soc_end = :soc_end, energy_kwh = :energy_kwh,
                       consumption = :consumption, kw_end = :kw_end,
                       distance_km = :distance_km
                       WHERE id = :_id""",
                    {**t, "distance_km": end_dist, "energy_kwh": end_energy,
                     "consumption": end_cons, "_id": existing["id"]},
                )
                existing = {**dict(existing), "soc_end": t.get("soc_end"),
                            "odo_end": t.get("odo_end"), "distance_km": end_dist}
                log.info("Trip %d extended end to %s", existing["id"], t["end_time"])
                extended = True
            # Extend start if newly detected trip began earlier (reverse-day
            # import: the older day fills in the true start of an overnight trip).
            if t["start_time"] < existing["start_time"] and "start" not in locked:
                start_dist = t.get("distance_km")
                if t.get("odo_start") is not None and existing["odo_end"] is not None:
                    start_dist = round(existing["odo_end"] - t["odo_start"], 1)
                start_energy, start_cons = _span_energy(
                    t.get("soc_start"), existing["soc_end"], t.get("cap_kwh"), start_dist)
                db.execute(
                    """UPDATE trips SET
                       start_time = :start_time, start_lat = :start_lat,
                       start_lon = :start_lon, odo_start = :odo_start,
                       soc_start = :soc_start, kw_start = :kw_start,
                       distance_km = :distance_km,
                       energy_kwh = COALESCE(:energy_kwh, energy_kwh),
                       consumption = CASE WHEN :energy_kwh IS NULL THEN consumption ELSE :consumption END
                       WHERE id = :_id""",
                    {**t, "distance_km": start_dist, "energy_kwh": start_energy,
                     "consumption": start_cons, "_id": existing["id"]},
                )
                log.info("Trip %d extended start to %s", existing["id"], t["start_time"])
                extended = True
            if extended:
                # The end (or start) moved — e.g. first seen at an intermediate
                # stop, now at the real destination: tags follow the geometry.
                retag_trip(db, existing["id"], locations, before)
            else:
                log.debug("Trip overlaps with existing: %s %s", t["device"], t["start_time"])
            continue

        # Dead-zone merge: data dropouts (tunnel / dead-zone) make the detector
        # close one trip and open another a few minutes later. If the new trip
        # starts shortly after a recent trip ended, absorb it into that trip
        # instead of creating a duplicate. Bounded by MERGE_LOOKBACK_HOURS so
        # this never scans the whole history.
        cutoff = (datetime.now(LOCAL_TZ) - timedelta(hours=MERGE_LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")
        split = db.execute(
            """SELECT id, start_time, end_time, start_lat, start_lon,
                      odo_start, soc_start, soc_end, energy_kwh, distance_km,
                      purpose, destination, visit_reason
               FROM trips WHERE device = ?
                 AND end_time < ?
                 AND end_time > ?
                 AND (julianday(?) - julianday(end_time)) * 1440 <= ?
                 AND COALESCE(is_manual, 0) = 0
                 AND COALESCE(manual_fields, '') NOT LIKE '%end%'
               ORDER BY end_time DESC LIMIT 1""",
            (t["device"], t["start_time"], cutoff, t["start_time"], config.MERGE_GAP_MIN),
        ).fetchone()
        # A dead zone (tunnel) never raises the SoC: with a rise of
        # SOC_JUMP_MIN in between the car was charged — two trips, so the
        # charge between them can be found (detect_external_from_trips).
        if (split and split["soc_end"] is not None and t.get("soc_start") is not None
                and t["soc_start"] - split["soc_end"] >= config.SOC_JUMP_MIN):
            log.info("Trip %s not merged into %d: SoC %.0f → %.0f %% in between (charged)",
                     t["start_time"], split["id"], split["soc_end"], t["soc_start"])
            split = None
        if split:
            # Distance: prefer odo delta (covers the data-gap km too); fall back to sum
            if t.get("odo_end") is not None and split["odo_start"] is not None:
                merged_dist = round(t["odo_end"] - split["odo_start"], 1)
            else:
                merged_dist = round((split["distance_km"] or 0) + (t.get("distance_km") or 0), 1)
            # Energy: the SoC span covers the gap too; without both SoC values
            # sum what was actually recorded (energy during the gap is lost)
            merged_energy, new_cons = _span_energy(
                split["soc_start"], t.get("soc_end"), t.get("cap_kwh"), merged_dist)
            if merged_energy is None:
                e1, e2 = split["energy_kwh"], t.get("energy_kwh")
                merged_energy = round((e1 or 0) + (e2 or 0), 2) if (e1 is not None or e2 is not None) else None
                new_cons = round(merged_energy / merged_dist * 100, 1) if merged_energy and merged_dist and merged_dist >= 10 else None
            # Re-resolve destination/purpose for the new end location, but keep
            # whatever the user (or earlier auto-tagging) already wrote.
            split_row = db.execute(
                "SELECT device, start_time, start_lat, start_lon, end_lat, end_lon FROM trips WHERE id = ?",
                (split["id"],)).fetchone()
            old_auto = auto_tags(db, split_row["device"], split_row["start_time"], split_row["start_lat"],
                                 split_row["start_lon"], split_row["end_lat"], split_row["end_lon"], locations)[:3]
            auto_purp, auto_dest, auto_vr, _ = auto_tags(
                db, t["device"], split_row["start_time"], split_row["start_lat"], split_row["start_lon"],
                t.get("end_lat"), t.get("end_lon"), locations)
            purp = _keep_or_auto(split["purpose"] or "", old_auto[0], auto_purp)
            dest = _keep_or_auto(split["destination"] or "", old_auto[1], auto_dest)
            vr = _keep_or_auto(split["visit_reason"] or "", old_auto[2], auto_vr)
            db.execute(
                """UPDATE trips SET
                   end_time = :end_time, end_lat = :end_lat, end_lon = :end_lon,
                   odo_end = :odo_end, soc_end = :soc_end, kw_end = :kw_end,
                   distance_km = :distance_km, energy_kwh = :energy_kwh,
                   consumption = :consumption,
                   destination = :destination, purpose = :purpose,
                   visit_reason = :visit_reason
                   WHERE id = :_id""",
                {**t, "distance_km": merged_dist,
                 "energy_kwh": merged_energy, "consumption": new_cons,
                 "destination": dest, "purpose": purp, "visit_reason": vr,
                 "_id": split["id"]},
            )
            log.info("Trip %d dead-zone-merged: gap-end %s, resumed %s",
                     split["id"], split["end_time"], t["start_time"])
            continue

        # Distance: odo_end of previous trip → odo_end of this trip
        # More accurate than odo_start→odo_end, since odo_start often arrives delayed
        if t.get("odo_end") is not None:
            prev = db.execute(
                """SELECT odo_end FROM trips WHERE device = ?
                   AND odo_end IS NOT NULL
                   AND datetime(end_time) < datetime(?)
                   ORDER BY end_time DESC LIMIT 1""",
                (t["device"], t["start_time"]),
            ).fetchone()
            # ...unless the jump to this trip's first odometer is a telemetry
            # outage: then those km belong into a gap, not into this trip
            # (counting them here AND filling the gap doubled them).
            outage = (prev is not None and prev["odo_end"] is not None
                      and t.get("odo_start") is not None
                      and t["odo_start"] - prev["odo_end"] > config.TRIP_ODO_GAP_KM)
            if outage:
                log.info("Trip %s: odometer jump %.1f km before start — kept out "
                         "of the trip (telemetry gap)", t["start_time"],
                         t["odo_start"] - prev["odo_end"])
            if prev and prev["odo_end"] is not None and not outage:
                dist = t["odo_end"] - prev["odo_end"]
                if dist > 0:
                    t["distance_km"] = round(dist, 1)
                    # Recalculate consumption with corrected distance
                    t["consumption"] = fm.trip_consumption(t.get("energy_kwh"), dist) or t.get("consumption")

        # Arrival tags of the geofences at the end point, departure tags at
        # the start point (anchored on the previous arrival when the first GPS
        # fix already drifted out of the fence), route rule on top.
        purp, dest, vr, tagged = auto_tags(
            db, t["device"], t["start_time"], t.get("start_lat"), t.get("start_lon"),
            t.get("end_lat"), t.get("end_lon"), locations)
        if tagged or purp or dest or vr:
            t["purpose"], t["destination"], t["visit_reason"] = purp, dest, vr
        else:
            t.setdefault("purpose", "")
            t.setdefault("destination", "")
            t.setdefault("visit_reason", "")

        db.execute(
            """INSERT INTO trips
               (device, start_time, end_time, start_lat, start_lon, end_lat, end_lon,
                odo_start, odo_end, distance_km, soc_start, soc_end, energy_kwh, consumption,
                kw_start, kw_end, purpose, destination, visit_reason)
               VALUES (:device, :start_time, :end_time, :start_lat, :start_lon,
                       :end_lat, :end_lon, :odo_start, :odo_end,
                       :distance_km, :soc_start, :soc_end,
                       :energy_kwh, :consumption, :kw_start, :kw_end,
                       :purpose, :destination, :visit_reason)""",
            t,
        )
    db.commit()
    log.info("Saved: %d trips", len(trips))
    # A journey covering these days takes the fresh trips in right away —
    # nobody has to open the journey and tick them.
    journey_sync.sync_journeys_for_trips(db, trips)


def save_charges(db, charges):
    for c in charges:
        # Duplicate check: overlap with existing charge in same time window
        # (exact start_time match is not enough — a rescan window that clips a
        # charge yields a slightly different start_time → would insert a second
        # row. Mirror save_trips' overlap test instead.)
        if db.execute(
            """SELECT id FROM charges WHERE device = ?
               AND datetime(start_time) < datetime(?)
               AND datetime(end_time)   > datetime(?)""",
            (c["device"], c["end_time"], c["start_time"]),
        ).fetchone():
            continue
        db.execute(
            """INSERT INTO charges
               (device, start_time, end_time, lat, lon, type,
                soc_start, soc_end, energy_kwh, max_power_kw)
               VALUES (:device, :start_time, :end_time, :lat, :lon, :type,
                       :soc_start, :soc_end, :energy_kwh, :max_power_kw)""",
            c,
        )
    db.commit()
    log.info("Saved: %d charging sessions", len(charges))


def auto_categorize(db):
    """Automatically set uncategorized trips older than 7 days as private."""
    row = db.execute(
        "SELECT name FROM purpose_meta WHERE is_private = 1 ORDER BY sort_order LIMIT 1"
    ).fetchone()
    if not row:
        return
    priv_name = row["name"]
    result = db.execute(
        """UPDATE trips SET purpose = ?
           WHERE (purpose IS NULL OR purpose = '')
             AND start_time < datetime('now', '-7 days')""",
        (priv_name,),
    )
    if result.rowcount > 0:
        db.commit()
        log.info("Auto-categorized: %d trips as %s", result.rowcount, priv_name)


def cleanup_db(db):
    """Remove orphaned records and compact DB."""
    # Orphaned GPX waypoints (trip deleted)
    r1 = db.execute(
        "DELETE FROM gpx_waypoints WHERE trip_id NOT IN (SELECT id FROM trips)"
    )
    # Orphaned journey assignments
    r2 = db.execute(
        "DELETE FROM journey_trips WHERE trip_id NOT IN (SELECT id FROM trips)"
    )
    r3 = db.execute(
        "DELETE FROM journey_trips WHERE journey_id NOT IN (SELECT id FROM journeys)"
    )
    r5 = db.execute(
        "DELETE FROM journey_trip_excluded WHERE trip_id NOT IN (SELECT id FROM trips) "
        "OR journey_id NOT IN (SELECT id FROM journeys)"
    )
    # Orphaned charge readings (session gone) — UNLINK, never delete. Readings
    # are user-paid energy data; losing them is a billing problem. Once unlinked
    # they surface in the readings editor's "unassigned" view so they can
    # be reassigned (and the next rebuild picks them up).
    r4 = db.execute(
        "UPDATE charge_readings SET session_id = NULL WHERE session_id IS NOT NULL "
        "AND session_id NOT IN (SELECT id FROM charge_sessions)"
    )
    total = r1.rowcount + r2.rowcount + r3.rowcount + r4.rowcount + r5.rowcount
    if total > 0:
        db.commit()
        db.execute("VACUUM")
        log.info("DB cleanup: %d orphaned records removed", total)


def downsample_influx(client):
    """Downsample data points older than 7 days to max 60s resolution."""
    delete_api = client.delete_api()
    query_api = client.query_api()

    # Find duplicates: points that are < 60s after the previous one, older than 7 days
    flux = f'''
        import "experimental"

        from(bucket: "{config.INFLUX_BUCKET}")
          |> range(start: -365d, stop: -7d)
          |> filter(fn: (r) => r._measurement == "v")
          |> filter(fn: (r) => r._field == "s")
          |> elapsed(unit: 1s)
          |> filter(fn: (r) => r.elapsed < 60 and r.elapsed > 0)
          |> count()
          |> yield(name: "count")
    '''
    try:
        tables = query_api.query(flux)
        count = 0
        for table in tables:
            for record in table.records:
                count = record.get_value()
        if count and count > 0:
            log.info("InfluxDB: %d data points with <60s interval found (older than 7d)", count)
    except Exception:
        log.debug("InfluxDB downsampling: query not possible (possibly missing permissions)")


def run_once(scope_from=None, scope_to=None):
    """Single run: detect new trips + charging sessions for all vehicles.

    When ``scope_from`` / ``scope_to`` (UTC datetimes) are given, the Influx
    query is restricted to that window — used by the TeslaMate import job
    to re-detect freshly imported days without scanning all history."""
    # Non-blocking: if another scan already holds the lock, skip this trigger
    # rather than queue up a redundant (and duplicate-prone) parallel scan.
    if not _scan_lock.acquire(blocking=False):
        log.info("scan already running, skipping")
        return
    try:
        db = get_db()
        client = get_influx()
        # get_influx() returns None when INFLUX_TOKEN is empty — without a client
        # query_drive_data()/client.close() would raise AttributeError every cycle
        # and skip db.close() (connection leak per run). Bail out cleanly here.
        if client is None:
            log.warning("Influx not configured (empty INFLUX_TOKEN) — skipping scan")
            db.close()
            return

        try:
            _run_scan(db, client, scope_from, scope_to)
        finally:
            # Always release both connections, even if scanning raised.
            try:
                client.close()
            finally:
                db.close()
    finally:
        _scan_lock.release()


def _run_scan(db, client, scope_from, scope_to):
    # Determine all vehicles with device tag; fallback to config
    vehicle_rows = db.execute(
        "SELECT DISTINCT device FROM vehicles WHERE device IS NOT NULL AND device != ''"
    ).fetchall()
    devices = [r["device"] for r in vehicle_rows]
    if not devices:
        devices = [config.INFLUX_DEVICE]

    for device in devices:
        since = scope_from if scope_from is not None else last_trip_end(db, device)
        log.info("Device %s: searching for new trips since %s%s",
                 device, since.isoformat(),
                 f" until {scope_to.isoformat()}" if scope_to else "")

        rows = query_drive_data(client, since, device, until=scope_to)
        if not rows:
            log.info("Device %s: no new data", device)
            continue

        rows = expand_rows(rows)

        from app import capacity_at
        _cap_at = capacity_at(db, device)

        trips = detect_trips(rows, _cap_at, device=device)
        charges = detect_charges(rows, device=device)

        if trips:
            save_trips(db, trips)
        if charges:
            save_charges(db, charges)

    auto_categorize(db)
    cleanup_db(db)

    # Safety net for the journey auto-fill: every scan tops up ALL journeys,
    # so trips that reached the DB past save_trips (an importer, a manual
    # entry, a merge) also land in the journeys covering their days without
    # anyone opening the journey.
    try:
        journey_sync.repair_orphan_journey_devices(db)
        journey_sync.sync_journey_trips(db)
    except Exception:
        log.exception("Journeys: auto-fill failed")

    # Propagate freshly detected trips into recent charge sessions, so the
    # post-charge soc_end / odometer (the "charged to 80%" value) appears
    # without waiting for the next webhook reading or a manual rebuild. Scoped
    # to ~36h and bounded by the manual "settled until" freeze on the app side.
    try:
        from app import rebuild_charge_sessions
        since = (datetime.now(LOCAL_TZ) - timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%S")
        rebuild_charge_sessions(db, since=since)
    except Exception:
        log.exception("post-scan charge rebuild failed")

    try:
        from blueprints.charges import detect_external_from_trips
        n = detect_external_from_trips(db)
        if n:
            log.info("detect_external: %d new external charge(s) created", n)
    except Exception:
        log.exception("post-scan external-charge detection failed")


def run_loop():
    """Infinite loop — search for new trips every SCAN_INTERVAL_MINUTES.
    First scan only after SCAN_INTERVAL_MINUTES (no scan at server start)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    log.info("Triplog Detector started (interval: %d min, first scan in %d min)",
             config.SCAN_INTERVAL_MINUTES, config.SCAN_INTERVAL_MINUTES)

    while True:
        time.sleep(config.SCAN_INTERVAL_MINUTES * 60)
        try:
            run_once()
        except Exception:
            log.exception("Error in detector")


if __name__ == "__main__":
    run_loop()
