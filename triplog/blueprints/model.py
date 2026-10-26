"""Vehicle model per vehicle: battery swaps and the charging efficiency
(logic in fahrzeugmodell.py, capacity in app._capacity_trend)."""

import time
from datetime import date, datetime

from flask import Blueprint, jsonify, request

import config
import detector
import fahrzeugmodell as fm
from app import (_capacity_trend, _counter_trips, _eff_cache, _get_charge_efficiency,
                 _measured_efficiency, _recompute_all_consumption, _trend_cache, admin_required,
                 charge_efficiency_info, get_db, log)

model_bp = Blueprint("model", __name__)


def _vehicle(db, plate):
    return db.execute("SELECT id, plate, device, battery_capacity_kwh, charge_efficiency, "
                      "charge_efficiency_dc FROM vehicles WHERE plate = ?", (plate,)).fetchone()


def _refresh(db, v):
    """Capacity or efficiency changed: refit and recompute this vehicle's trips."""
    _trend_cache.pop(v["plate"], None)
    _eff_cache.pop(v["plate"], None)
    if v["device"]:
        n = _recompute_all_consumption(db, v["device"])
        log.info("vehicle model: %s changed, %d trips recomputed", v["plate"], n)


@model_bp.route("/api/vehicles/<plate>/model")
@admin_required
def vehicle_model(plate):
    db = get_db()
    try:
        v = _vehicle(db, plate)
        if not v:
            return jsonify({"error": "not found"}), 404
        tr = _capacity_trend(db, plate)
        return jsonify({
            "plate": plate,
            "source": tr.get("source"),
            "segments": tr.get("segments", []),
            "battery_change": tr.get("battery_change"),
            "soc_profile": tr.get("soc_profile"),
            "dc_offset": tr.get("dc_offset"),
            "bms": bms_compare(v["device"], tr.get("cap_now")),
            "buffer": buffer_view(v["device"]),
            "efficiency": _efficiency_view(db, plate),
        })
    finally:
        db.close()


@model_bp.route("/api/vehicles/<plate>/batteries", methods=["POST"])
@admin_required
def battery_add(plate):
    """Record a battery swap: {installed_at: 'YYYY-MM-DD', nominal_kwh, note}."""
    data = request.get_json(silent=True) or {}
    try:
        day = date.fromisoformat(str(data.get("installed_at") or "")[:10]).isoformat()
    except ValueError:
        return jsonify({"error": "installed_at"}), 400
    nominal = data.get("nominal_kwh")
    try:
        nominal = float(nominal) if nominal not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "nominal_kwh"}), 400
    if nominal is not None and not (10 <= nominal <= 200):
        return jsonify({"error": "nominal_kwh"}), 400
    db = get_db()
    try:
        v = _vehicle(db, plate)
        if not v:
            return jsonify({"error": "not found"}), 404
        cur = db.execute("INSERT INTO vehicle_batteries (vehicle_id, installed_at, nominal_kwh, note) "
                         "VALUES (?, ?, ?, ?)",
                         (v["id"], day, nominal, (data.get("note") or "").strip()[:200] or None))
        db.commit()
        _refresh(db, v)
        return jsonify({"ok": True, "id": cur.lastrowid})
    finally:
        db.close()


@model_bp.route("/api/vehicles/<plate>/batteries/<int:bid>", methods=["DELETE"])
@admin_required
def battery_delete(plate, bid):
    db = get_db()
    try:
        v = _vehicle(db, plate)
        if not v:
            return jsonify({"error": "not found"}), 404
        db.execute("DELETE FROM vehicle_batteries WHERE id = ? AND vehicle_id = ?", (bid, v["id"]))
        db.commit()
        _refresh(db, v)
        return jsonify({"ok": True})
    finally:
        db.close()


def _bms_daily(device, since):
    """Daily mean of the capacity the BMS reports (Influx "ca") since a date."""
    client = detector.get_influx(timeout_ms=60000)
    if not client:
        return {}
    try:
        q = f'''
        from(bucket: "{config.INFLUX_BUCKET}")
          |> range(start: {since}T00:00:00Z)
          |> filter(fn: (r) => r._measurement == "v" and r.d == "{device}" and r._field == "ca")
          |> filter(fn: (r) => r._value > 10.0)
          |> aggregateWindow(every: 1d, fn: mean, createEmpty: false)
        '''
        out = {}
        for t in client.query_api().query(q, org=config.INFLUX_ORG):
            for rec in t.records:
                # aggregateWindow stamps the window end: the day before is the day measured
                d = rec.get_time().astimezone(detector.LOCAL_TZ)
                out[datetime.fromtimestamp(d.timestamp() - 1, detector.LOCAL_TZ).strftime("%Y-%m-%d")] = rec.get_value()
        return out
    finally:
        client.close()


