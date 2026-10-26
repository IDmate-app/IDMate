"""Analysis → "Fahrzeug": a certificate-like report of one vehicle over its
whole history — battery health from the own calculation, consumption,
charging, mileage, data quality (logic in fahrzeugmodell.py)."""

import hashlib
import json
from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

import config
import detector
import diagnose
import fahrzeugmodell as fm
from app import (_capacity_trend, _find_odo_gaps, _median_line, _measured_efficiency, _real_consumption,
                 active_vehicle, allowed_vehicle_ids, charge_efficiency_info, get_db, log)

report_bp = Blueprint("report", __name__)


def _vehicle(db):
    """?plate= when the user may see it, else the active vehicle."""
    plate = request.args.get("plate")
    if plate:
        v = db.execute("SELECT * FROM vehicles WHERE plate = ?", (plate,)).fetchone()
        allowed = allowed_vehicle_ids(db, current_user)
        if v and (allowed is None or v["id"] in allowed):
            return dict(v)
    v = active_vehicle(db)
    return dict(v) if v else None


def _battery(db, v):
    tr = _capacity_trend(db, v["plate"])
    segs = tr.get("segments") or []
    cur = segs[-1] if segs else {}
    nominal = cur.get("nominal")
    pts = tr.get("points") or []
    # Spread over the last 12 months of the current battery
    cutoff = (datetime.now() - timedelta(days=365)).timestamp()
    seg_i = len(segs) - 1
    recent = [p["cap"] for p in pts
              if p["ts"] >= cutoff and fm.segment_index(segs, p["ts"]) == seg_i] if segs else []
    cap = tr.get("cap_now")
    soh = round(cap / nominal * 100, 1) if cap and nominal else None
    p25, p75 = fm.percentile(recent, 0.25), fm.percentile(recent, 0.75)
    try:
        from blueprints.model import bms_compare, buffer_view
        bms = bms_compare(v["device"], cap)
        buffer = buffer_view(v["device"])
    except Exception:
        bms = buffer = None
    if bms:
        bms.pop("series", None)
    # chart: the whole history + trend endpoints per battery
    chart_pts = [{"date": datetime.fromtimestamp(p["ts"]).strftime("%Y-%m-%d"), "cap": round(p["cap"], 1)}
                 for p in pts]
    trend = []
    for i, g in enumerate(segs):
        own = [p for p in pts if fm.segment_index(segs, p["ts"]) == i]
        if g["kind"] not in ("trend", "level") or not own:
            continue
        t0 = own[0]["ts"]
        nxt = segs[i + 1]["from"] if i + 1 < len(segs) else None
        t1 = datetime.fromisoformat(nxt).timestamp() - 86400 if nxt else datetime.now().timestamp()
        if trend:
            trend.append({"date": datetime.fromtimestamp(t0).strftime("%Y-%m-%d"), "cap": None})
        for t in (t0, t1):
            c = tr["cap_at"](t)
            trend.append({"date": datetime.fromtimestamp(t).strftime("%Y-%m-%d"),
                          "cap": round(c, 2) if c else None})
    return {
        "capacity_kwh": cap, "nominal_kwh": nominal, "soh_pct": soh,
        "soh_p25": round(p25 / nominal * 100, 1) if p25 and nominal else None,
        "soh_p75": round(p75 / nominal * 100, 1) if p75 and nominal else None,
        "n": tr.get("n"), "n_12m": len(recent), "slope_per_year": tr.get("slope_per_year"),
        "kind": cur.get("kind"), "source": tr.get("source"),
        "batteries": [{k: g.get(k) for k in ("from", "nominal", "note", "cap_now", "n")} for g in segs],
        "battery_change_suspected": tr.get("battery_change"),
        "bms": bms, "buffer": buffer, "chart": {"points": chart_pts, "trend": trend, "median": _median_line(tr)},
    }


def _consumption(db, v):
    trips = [dict(r) for r in db.execute(
        "SELECT start_time, distance_km, energy_kwh, kw_start, kw_end FROM trips "
        "WHERE device = ? AND COALESCE(is_manual, 0) = 0 ORDER BY start_time", (v["device"],))]
    since = (date.today() - timedelta(days=365)).isoformat()

    def real(frm=None):
        r = _real_consumption(db, v["plate"], frm) or {}
        return {k: r.get(k) for k in ("cons_batt", "cons_grid", "km")}
    avg = fm.avg_consumption(trips)
    months = fm.monthly_consumption(trips)
    temps = _monthly_temp(v["device"], months[0]["month"] if months else None)
    for m in months:
        m["temp"] = temps.get(m["month"])
    return {
        "real_all": real(), "real_12m": real(since),
        "trips_batt": avg["cons"] if avg and avg["km"] >= 100 else None,
        "seasons": fm.season_consumption(trips),
        "recuperation": fm.recuperation(trips),
        "monthly": months,
        "temp_fit": fm.temperature_fit(months),
    }


