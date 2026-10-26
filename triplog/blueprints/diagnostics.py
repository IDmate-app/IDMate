"""Admin → Diagnose: health checks, recent log, ZIP export of recorded data,
derived figures and model parameters (logic in diagnose.py)."""

import json
import os
import secrets
import threading
import time
from datetime import date, datetime, timedelta

from flask import Blueprint, Response, jsonify, request, send_file
from flask_login import current_user

import config
import detector
import diagnose
import fahrzeugmodell as fm
import migrations
import trip_check
from app import (ENCRYPTED_SETTINGS, _ALL_FIELDS, _annotate_gaps, _capacity_points,
                 _capacity_trend, _find_odo_gaps, _get_charge_efficiency,
                 _real_consumption, admin_required, active_vehicle, get_bat_kwh,
                 get_db, log)
import app as app_module

diagnostics_bp = Blueprint("diagnostics", __name__)
diagnose.install_log_ring()

# Endpoints whose JSON goes into calc/api/ (what the UI shows for the active
# vehicle). {plate} is filled in; from/to are appended.
SNAPSHOT = [
    "/api/stats", "/api/analysis", "/api/analysis/yearly-purposes", "/api/battery-history",
    "/api/between-charges", "/api/efficiency-data", "/api/charge/stats", "/api/charge/gaps",
    "/api/charge/meter-gaps", "/api/charge/sessions/list", "/api/vehicle-state",
    "/api/vehicles/capacity-estimates", "/api/vehicles/{plate}/capacity-detail",
    "/api/admin/charge-efficiency", "/api/admin/gaps?vehicle={plate}",
    "/api/admin/stats/carrier-history", "/api/admin/db-stats", "/api/visited-countries",
]
# Telemetry resolution caps: a raw export of months would be gigabytes.
RAW_MAX_DAYS = 3
FINE_MAX_DAYS = 31


def _range(args):
    """from/to (local dates, inclusive) → (from, to, since, until) with
    since/until as SQL bounds on the local ISO times. Default: 30 days."""
    today = date.today()
    try:
        d1 = date.fromisoformat(args.get("to") or today.isoformat())
        d0 = date.fromisoformat(args.get("from") or (d1 - timedelta(days=29)).isoformat())
    except ValueError:
        d1, d0 = today, today - timedelta(days=29)
    if d0 > d1:
        d0, d1 = d1, d0
    return d0, d1, d0.isoformat(), (d1 + timedelta(days=1)).isoformat()


def _vehicles(db):
    return db.execute(
        "SELECT id, plate, name, model, device, battery_capacity_kwh FROM vehicles "
        "WHERE plate IS NOT NULL AND plate != '' ORDER BY id").fetchall()


def _checks(db):
    vehicles = _vehicles(db)
    out = diagnose.sql_checks(db, vehicles, _get_charge_efficiency(db))
    for v in vehicles:
        try:
            tr = _capacity_trend(db, v["plate"])
            nominal = fm.batteries(db, v["plate"])[-1]["nominal"]
            out.append(diagnose.capacity_check(v["plate"], nominal, tr))
            ch = tr.get("battery_change")
            if ch:
                out.append(diagnose._check("battery_change_suspected", "warn",
                                           f"{ch['before']} → {ch['after']} kWh", v["plate"],
                                           detail=ch["date"]))
        except Exception as e:
            out.append(diagnose._check("capacity_trend", "error", str(e)[:200], v["plate"]))
        if v["device"]:
            try:
                out.append(diagnose.energy_mismatch_check(
                    db, v["plate"], v["device"],
                    lambda ts, dev=v["device"]: get_bat_kwh(db, dev, ts)))
            except Exception as e:
                out.append(diagnose._check("trips_energy_mismatch", "error", str(e)[:200], v["plate"]))
        try:
            gaps = _find_odo_gaps(db, v["plate"], min_km=2)
            # Without a trip before it the "gap" is only the odometer at the
            # first recorded trip, not an outage.
            open_gaps = [g for g in gaps if g.get("prev_trip_id")
                         and (g.get("assigned_km") or 0) < g.get("km", 0) - 1]
            out.append(diagnose._check("odometer_gaps", "warn" if open_gaps else "ok",
                                       len(open_gaps), v["plate"],
                                       detail=f"{round(sum(g['km'] for g in open_gaps))} km"))
        except Exception as e:
            out.append(diagnose._check("odometer_gaps", "error", str(e)[:200], v["plate"]))
    out.append(diagnose.meter_overlap_check(db))
    client = detector.get_influx(timeout_ms=15000)
    try:
        out += diagnose.influx_checks(client, config.INFLUX_BUCKET, config.INFLUX_ORG,
                                      [v["device"] for v in vehicles if v["device"]])
    finally:
        if client:
            client.close()
    out.append(diagnose.log_check())
    return out