def bms_compare(device, own_kwh):
    """What the BMS reports (Influx "ca") next to the own calculation — shown
    for comparison only, never used for calculating."""
    if not device:
        return None
    try:
        daily = _bms_daily(device, "2019-01-01")
    except Exception as e:  # Influx down: the comparison is optional
        log.warning("bms compare: %s", e)
        return None
    if not daily:
        return None
    day = max(daily)
    latest = round(daily[day], 2)
    return {"latest": latest, "date": day,
            "deviation_pct": round((latest - own_kwh) / own_kwh * 100, 1) if own_kwh else None,
            "resets": fm.bms_resets(daily),
            "series": [[d, round(v, 2)] for d, v in sorted(daily.items())]}


_buffer_cache = {}
BUFFER_TTL_S = 6 * 3600


def _rest_samples(device):
    """Parked moments with about no current (10-min means) whose SoC lies in
    a band of the buffer watch: [{ts, s, u, bt}]."""
    client = detector.get_influx(timeout_ms=60000)
    if not client:
        return []
    bands = " or ".join(f"(r.s >= {lo} and r.s <= {hi})" for lo, hi, _ in fm.BUFFER_BANDS.values())
    q = f'''
    from(bucket: "{config.INFLUX_BUCKET}")
      |> range(start: 2019-01-01T00:00:00Z)
      |> filter(fn: (r) => r._measurement == "v" and r.d == "{device}" and
                           (r._field == "s" or r._field == "u" or r._field == "i" or r._field == "v" or r._field == "bt"))
      |> aggregateWindow(every: 10m, fn: mean, createEmpty: false)
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
      |> filter(fn: (r) => exists r.u and exists r.s and exists r.i and r.u > 100.0
                           and r.i > -3.0 and r.i < 3.0 and (not exists r.v or r.v < 1.0)
                           and ({bands}))
    '''
    try:
        out = []
        for t in client.query_api().query(q, org=config.INFLUX_ORG):
            for rec in t.records:
                v = rec.values
                out.append({"ts": rec.get_time().timestamp(), "s": v.get("s"), "u": v.get("u"),
                            "bt": v.get("bt")})
        return out
    finally:
        client.close()


def buffer_view(device):
    """Buffer watch of a vehicle (fahrzeugmodell.buffer_watch), cached for a
    few hours: the query reads the whole telemetry. None without data."""
    if not device:
        return None
    hit = _buffer_cache.get(device)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        res = fm.buffer_watch(_rest_samples(device)) or None
    except Exception as e:  # Influx down: the watch is optional
        log.warning("buffer watch: %s", e)
        return hit[1] if hit else None
    _buffer_cache[device] = (time.time() + BUFFER_TTL_S, res)
    return res


def _efficiency_view(db, plate):
    """AC and DC: value in use, where it comes from, and the measurement."""
    measured = _measured_efficiency(db, plate) or {}
    out = {}
    for kind in ("ac", "dc"):
        val, origin = charge_efficiency_info(db, plate, dc=kind == "dc")
        out[kind] = {"used": val, "origin": origin, "measured": measured.get(kind)}
    out["global"] = _get_charge_efficiency(db)
    out["has_counter"] = bool(measured)
    return out


@model_bp.route("/api/vehicles/<plate>/efficiency", methods=["POST"])
@admin_required
def efficiency_set(plate):
    """{eff: 0.75..1.0, kind: 'ac'|'dc'} fixes this vehicle's efficiency by
    hand, {eff: null} returns to automatic (measured, else AC/global).
    Recomputes the vehicle's trips."""
    data = request.get_json(silent=True) or {}
    eff = data.get("eff")
    col = "charge_efficiency_dc" if data.get("kind") == "dc" else "charge_efficiency"
    if eff is not None:
        try:
            eff = round(float(eff), 3)
        except (TypeError, ValueError):
            return jsonify({"error": "eff"}), 400
        if not (fm.EFF_MIN <= eff <= fm.EFF_MAX):
            return jsonify({"error": "eff"}), 400
    db = get_db()
    try:
        v = _vehicle(db, plate)
        if not v:
            return jsonify({"error": "not found"}), 404
        db.execute(f"UPDATE vehicles SET {col} = ? WHERE id = ?", (eff, v["id"]))
        db.commit()
        _refresh(db, v)
        return jsonify({"ok": True, "used": _get_charge_efficiency(db, plate),
                        "used_dc": _get_charge_efficiency(db, plate, dc=True)})
    finally:
        db.close()
