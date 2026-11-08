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
import stick_cfg
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
    out = {k: st.get(k) for k in ("bat_temp", "ext_temp", "speed", "odometer", "range_km",
                                  "soe", "hvac_kw", "cabin_temp")}
    out["can_soc"] = st.get("soc")
    out["age_s"] = round(age) if age is not None else None
    out["fresh"] = age is not None and age <= bl.FRESH_S and st.get("soc") is not None
    return out


def _series(device, hours=36, with_time=False):
    """[(odometer, SoC)] of the last hours, oldest first (delta-compressed
    telemetry: both carried forward); with_time: [(epoch s, odometer, SoC)]."""
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
                if with_time:
                    t = r.get("_time")
                    out.append((t.timestamp() if t else None, float(od), float(soc)))
                else:
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
             "temp": temps.get((r["start_time"] or "")[:10]),
             "v": _avg_kmh(r["start_time"], r["end_time"], r["distance_km"])}
            for r in db.execute(
                "SELECT start_time, end_time, distance_km, energy_kwh FROM trips WHERE device = ? "
                "AND start_time >= ? AND energy_kwh > 0 AND distance_km > 0", (device, since))]


def _avg_kmh(start, end, km):
    """Average speed of a trip (start/end ISO), None if unusable."""
    try:
        h = (datetime.fromisoformat(str(end).replace("Z", "+00:00"))
             - datetime.fromisoformat(str(start).replace("Z", "+00:00"))).total_seconds() / 3600
    except (TypeError, ValueError):
        return None
    return round(km / h, 1) if h > 0 and km else None


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
           "learn": _learn_status(points, disp, rng),
           "stick": stick_cfg.build(points),
           "stick_sent": (stick_cfg.last_sent(db, device) or {}).get("ts") if device else None,
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
    trips = _trips(db, device) if left else []
    now["real"] = bl.real_range(left, trips, et) if left else None
    # One number for map and box: the model from the own trips at today's
    # temperature, at motorway speed, or, while driving / driven in the last
    # 2 h, at the current average speed (charge planning on the way).
    cruise = None
    if left:
        model = bl.speed_model(trips)
        drv = bl.recent_speed(_series(device, hours=bl.DRIVE_WINDOW_H, with_time=True)) if model else None
        v_eval = drv["v"] if drv else bl.CRUISE_AVG_KMH
        cruise = bl.cruise_range(left, model, et, v=v_eval)
        if cruise:
            cruise.update(mode="drive" if drv else "model", temp=et, trips=model["n"],
                          with_temp=model["temp"], dist=drv["km"] if drv else None,
                          model=model, left=round(left, 2), v_eval=v_eval)   # for the wind per direction
            if not drv:                 # shown as cruise control 130, computed at its trip average
                cruise.update(v=round(bl.CRUISE_KMH), v_avg=round(bl.CRUISE_AVG_KMH))
    now["cruise"] = cruise
    out["now"] = now
    return out


def _cons(data):
    """Displayed average consumption → kWh/100 km. Typed as Wh/km (228) or,
    as before, kWh/100 km (22.8): no car shows less than 100 Wh/km or more
    than 80 kWh/100 km, so the two never overlap."""
    v = data.get("disp_cons")
    if v in (None, ""):
        return None
    v = float(str(v).replace(",", "."))
    if v >= 100:
        v = v / 10.0
    if not 3 <= v <= 80:
        raise ValueError("disp_cons")
    return round(v, 2)


def _learn_status(points, disp, rng):
    """How far the learning is: points, error, what joins next, whether the
    range still has to be typed in."""
    off = bl.range_offset(points)
    return {"n": len(points), "min_points": bl.MIN_POINTS, "full_points": bl.FULL_POINTS,
            "terms": disp["terms"] if disp else [], "mae_loo": disp.get("mae_loo") if disp else None,
            "range_n": off["n"], "range_need": bl.RANGE_CAN_MIN_N, "range_spread": off["spread"],
            "range_diff": off["diff"], "range_from_can": off["reliable"]}


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
            cons = _cons(data)
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
                   can_range, soe, hvac_kw, cabin_temp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (v["plate"], datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), soc, rng, cons,
             live["can_soc"], live["age_s"], live.get("bat_temp"), live.get("ext_temp"), live.get("speed"),
             live.get("odometer"), cap, win[10], win[30], win[50], win[100], live.get("range_km"),
             live.get("soe"), live.get("hvac_kw"), live.get("cabin_temp")))
        db.commit()
        log.info("bms learn: %s display %s %% at CAN %s %%", v["plate"], soc, live["can_soc"])
        stick_cfg.push_vehicle(db, v["plate"], v["device"])
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
        stick_cfg.push_vehicle(db, v["plate"], v["device"])
        return jsonify(_state(db, v))
    finally:
        db.close()