@diagnostics_bp.route("/api/admin/diagnostics/checks")
@admin_required
def diagnostics_checks():
    db = get_db()
    try:
        checks = _checks(db)
        mig = db.execute("PRAGMA user_version").fetchone()[0]
    finally:
        db.close()
    env = diagnose.environment()
    env["migration"] = mig
    env["migration_latest"] = migrations.SCHEMA_VERSION
    db = get_db()
    try:
        acked = db.execute("SELECT COUNT(*) FROM diag_ack").fetchone()[0]
    except Exception:
        acked = 0
    finally:
        db.close()
    return jsonify({"env": env, "checks": checks, "acked": acked})


TRIP_EDIT_FIELDS = ("start_time", "end_time", "odo_start", "odo_end", "soc_start", "soc_end")
_TRIP_SIDE = {"start_time": "start", "odo_start": "start", "soc_start": "start",
              "end_time": "end", "odo_end": "end", "soc_end": "end"}


def _trip_view(r):
    return {k: r[k] for k in ("id", "start_time", "end_time", "odo_start", "odo_end", "distance_km",
                              "soc_start", "soc_end", "energy_kwh", "consumption", "start_address",
                              "end_address", "is_manual", "manual_fields")}


@diagnostics_bp.route("/api/admin/diagnostics/trip/<int:trip_id>")
@admin_required
def diagnostics_trip(trip_id):
    """A trip of a finding with the trips before and after it (same car), so
    times and odometer can be fixed against their neighbours."""
    db = get_db()
    try:
        t = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
        if not t:
            return jsonify({"error": "not found"}), 404
        # Neighbours by odometer when there is one: a finding is often a
        # broken time, so the time is no guide to the trips around it.
        if t["odo_start"] is not None and t["odo_end"] is not None:
            prev = db.execute("SELECT * FROM trips WHERE device = ? AND id != ? AND odo_end <= ? + 1 "
                              "ORDER BY odo_end DESC, start_time DESC LIMIT 1",
                              (t["device"], t["id"], t["odo_start"])).fetchone()
            nxt = db.execute("SELECT * FROM trips WHERE device = ? AND id != ? AND odo_start >= ? - 1 "
                             "ORDER BY odo_start, start_time LIMIT 1",
                             (t["device"], t["id"], t["odo_end"])).fetchone()
        else:
            prev = db.execute("SELECT * FROM trips WHERE device = ? AND id != ? AND start_time <= ? "
                              "ORDER BY start_time DESC LIMIT 1", (t["device"], t["id"], t["start_time"])).fetchone()
            nxt = db.execute("SELECT * FROM trips WHERE device = ? AND id != ? AND start_time >= ? "
                             "ORDER BY start_time LIMIT 1", (t["device"], t["id"], t["start_time"])).fetchone()
        return jsonify({"trip": _trip_view(t), "prev": _trip_view(prev) if prev else None,
                        "next": _trip_view(nxt) if nxt else None})
    finally:
        db.close()


