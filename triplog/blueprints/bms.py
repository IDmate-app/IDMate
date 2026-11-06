"""BMS learning (dashboard button "BMS lernen", opt-in per vehicle): store
what the car's display shows next to the CAN values of that moment, and the
models fitted from those points (logic in bms_learn.py)."""

import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

import bms_learn as bl
import config
import detector
import fahrzeugmodell as fm
from app import (_state_cache, _state_lock, _translations, active_vehicle, get_bat_kwh,
                 get_db, get_language, log)

bms_bp = Blueprint("bms", __name__)

_temp_cache = {}               # device -> (t, {YYYY-MM-DD: °C})
_TEMP_TTL_S = 6 * 3600


def _t():
    return _translations.get(get_language(), _translations["DE"])


def _age_s(iso):
    try:
        ts = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())


def _live(device):
    """CAN values of the state cache (MQTT), with the age of the SoC."""
    with _state_lock:
        st = dict(_state_cache.get(device, {}))
    ft = st.get("field_times") or {}
    age = _age_s(ft.get("soc"))
    out = {k: st.get(k) for k in ("bat_temp", "ext_temp", "speed", "odometer", "range_km")}
    out["can_soc"] = st.get("soc")
    out["age_s"] = round(age) if age is not None else None
    out["fresh"] = age is not None and age <= bl.FRESH_S and st.get("soc") is not None
    return out


def _series(device, hours=36):
    """[(odometer, SoC)] of the last hours, oldest first (delta-compressed
    telemetry: both carried forward)."""
    client = detector.get_influx(timeout_ms=15000)
    if not client:
        return []
    try:
        q = f'''
        from(bucket: "{config.INFLUX_BUCKET}")
          |> range(start: -{int(hours)}h)
          |> filter(fn: (r) => r._measurement == "v" and r.d == "{device}" and (r._field == "od" or r._field == "s"))
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
          |> sort(columns: ["_time"])
        '''
        rows = []
        for table in client.query_api().query(q, org=config.INFLUX_ORG):
            rows += [r.values for r in table.records]
        rows.sort(key=lambda r: r.get("_time"))
        out, od, soc = [], None, None
        for r in rows:
            od = r.get("od") if r.get("od") is not None else od
            soc = detector.sanitize_soc(r.get("s")) if r.get("s") is not None else soc
            if od is not None and soc is not None:
                out.append((float(od), float(soc)))
        return out
    except Exception as e:      # the point is stored without the windows
        log.warning("bms learn: consumption series failed: %s", e)
        return []
    finally:
        client.close()


def _daily_temps(device):
    hit = _temp_cache.get(device)
    if hit and time.time() - hit[0] < _TEMP_TTL_S:
        return hit[1]
    temps = {}
    client = detector.get_influx(timeout_ms=30000)
    if client:
        try:
            q = f'''
            from(bucket: "{config.INFLUX_BUCKET}")
              |> range(start: -365d)
              |> filter(fn: (r) => r._measurement == "v" and r.d == "{device}" and r._field == "et")
              |> filter(fn: (r) => r._value > -50.0 and r._value < 60.0)
              |> aggregateWindow(every: 1d, fn: mean, createEmpty: false, timeSrc: "_start")
            '''
            for table in client.query_api().query(q, org=config.INFLUX_ORG):
                for rec in table.records:
                    temps[rec.get_time().strftime("%Y-%m-%d")] = rec.get_value()
        except Exception as e:
            log.warning("bms learn: daily temperatures failed: %s", e)
        finally:
            client.close()
    _temp_cache[device] = (time.time(), temps)
    return temps


def _trips(db, device):
    since = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    temps = _daily_temps(device)
    return [{"km": r["distance_km"], "cons": fm.trip_consumption(r["energy_kwh"], r["distance_km"]),
             "temp": temps.get((r["start_time"] or "")[:10])}
            for r in db.execute(
                "SELECT start_time, distance_km, energy_kwh FROM trips WHERE device = ? "
                "AND start_time >= ? AND energy_kwh > 0 AND distance_km > 0", (device, since))]