def _monthly_temp(device, first_month):
    """Mean outside temperature (Influx "et") per month while the car was
    awake — {YYYY-MM: °C}; {} without Influx."""
    if not device or not first_month:
        return {}
    client = detector.get_influx(timeout_ms=30000)
    if not client:
        return {}
    try:
        q = f'''
        from(bucket: "{config.INFLUX_BUCKET}")
          |> range(start: {first_month}-01T00:00:00Z)
          |> filter(fn: (r) => r._measurement == "v" and r.d == "{device}" and r._field == "et")
          |> filter(fn: (r) => r._value > -50.0 and r._value < 60.0)
          |> aggregateWindow(every: 1mo, fn: mean, createEmpty: false, timeSrc: "_start")
        '''
        out = {}
        for t in client.query_api().query(q, org=config.INFLUX_ORG):
            for rec in t.records:
                out[rec.get_time().strftime("%Y-%m")] = round(rec.get_value(), 1)
        return out
    except Exception as e:  # the chart works without temperatures
        log.warning("vehicle report: monthly temperature failed: %s", e)
        return {}
    finally:
        client.close()


def _charging(db, v):
    sessions = [dict(r) for r in db.execute(
        "SELECT * FROM charge_sessions WHERE vehicle_plate = ? ORDER BY start_time", (v["plate"],))]
    homes = {r[0] for r in db.execute("SELECT name FROM charge_locations WHERE is_home = 1")}
    eff = {kind: charge_efficiency_info(db, v["plate"], dc=kind == "dc") for kind in ("ac", "dc")}
    out = fm.charging_summary(sessions, homes, eff["ac"][0], eff["dc"][0])
    measured = _measured_efficiency(db, v["plate"]) or {}
    for kind in ("ac", "dc"):
        val, origin = eff[kind]
        m = measured.get(kind)
        out["eff_" + kind] = {"value": val, "origin": origin, "n": m["n"] if m else 0}
    return out


def _mileage(db, v):
    r = db.execute(
        "SELECT MIN(start_time) AS first, MAX(end_time) AS last, COUNT(*) AS n, "
        "SUM(distance_km) AS km, MIN(NULLIF(odo_start, 0)) AS odo0, MAX(odo_end) AS odo1 "
        "FROM trips WHERE device = ?", (v["device"],)).fetchone()
    odo1 = r["odo1"]
    o = db.execute("SELECT MAX(odometer) AS o FROM charge_sessions WHERE vehicle_plate = ?",
                   (v["plate"],)).fetchone()["o"]
    if o and (not odo1 or o > odo1):
        odo1 = o
    years = None
    if r["first"]:
        days = (datetime.now() - datetime.fromisoformat(str(r["first"])[:19])).days
        years = days / 365.25 if days > 30 else None
    span = (odo1 - r["odo0"]) if odo1 and r["odo0"] else None
    return {"first": str(r["first"])[:10] if r["first"] else None, "last": str(r["last"])[:10] if r["last"] else None,
            "odometer": round(odo1) if odo1 else None, "trips": r["n"],
            "km_recorded": round(r["km"] or 0),
            "km_per_year": round(span / years) if span and years else None,
            "avg_trip_km": round((r["km"] or 0) / r["n"], 1) if r["n"] else None}


def _quality(db, v):
    checks = [c for c in diagnose.sql_checks(db, [v], 0.9) if c.get("vehicle") == v["plate"]]
    n_trips = db.execute("SELECT COUNT(*) FROM trips WHERE device = ? AND distance_km >= 1",
                         (v["device"],)).fetchone()[0]
    with_e = db.execute("SELECT COUNT(*) FROM trips WHERE device = ? AND distance_km >= 1 "
                        "AND energy_kwh IS NOT NULL", (v["device"],)).fetchone()[0]
    try:
        gaps = [g for g in _find_odo_gaps(db, v["plate"], min_km=2) if g.get("prev_trip_id")
                and (g.get("assigned_km") or 0) < g.get("km", 0) - 1]
    except Exception:
        gaps = []
    return {"errors": sum(c["level"] == "error" for c in checks),
            "warnings": sum(c["level"] == "warn" for c in checks),
            "energy_pct": round(with_e / n_trips * 100) if n_trips else None,
            "open_gaps": len(gaps), "open_gap_km": round(sum(g["km"] for g in gaps))}


@report_bp.route("/api/vehicle-report")
@login_required
def vehicle_report():
    db = get_db()
    try:
        v = _vehicle(db)
        if not v or not v.get("device"):
            return jsonify({"error": "no vehicle"}), 404
        rep = {
            "vehicle": {"plate": v["plate"], "name": v.get("name"), "model": v.get("model"),
                        "vin": diagnose._vin(v.get("vin"))},
            "battery": _battery(db, v),
            "consumption": _consumption(db, v),
            "charging": _charging(db, v),
            "mileage": _mileage(db, v),
            "quality": _quality(db, v),
        }
    finally:
        db.close()
    b, q, m = rep["battery"], rep["quality"], rep["mileage"]
    b["soh_pm"] = round((b["soh_p75"] - b["soh_p25"]) / 2, 1) if b.get("soh_p25") is not None else None
    q["accuracy"] = fm.data_accuracy(q.get("energy_pct"),
                                     q.get("open_gap_km"), m.get("km_recorded"), q.get("errors"), q.get("warnings"))
    rep["meta"] = {"created": datetime.now().strftime("%Y-%m-%d %H:%M"), "version": config.VERSION, "repo": config.REPO_URL}
    # Fingerprint of the content: the same data gives the same code.
    body = json.dumps({k: rep[k] for k in ("vehicle", "battery", "consumption", "charging", "mileage", "quality")},
                      sort_keys=True, default=str)
    rep["meta"]["fingerprint"] = hashlib.sha256(body.encode()).hexdigest()[:12].upper()
    return jsonify(rep)