@diagnostics_bp.route("/api/admin/diagnostics/trip/<int:trip_id>", methods=["POST"])
@admin_required
def diagnostics_trip_save(trip_id):
    """Times, odometer and SoC of a trip, fixed by hand. Distance, energy
    and consumption follow; the changed side is locked against a rescan."""
    from blueprints.admin import _norm_ts
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        t = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
        if not t:
            return jsonify({"error": "not found"}), 404
        new = {k: t[k] for k in TRIP_EDIT_FIELDS}
        try:
            for k in TRIP_EDIT_FIELDS:
                if k not in data:
                    continue
                v = data[k]
                if k.endswith("_time"):
                    v = _norm_ts(v)
                    if v is None:
                        raise ValueError(k)
                else:
                    v = None if v in ("", None) else float(v)
                    if v is not None and k.startswith("soc") and not 0 <= v <= 100:
                        raise ValueError(k)
                new[k] = v
        except (TypeError, ValueError) as e:
            return jsonify({"error": f"invalid {e}"}), 400
        if new["end_time"] <= new["start_time"]:
            return jsonify({"error": "end_time"}), 400
        if new["odo_start"] is not None and new["odo_end"] is not None and new["odo_end"] < new["odo_start"]:
            return jsonify({"error": "odo_end"}), 400
        changed = [k for k in TRIP_EDIT_FIELDS if new[k] != t[k]]
        if not changed:
            return jsonify({"ok": True, "trip": _trip_view(t)})
        dist = t["distance_km"]
        if new["odo_start"] is not None and new["odo_end"] is not None:
            dist = round(new["odo_end"] - new["odo_start"], 1)
        energy = t["energy_kwh"]
        if any(k.startswith("soc") for k in changed) or "start_time" in changed:
            energy = None
            if new["soc_start"] is not None and new["soc_end"] is not None and new["soc_start"] > new["soc_end"]:
                cap = get_bat_kwh(db, t["device"], at_time=new["start_time"])
                energy = round((new["soc_start"] - new["soc_end"]) / 100 * cap, 2) if cap else None
        locked = {f for f in (t["manual_fields"] or "").split(",") if f} | {_TRIP_SIDE[k] for k in changed}
        db.execute("UPDATE trips SET start_time = ?, end_time = ?, odo_start = ?, odo_end = ?, soc_start = ?, "
                   "soc_end = ?, distance_km = ?, energy_kwh = ?, consumption = ?, manual_fields = ? WHERE id = ?",
                   (new["start_time"], new["end_time"], new["odo_start"], new["odo_end"], new["soc_start"],
                    new["soc_end"], dist, energy, fm.trip_consumption(energy, dist), ",".join(sorted(locked)), trip_id))
        db.commit()
        log.info("diagnostics: trip %s fixed by hand (%s)", trip_id, ", ".join(changed))
        return jsonify({"ok": True, "trip": _trip_view(db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone())})
    finally:
        db.close()


@diagnostics_bp.route("/api/admin/diagnostics/trip/<int:trip_id>", methods=["DELETE"])
@admin_required
def diagnostics_trip_delete(trip_id):
    """Delete a trip of any vehicle (a telemetry dropout, a double)."""
    db = get_db()
    try:
        db.execute("DELETE FROM gpx_waypoints WHERE trip_id = ?", (trip_id,))
        db.execute("DELETE FROM journey_trips WHERE trip_id = ?", (trip_id,))
        n = db.execute("DELETE FROM trips WHERE id = ?", (trip_id,)).rowcount
        db.commit()
        log.info("diagnostics: trip %s deleted", trip_id)
        return jsonify({"ok": bool(n)})
    finally:
        db.close()


@diagnostics_bp.route("/api/admin/diagnostics/charge/<int:session_id>")
@admin_required
def diagnostics_charge(session_id):
    db = get_db()
    try:
        c = db.execute("SELECT id, vehicle_plate, session_number, start_time, end_time, total_kwh, soc_start, "
                       "soc_end, odometer, is_external, location_name, note FROM charge_sessions WHERE id = ?",
                       (session_id,)).fetchone()
        return jsonify({"charge": dict(c)}) if c else (jsonify({"error": "not found"}), 404)
    finally:
        db.close()


