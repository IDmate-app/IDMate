"""BMS learning (dashboard button "BMS lernen", opt-in per vehicle): store
what the car's display shows next to the CAN values of that moment, and the
models fitted from those points (logic in bms_learn.py)."""

import json
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, jsonify, request

import bms_learn as bl
import config
import detector
import fahrzeugmodell as fm
import stick_cfg
from app import (_state_cache, _state_lock, _translations, active_vehicle, get_bat_kwh,
                 get_db, get_language, log)

bms_bp = Blueprint("bms", __name__)

_temp_cache = {}               # (device, field) -> (t, {YYYY-MM-DDTHH: mean})
_TEMP_TTL_S = 6 * 3600
SHOWN_POINTS = 3


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


def _series(device, hours=36):
    """[(epoch s, odometer, SoC)] of the last hours, oldest first
    (delta-compressed telemetry: both carried forward)."""
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
                t = r.get("_time")
                out.append((t.timestamp() if t else None, float(od), float(soc)))
        return out
    except Exception as e:      # the point is stored without the windows
        log.warning("bms learn: consumption series failed: %s", e)
        return []
    finally:
        client.close()


def _hourly(device, field="et"):
    """{"YYYY-MM-DDTHH" (UTC): mean of that hour} of one telemetry field over
    the last year: et outside, bt battery, ct cabin temperature, hk heating
    and cooling power. The car only reports while it is awake, so there are
    hours for the trips and little else."""
    hit = _temp_cache.get((device, field))
    if hit and time.time() - hit[0] < _TEMP_TTL_S:
        return hit[1]
    vals = {}
    client = detector.get_influx(timeout_ms=30000)
    if client:
        try:
            q = f'''
            from(bucket: "{config.INFLUX_BUCKET}")
              |> range(start: -365d)
              |> filter(fn: (r) => r._measurement == "v" and r.d == "{device}" and r._field == "{field}")
              |> filter(fn: (r) => r._value > -50.0 and r._value < 60.0)
              |> aggregateWindow(every: 1h, fn: mean, createEmpty: false, timeSrc: "_start")
            '''
            for table in client.query_api().query(q, org=config.INFLUX_ORG):
                for rec in table.records:
                    vals[rec.get_time().strftime("%Y-%m-%dT%H")] = rec.get_value()
        except Exception as e:
            log.warning("bms learn: hourly %s failed: %s", field, e)
        finally:
            client.close()
    _temp_cache[(device, field)] = (time.time(), vals)
    return vals


def _utc(local_iso):
    """Trip times are stored as local time without offset."""
    try:
        dt = datetime.fromisoformat(str(local_iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=detector.LOCAL_TZ)).astimezone(timezone.utc)


def _trips(db, device, extra=False):
    """The trips of the last year as the models want them: km, kWh/100 km,
    average speed, the outside temperature during the trip and how long the
    car stood before it. extra (export): battery and cabin temperature at
    the start and the heating power, where the stick reports them."""
    since = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    temps = _hourly(device)
    more = {f: _hourly(device, f) for f in ("bt", "ct", "hk")} if extra else {}
    out, prev_end = [], None
    # every trip counts for the standing time, only those with energy as points
    for r in db.execute("SELECT id, start_time, end_time, distance_km, energy_kwh, soc_start, soc_end FROM trips "
                        "WHERE device = ? AND start_time >= ? ORDER BY start_time", (device, since)):
        t0, t1 = _utc(r["start_time"]), _utc(r["end_time"])
        stood = round((t0 - prev_end).total_seconds() / 3600, 2) if t0 and prev_end and t0 >= prev_end else None
        prev_end = t1 or prev_end
        if not (r["energy_kwh"] or 0) > 0 or not (r["distance_km"] or 0) > 0:
            continue
        t = {"id": r["id"], "start": r["start_time"], "end": r["end_time"],
             "km": r["distance_km"], "kwh": r["energy_kwh"],
             "cons": fm.trip_consumption(r["energy_kwh"], r["distance_km"]),
             "temp": bl.trip_temp(temps, t0, t1), "stood_h": stood,
             "v": _avg_kmh(r["start_time"], r["end_time"], r["distance_km"]),
             "soc_start": r["soc_start"], "soc_end": r["soc_end"]}
        if extra and t0:
            h = t0.strftime("%Y-%m-%dT%H")
            t.update(bat_temp_start=_r1(more["bt"].get(h)), cabin_temp_start=_r1(more["ct"].get(h)),
                     hvac_kw=_r1(bl.trip_temp(more["hk"], t0, t1), 2))
        out.append(t)
    return out