def _state(db, v):
    """Everything the dashboard box and the input dialog show."""
    device = v["device"]
    points = [dict(r) for r in db.execute(
        "SELECT * FROM bms_points WHERE plate = ? ORDER BY created_at", (v["plate"],))]
    live = _live(device) if device else {"fresh": False}
    disp = bl.fit_display(points)
    rng = bl.fit_range(points)
    cap = get_bat_kwh(db, device, datetime.now().isoformat()) if device else None
    out = {"enabled": bool(v.get("bms_learn")), "plate": v["plate"], "live": live,
           "fresh_s": bl.FRESH_S, "n": len(points), "display": disp, "range": rng,
           "points": list(reversed(points[-30:]))}
    can = live.get("can_soc")
    if can is None:
        return out
    bt, et = live.get("bat_temp"), live.get("ext_temp")
    now = {"display_soc": bl.predict_display(disp, can, bt, et)}
    zero = bl.display_zero_can(disp, bt, et)
    left = (can - (zero or 0.0)) / 100.0 * cap if cap else None
    now["remaining_kwh"] = round(left, 1) if left is not None else None
    if rng and rng.get("window_km") and now["display_soc"] is not None and live.get("fresh"):
        cons = bl.window_consumption(_series(device), cap).get(rng["window_km"])
        if cons:
            now["vw_range"] = round(now["display_soc"] / 100.0 * rng["e_kwh"] / (rng["cons_factor"] * cons) * 100)
    now["real"] = bl.real_range(left, _trips(db, device), et) if left else None
    out["now"] = now
    return out


def _num(data, key, lo, hi):
    v = data.get(key)
    if v in (None, ""):
        return None
    v = float(str(v).replace(",", "."))
    if not lo <= v <= hi:
        raise ValueError(key)
    return v


@bms_bp.route("/api/bms-learn")
def bms_state():
    db = get_db()
    try:
        v = active_vehicle(db)
        if not v:
            return jsonify({"enabled": False})
        return jsonify(_state(db, v))
    finally:
        db.close()


@bms_bp.route("/api/bms-learn", methods=["POST"])
def bms_add():
    t = _t()
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        v = active_vehicle(db)
        if not v or not v.get("bms_learn"):
            return jsonify({"error": t["bms_not_enabled"]}), 400
        try:
            soc = _num(data, "disp_soc", 0, 100)
            rng = _num(data, "disp_range", 0, 1500)
            cons = _num(data, "disp_cons", 3, 80)
        except ValueError:
            return jsonify({"error": t["bms_invalid_input"]}), 400
        if soc is None:
            return jsonify({"error": t["bms_soc_required"]}), 400
        live = _live(v["device"])
        if not live["fresh"]:
            age = live.get("age_s")
            msg = t["bms_stale"].replace("{0}", str(round(age / 60))) if age is not None else t["bms_stale_none"]
            return jsonify({"error": msg, "live": live}), 409
        cap = get_bat_kwh(db, v["device"], datetime.now().isoformat())
        win = bl.window_consumption(_series(v["device"]), cap)
        db.execute(
            """INSERT INTO bms_points (plate, created_at, disp_soc, disp_range, disp_cons, can_soc, can_age_s,
                   bat_temp, ext_temp, speed, odometer, capacity_kwh, cons_10, cons_30, cons_50, cons_100,
                   can_range)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (v["plate"], datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), soc, rng, cons,
             live["can_soc"], live["age_s"], live.get("bat_temp"), live.get("ext_temp"), live.get("speed"),
             live.get("odometer"), cap, win[10], win[30], win[50], win[100], live.get("range_km")))
        db.commit()
        log.info("bms learn: %s display %s %% at CAN %s %%", v["plate"], soc, live["can_soc"])
        return jsonify(_state(db, v))
    finally:
        db.close()


@bms_bp.route("/api/bms-learn/<int:point_id>", methods=["DELETE"])
def bms_delete(point_id):
    db = get_db()
    try:
        v = active_vehicle(db)
        if not v:
            return jsonify({"error": _t()["err_not_found"]}), 404
        db.execute("DELETE FROM bms_points WHERE id = ? AND plate = ?", (point_id, v["plate"]))
        db.commit()
        return jsonify(_state(db, v))
    finally:
        db.close()