@diagnostics_bp.route("/api/admin/diagnostics/ack", methods=["POST"])
@admin_required
def diagnostics_ack():
    """{key, id}: mark a finding as checked (kept as it is); {key, id,
    undo: true} takes it back; {reset: true} clears all marks."""
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        if data.get("reset"):
            n = db.execute("DELETE FROM diag_ack").rowcount
        else:
            key, rid = str(data.get("key") or ""), data.get("id")
            if not key or not isinstance(rid, int):
                return jsonify({"error": "key/id"}), 400
            if data.get("undo"):
                n = db.execute("DELETE FROM diag_ack WHERE check_key = ? AND ref_id = ?", (key, rid)).rowcount
            else:
                n = db.execute("INSERT OR IGNORE INTO diag_ack (check_key, ref_id) VALUES (?, ?)", (key, rid)).rowcount
        db.commit()
        return jsonify({"ok": True, "n": n,
                        "total": db.execute("SELECT COUNT(*) FROM diag_ack").fetchone()[0]})
    finally:
        db.close()


@diagnostics_bp.route("/api/admin/diagnostics/log")
@admin_required
def diagnostics_log():
    level = (request.args.get("level") or "WARNING").upper()
    return jsonify({"lines": diagnose.log_lines(level, limit=int(request.args.get("limit", 300)))})


def _buffer(device):
    try:
        from blueprints.model import buffer_view
        return buffer_view(device)
    except Exception as e:      # optional, Influx may be away
        log.warning("diagnostics: buffer watch: %s", e)
        return None


def _model(db, vehicles):
    """Parameters of the calculations as they are right now."""
    per_vehicle = {}
    for v in vehicles:
        tr = _capacity_trend(db, v["plate"]) or {}
        per_vehicle[v["plate"]] = {
            "device": v["device"], "model": v["model"],
            "capacity_anchor_kwh": v["battery_capacity_kwh"],
            "capacity_anchor_used": get_bat_kwh(db, v["device"]),
            "capacity_trend": {k: tr.get(k) for k in
                               ("ok", "n", "n_reconstructed", "cap_now", "slope_per_year")},
            "capacity_source": tr.get("source"),
            "batteries": tr.get("segments"),
            "battery_change_suspected": tr.get("battery_change"),
            "charge_efficiency": _get_charge_efficiency(db, v["plate"]),
            "soc_profile": tr.get("soc_profile"),
            "dc_offset": tr.get("dc_offset"),
            "buffer_watch": _buffer(v["device"]),
        }
    consts = {k: getattr(app_module, k) for k in dir(app_module)
              if k.startswith(("_CAP", "_CHARGE", "_WPK")) and k.isupper()
              and isinstance(getattr(app_module, k), (int, float))}
    return {
        "charge_grid_efficiency": _get_charge_efficiency(db),
        "constants": consts,
        "trip_check": {k: getattr(trip_check, k) for k in dir(trip_check)
                       if k.isupper() and isinstance(getattr(trip_check, k), (int, float))},
        "detector": {k: getattr(config, k) for k in (
            "TRIP_MIN_DISTANCE_KM", "TRIP_STOP_MINUTES", "SCAN_INTERVAL_MINUTES",
            "DATA_GAP_MINUTES", "SOC_JUMP_MIN", "SOC_JUMP_STILL_KMH", "GPS_MAX_KMH",
            "MERGE_GAP_MIN", "TRIP_ODO_GAP_KM") if hasattr(config, k)},
        "vehicles": per_vehicle,
    }