PARKED_TEMP_H = 0.5            # standing this long: the sensor no longer tells the weather


def _outside_temp(db, device, live, parked_h):
    """(°C, "car" | "forecast"). On the road the car's own sensor. Standing,
    the sensor holds the last value of the trip before, or the garage: then
    the forecast for this hour at the car's place, which comes with the wind
    (wind.py) — so the range before setting off already knows the weather."""
    sensor = live.get("ext_temp")
    moving = (live.get("speed") or 0) > 0 or (parked_h is not None and parked_h < PARKED_TEMP_H)
    if moving or not config.WIND_SOURCE:
        return sensor, "car"
    with _state_lock:
        st = _state_cache.get(device) or {}
        lat, lon = st.get("lat"), st.get("lon")
    if lat is None or lon is None:
        return sensor, "car"
    import wind
    try:
        temp = wind.temp_at(db, float(lat), float(lon))
    except Exception as e:      # the range must not fail over the weather
        log.warning("bms learn: forecast temperature failed: %s", e)
        temp = None
    return (round(temp, 1), "forecast") if temp is not None else (sensor, "car")


def _r1(v, nd=1):
    return round(v, nd) if v is not None else None


_model_cache = {}              # device -> (key, model)


def _model(device, trips):
    """The speed model of these trips; fitted anew only when they changed
    (the fit checks four forms on held-out trips, a few 100 ms a year)."""
    key = (len(trips), trips[-1]["start"] if trips else None, round(sum(t["km"] for t in trips)),
           sum(1 for t in trips if t.get("temp") is not None))
    hit = _model_cache.get(device)
    if hit and hit[0] == key:
        return hit[1]
    model = bl.speed_model(trips)
    _model_cache[device] = (key, model)
    return model


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
           # the dialog shows the last few (to delete a slip); all of them
           # are in the export
           "points": list(reversed(points[-SHOWN_POINTS:]))}
    can = live.get("can_soc")
    if can is None:
        return out
    bt, et = live.get("bat_temp"), live.get("ext_temp")
    now = {"display_soc": bl.predict_display(disp, can, bt, et)}
    zero = bl.display_zero_can(disp, bt, et)
    left = (can - (zero or 0.0)) / 100.0 * cap if cap else None
    now["remaining_kwh"] = round(left, 1) if left is not None else None
    series = _series(device)
    ctx = bl.trip_context(series, time.time())
    live.update(trip_km=ctx["trip_km"], fresh_trip=bl.fresh_trip(ctx))
    if rng and rng.get("window_km") and now["display_soc"] is not None and live.get("fresh"):
        cons = bl.window_consumption([(o, s) for _t, o, s in series], cap).get(rng["window_km"])
        if cons:
            now["vw_range"] = round(now["display_soc"] / 100.0 * rng["e_kwh"] / (rng["cons_factor"] * cons) * 100)
    et, temp_src = _outside_temp(db, device, live, ctx["parked_h"])
    now["temp"], now["temp_src"] = et, temp_src
    trips = _trips(db, device) if left else []
    now["real"] = bl.real_range(left, trips, et) if left else None
    # One number for map and box: the model from the own trips at today's
    # temperature, at motorway speed, or, while driving / driven in the last
    # 2 h, at the current average speed (charge planning on the way).
    cruise = None
    if left:
        model = _model(device, trips)
        since = time.time() - bl.DRIVE_WINDOW_H * 3600
        drv = bl.recent_speed([p for p in series if p[0] and p[0] >= since]) if model else None
        v_eval = drv["v"] if drv else bl.CRUISE_AVG_KMH
        # A car that stood in the cold spends energy once on warming up
        # before it gets anywhere: taken off the energy for the range (and
        # with it off the map and the check of a place).
        warm = min(left, bl.warmup_kwh(model, et, ctx["parked_h"])) if model else 0.0
        cruise = bl.cruise_range(left - warm, model, et, v=v_eval)
        if cruise:
            cruise.update(mode="drive" if drv else "model", temp=et, temp_src=temp_src, trips=model["n"],
                          with_temp=model["temp"], dist=drv["km"] if drv else None,
                          warmup_kwh=round(warm, 2), parked_h=ctx["parked_h"],
                          left_full=round(left, 2),          # for "arrive with … %" on the map
                          soc_now=now["display_soc"] if now["display_soc"] is not None else can,
                          model=model, left=round(left - warm, 2), v_eval=v_eval)   # for the wind per direction
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
        series = _series(v["device"])
        win = bl.window_consumption([(o, s) for _t, o, s in series], cap)
        ctx = bl.trip_context(series, time.time())
        db.execute(
            """INSERT INTO bms_points (plate, created_at, disp_soc, disp_range, disp_cons, can_soc, can_age_s,
                   bat_temp, ext_temp, speed, odometer, capacity_kwh, cons_10, cons_30, cons_50, cons_100,
                   can_range, soe, hvac_kw, cabin_temp, trip_km, trip_min, stood_h)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (v["plate"], datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), soc, rng, cons,
             live["can_soc"], live["age_s"], live.get("bat_temp"), live.get("ext_temp"), live.get("speed"),
             live.get("odometer"), cap, win[10], win[30], win[50], win[100], live.get("range_km"),
             live.get("soe"), live.get("hvac_kw"), live.get("cabin_temp"),
             ctx["trip_km"], ctx["trip_min"], ctx["stood_h"]))
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


@bms_bp.route("/api/bms-learn/export")
def bms_export():
    """Everything the models are built from, as one JSON file to talk about
    how they learn: all learning points, the fitted models with the error of
    every form, and the trips of the last year with what the model expects
    for each. No positions, addresses, plate or device name."""
    db = get_db()
    try:
        v = active_vehicle(db)
        if not v:
            return jsonify({"error": _t()["err_not_found"]}), 404
        st = _state(db, v)
        device = v["device"]
        trips = _trips(db, device, extra=True) if device else []
        model = _model(device, trips) if device else None
        usable = lambda t: bool(t["cons"] and t.get("v") and 25 <= t["v"] <= 125 and 5 <= t["cons"] <= 60)
        for t in trips:
            t["in_model"] = bool(model) and usable(t) and (t["temp"] is not None or not model["temp"])
            if model and t.get("v") and t["km"]:
                warm = bl.warmup_kwh(model, t["temp"], t["stood_h"])
                t["model_cons"] = round(bl.consumption(model, t["v"], t["temp"]) + warm * 100.0 / t["km"], 1)
                t["model_warmup_kwh"] = round(warm, 2)
        points = [{k: r[k] for k in r.keys() if k != "plate"} for r in db.execute(
            "SELECT * FROM bms_points WHERE plate = ? ORDER BY created_at", (v["plate"],))]
        now = st.get("now") or {}
        data = {
            "idmate": config.VERSION,
            "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "about": {
                "speed_model": "kWh/100 km = a + b*(v/100)^2 + c*cold + p*cold*100/v, plus e0*cold*w kWh once per "
                               "trip; cold = max(0, 15 - outside C), w = 1 - exp(-stood_h/2); v = trip average km/h",
                "cv": "error of each form on trips left out of the fit, kWh/100 km (lower is better)",
                "trips": "cons = kwh/km*100 (none below 10 km); temp = outside C during the trip; stood_h = hours "
                         "since the trip before; model_cons = what the model expects; hvac_kw = mean heating/cooling "
                         "power of the hours the trip touches",
                "points": "disp_* typed from the car's display, the rest read from the CAN bus at that moment; "
                          "trip_km/trip_min = since the trip began, stood_h = standing time before it",
            },
            "capacity_kwh": get_bat_kwh(db, device, datetime.now().isoformat()) if device else None,
            "now": {"live": st.get("live"), "display_soc": now.get("display_soc"),
                    "remaining_kwh": now.get("remaining_kwh"), "vw_range": now.get("vw_range"),
                    "real": now.get("real"), "cruise": now.get("cruise")},
            "display_model": st.get("display"), "range_model": st.get("range"), "learn": st.get("learn"),
            "speed_model": model,
            "points": points,
            "trips": trips,
        }
    finally:
        db.close()
    name = "idmate-bms-%s.json" % datetime.now().strftime("%Y-%m-%d")
    return Response(json.dumps(data, ensure_ascii=False, indent=1), mimetype="application/json",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % name})