def _trips_derived(db, v, since, until):
    rows = db.execute(
        "SELECT * FROM trips WHERE device = ? AND start_time >= ? AND start_time < ? "
        "ORDER BY start_time", (v["device"], since, until)).fetchall()
    overl = {r[0] for r in db.execute(
        "SELECT a.id FROM trips a JOIN trips b ON b.device = a.device AND b.id <> a.id "
        "AND b.start_time < a.end_time AND b.end_time > a.start_time "
        "WHERE a.device = ? AND a.start_time >= ? AND a.start_time < ?",
        (v["device"], since, until))}
    out = []
    for t in rows:
        km = t["distance_km"]
        dsoc = (t["soc_start"] - t["soc_end"]) if t["soc_start"] is not None and t["soc_end"] is not None else None
        cap = get_bat_kwh(db, v["device"], t["start_time"]) if t["start_time"] else None
        e_soc = round(dsoc / 100 * cap, 2) if dsoc is not None and cap else None
        chk = trip_check.check_trip(t, overlaps=t["id"] in overl)
        out.append({
            "id": t["id"], "plate": v["plate"], "start_time": t["start_time"], "end_time": t["end_time"],
            "distance_km": km, "odo_start": t["odo_start"], "odo_end": t["odo_end"],
            "soc_start": t["soc_start"], "soc_end": t["soc_end"], "soc_delta": dsoc,
            "energy_kwh": t["energy_kwh"], "consumption_stored": t["consumption"],
            "capacity_at_trip_kwh": cap, "energy_from_soc_kwh": e_soc,
            "consumption_from_energy": round(t["energy_kwh"] / km * 100, 1)
            if t["energy_kwh"] is not None and km and km >= 1 else None,
            "consumption_from_soc": round(e_soc / km * 100, 1) if e_soc is not None and km and km >= 1 else None,
            "kw_start": t["kw_start"], "kw_end": t["kw_end"],
            "is_manual": t["is_manual"], "manual_fields": t["manual_fields"], "is_gpx": t["is_gpx"],
            "check_time": chk["time"], "check_speed_kmh": chk["speed"], "check_consumption": chk["cons_state"],
            "purpose": t["purpose"],
        })
    return out


def _snapshot(zo, plate, d0, d1, user_id):
    """calc/api/*.json: the analysis endpoints as the active vehicle shows them."""
    client = app_module.app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
    for ep in SNAPSHOT:
        url = ep.format(plate=plate)
        url += ("&" if "?" in url else "?") + f"from={d0}&to={d1}"
        name = "calc/api/" + ep.split("?")[0].strip("/").replace("/", "_").replace("{plate}", "plate") + ".json"

        def run(url=url):
            r = client.get(url)
            body = r.get_json(silent=True)
            return {"url": url, "status": r.status_code,
                    "data": body if body is not None else r.get_data(as_text=True)[:2000]}
        res = zo.safe(name, run)
        if res is not None:
            zo.json(name, res)


def _export_params(args):
    """Query/JSON options → export parameters (range, parts, resolution)."""
    d0, d1, since, until = _range(args)
    days = (d1 - d0).days + 1
    try:
        window = int(args.get("window", "10"))
    except (TypeError, ValueError):
        window = 10
    if window == 0 and days > RAW_MAX_DAYS:
        window = 10
    if window < 60 and days > FINE_MAX_DAYS:
        window = 60
    on = lambda k, d="1": str(args.get(k, d)) == "1"
    return {"d0": d0, "d1": d1, "since": since, "until": until, "window": window,
            "anon": on("anon", "0"), "calc": on("calc"), "tele": on("telemetry"),
            "long": on("longterm"), "full": {"charge_readings"} if on("readings_all") else set()}


def _build_export(p, user_id, active_plate, zo):
    """Fill the ZIP: tables, settings, checks, model, derived figures,
    endpoint snapshot, telemetry, log, manifest."""
    d0, d1, since, until = p["d0"], p["d1"], p["since"], p["until"]
    anon, full, window = p["anon"], p["full"], p["window"]
    db = get_db()
    try:
        vehicles = _vehicles(db)
        for tbl in diagnose.table_names(db):
            res = zo.safe(f"data/{tbl}.csv", lambda tbl=tbl: diagnose.table_csv(db, tbl, since, until, anon, full))
            if res:
                zo.text(f"data/{tbl}.csv", res[0], res[1])
        zo.json("settings.json", diagnose.redact_settings(
            db.execute("SELECT key, value FROM settings ORDER BY key").fetchall(), ENCRYPTED_SETTINGS))
        zo.json("config.json", diagnose.config_snapshot())
        checks = zo.safe("checks.json", lambda: _checks(db))
        if checks is not None:
            zo.json("checks.json", checks)
        model = zo.safe("model.json", lambda: _model(db, vehicles))
        if model is not None:
            zo.json("model.json", model)

        if p["calc"]:
            derived = []
            for v in vehicles:
                if v["device"]:
                    part = zo.safe(f"calc/trips_derived ({v['plate']})",
                                   lambda v=v: _trips_derived(db, v, since, until))
                    derived += part or []
                pts = zo.safe(f"calc/capacity_{v['plate']}.csv", lambda v=v: _capacity_points(db, v["plate"]))
                if pts:
                    zo.text(f"calc/capacity_{v['plate']}.csv", diagnose.rows_csv(pts[0]), len(pts[0]))
                rc = zo.safe(f"calc/real_consumption_{v['plate']}.json",
                             lambda v=v: _real_consumption(db, v["plate"], since, until))
                zo.json(f"calc/real_consumption_{v['plate']}.json", rc)

                def gaps(v=v):
                    g = _find_odo_gaps(db, v["plate"], min_km=2, date_from=since, date_to=d1.isoformat())
                    _annotate_gaps(db, v["plate"], v["device"], g)
                    return g
                g = zo.safe(f"calc/gaps_{v['plate']}.json", gaps)
                if g is not None:
                    zo.json(f"calc/gaps_{v['plate']}.json", g)
            zo.text("calc/trips_derived.csv", diagnose.rows_csv(derived), len(derived))
            if active_plate:
                _snapshot(zo, active_plate, d0.isoformat(), d1.isoformat(), user_id)

        if p["tele"] or p["long"]:
            client = detector.get_influx(timeout_ms=600000)
            if client:
                try:
                    for v in vehicles if p["long"] else []:
                        if not v["device"]:
                            continue
                        name = f"telemetry_hourly/{v['device']}.csv"
                        res = zo.safe(name, lambda v=v: diagnose.telemetry_hourly_csv(
                            client, config.INFLUX_BUCKET, config.INFLUX_ORG, v["device"]))
                        if res:
                            zo.text(name, res[0], res[1])
                    tz = detector.LOCAL_TZ
                    s_utc = datetime(d0.year, d0.month, d0.day, tzinfo=tz)
                    e_utc = datetime(d1.year, d1.month, d1.day, tzinfo=tz) + timedelta(days=1)
                    for v in vehicles if p["tele"] else []:
                        if not v["device"]:
                            continue
                        name = f"telemetry/{v['device']}.csv"
                        res = zo.safe(name, lambda v=v: diagnose.telemetry_csv(
                            client, config.INFLUX_BUCKET, config.INFLUX_ORG, v["device"],
                            s_utc, e_utc, window, anon))
                        if res:
                            zo.text(name, res[0], res[1])
                finally:
                    client.close()

        zo.text("logs/recent.jsonl", "\n".join(
            json.dumps(r, ensure_ascii=False) for r in diagnose.log_lines("DEBUG", 3000)))
        mig = db.execute("PRAGMA user_version").fetchone()[0]
    finally:
        db.close()

    env = diagnose.environment()
    zo.text("README.txt", diagnose.README)
    zo.json("manifest.json", {
        **env, "migration": mig, "migration_latest": migrations.SCHEMA_VERSION,
        "range": {"from": d0.isoformat(), "to": d1.isoformat()},
        "options": {"anon": anon, "calc": p["calc"], "telemetry": p["tele"], "window_s": window,
                    "longterm": p["long"], "readings_all": bool(full)},
        "vehicles": [dict(v) for v in vehicles],
        "influx_fields": _ALL_FIELDS,
        "files": zo.files,
        "errors": len(zo.errors),
    })
    return zo.close()


def _export_name():
    return f"idmate-diagnose-{config.VERSION}-{date.today().isoformat()}.zip"


def _active_plate():
    db = get_db()
    try:
        av = active_vehicle(db)
        return av["plate"] if av else None
    finally:
        db.close()


@diagnostics_bp.route("/api/admin/diagnostics/export")
@admin_required
def diagnostics_export():
    """Synchronous export (small ranges). Large ones go through the job below:
    a reverse proxy cuts requests after ~60 s (504)."""
    data = _build_export(_export_params(request.args), current_user.get_id(), _active_plate(),
                         diagnose.ZipOut())
    return Response(data, mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{_export_name()}"',
                             "Cache-Control": "no-store"})


# ── Export as a background job ─────────────────────────────────────────────

_job_lock = threading.Lock()
_job = {}              # id, state (running|done|error), step, started, finished, size, error, path, name
JOB_KEEP_S = 6 * 3600  # a finished file stays downloadable this long


def _job_dir():
    d = os.path.join(os.path.dirname(os.path.abspath(config.DB_PATH)), "diagnose")
    os.makedirs(d, exist_ok=True)
    return d


def _job_public():
    j = {k: v for k, v in _job.items() if k != "path"}
    if j.get("started"):
        j["elapsed_s"] = int((j.get("finished") or time.time()) - j["started"])
    return j


def _drop_old_file():
    path = _job.get("path")
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _run_job(job_id, params, user_id, active_plate, path):
    def progress(name):
        with _job_lock:
            if _job.get("id") == job_id:
                _job["step"] = name
    try:
        with app_module.app.app_context():
            _build_export(params, user_id, active_plate, diagnose.ZipOut(path, progress))
        with _job_lock:
            if _job.get("id") == job_id:
                _job.update(state="done", step=None, finished=time.time(), size=os.path.getsize(path))
    except Exception as e:  # report it on the page instead of a silent thread death
        log.exception("diagnose export job failed")
        with _job_lock:
            if _job.get("id") == job_id:
                _job.update(state="error", error=f"{type(e).__name__}: {e}", finished=time.time())


@diagnostics_bp.route("/api/admin/diagnostics/export/start", methods=["POST"])
@admin_required
def diagnostics_export_start():
    args = {**request.args, **(request.get_json(silent=True) or {})}
    with _job_lock:
        if _job.get("state") == "running":
            return jsonify(_job_public())
        _drop_old_file()
        job_id = secrets.token_hex(8)
        path = os.path.join(_job_dir(), f"export-{job_id}.zip")
        _job.clear()
        _job.update(id=job_id, state="running", step=None, started=time.time(),
                    finished=None, size=None, error=None, path=path, name=_export_name())
    threading.Thread(target=_run_job, name="diagnose-export", daemon=True,
                     args=(job_id, _export_params(args), current_user.get_id(), _active_plate(), path)).start()
    return jsonify(_job_public())


@diagnostics_bp.route("/api/admin/diagnostics/export/status")
@admin_required
def diagnostics_export_status():
    with _job_lock:
        if _job.get("state") == "done" and time.time() - (_job.get("finished") or 0) > JOB_KEEP_S:
            _drop_old_file()
            _job.clear()
        return jsonify(_job_public())


@diagnostics_bp.route("/api/admin/diagnostics/export/file")
@admin_required
def diagnostics_export_file():
    with _job_lock:
        path, name, state = _job.get("path"), _job.get("name"), _job.get("state")
    if state != "done" or not path or not os.path.exists(path):
        return jsonify({"error": "no export ready"}), 404
    return send_file(path, mimetype="application/zip", as_attachment=True, download_name=name,
                     max_age=0)
