"""Trip routes: list/detail, edit/merge/split, delete, CSV+GPX import/export
(FIXES 6.1).

Moved verbatim out of app.py (``@app.route`` → ``@trips_bp.route``; internal
``url_for("trips_list")`` → ``url_for("trips.trips_list")``). Endpoint names gain
the ``trips.`` prefix; templates use hardcoded paths so they are unaffected.
Note: ``clear_trip_address`` stays in app.py (it lives in the geocode cluster).
"""

import csv
from urllib.parse import quote
import io
import threading
from datetime import datetime, timedelta, timezone

import config
import fahrzeugmodell as fm
import gpxexport
import detector
import geocoder as geo
from flask import (Blueprint, Response, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_login import current_user, login_required

from app import (GPX_MAX_DAYS, _LOCAL_TZ, _parse_coord, _parse_local,
                 _parse_num, _plmn_name, _real_consumption, _to_rfc3339,
                 _to_rfc3339_padded, _translations, _trip_telemetry_points,
                 active_device, active_vehicle, admin_required, effective_date_range,
                 get_bat_kwh, get_db, get_language, get_preset_values,
                 get_purpose_meta, haversine_m, log, match_location)
from journey_sync import repair_orphan_journey_devices, sync_journey_trips
import trip_check

trips_bp = Blueprint("trips", __name__)


@trips_bp.route("/trips")
def trips_list():
    db = get_db()
    device = active_device()

    filter_mode = request.args.get("filter", "")
    location_filter = request.args.get("location", "").strip()

    # Shared global range: explicit URL params override and re-stamp the session;
    # otherwise default to the last 30 days, auto-reverting after 60 min (perf).
    if request.args.get("from") or request.args.get("to"):
        date_from = request.args.get("from", "")
        date_to = request.args.get("to", "")
        session["date_from"] = date_from
        session["date_to"] = date_to
        session["date_set_at"] = datetime.now().isoformat()
    else:
        date_from, date_to = effective_date_range()

    # Build the query from independent, combinable conditions. A purpose or
    # location filter spans all history (no date limit) so the user can assign
    # e.g. every past trip to/from one geofence in one go.
    where = ["device = ?"]
    params = [device]
    if filter_mode == "uncategorized":
        where.append("(purpose IS NULL OR purpose = '')")
    elif filter_mode:
        where.append("purpose = ?")
        params.append(filter_mode)
    if location_filter:
        where.append("(start_address = ? OR end_address = ?)")
        params.extend([location_filter, location_filter])

    use_limit = False
    if not filter_mode and not location_filter:
        if date_from and date_to:
            where.append("start_time >= ? AND start_time <= ?")
            params.extend([date_from, date_to + "T23:59:59"])
        else:
            use_limit = True

    sql = f"SELECT * FROM trips WHERE {' AND '.join(where)} ORDER BY start_time DESC"
    if use_limit:
        sql += " LIMIT 20"
    trips = db.execute(sql, params).fetchall()

    # Filter out spurious micro-trips (< 1 km) — usually log gaps masquerading
    # as trips. Show as a count above the table so they're not silently gone.
    # Opt-in toggle via ?show_short=1 to surface them anyway.
    show_short = request.args.get("show_short") == "1"
    short_hidden = 0
    if not show_short:
        kept = []
        for t in trips:
            d = t["distance_km"]
            if d is not None and d < 1 and not t["is_manual"]:
                short_hidden += 1
            else:
                kept.append(t)
        trips = kept

    # Trips filled in by hand or corrected in the gap editor: red asterisk
    # with a tooltip saying whether their times and consumption are plausible.
    _tt = _translations.get(get_language(), _translations["DE"])
    flags = {}
    for t in trips:
        if not trip_check.is_flagged(t):
            continue
        overlaps = db.execute(
            """SELECT 1 FROM trips WHERE device = ? AND id <> ?
               AND datetime(start_time) < datetime(?)
               AND datetime(end_time)   > datetime(?) LIMIT 1""",
            (t["device"], t["id"], t["end_time"], t["start_time"])).fetchone() is not None
        chk = trip_check.check_trip(t, overlaps=overlaps)
        flags[t["id"]] = {"title": trip_check.flag_title(t, chk, _tt), "ok": chk["ok"]}

    # Count uncategorized trips (purpose empty or NULL)
    uncategorized = db.execute(
        """SELECT COUNT(*) AS c FROM trips WHERE device = ?
           AND (purpose IS NULL OR purpose = '')""",
        (device,),
    ).fetchone()["c"]

    # Private purposes for template
    priv_names = {r["name"] for r in db.execute(
        "SELECT name FROM purpose_meta WHERE is_private = 1"
    ).fetchall()}

    # Geofence names for the location filter dropdown
    location_options = [r["name"] for r in db.execute(
        "SELECT name FROM locations WHERE alias_of IS NULL AND keep_address = 0 ORDER BY name COLLATE NOCASE"
    ).fetchall()]

    # Charge-anchored "Ø real" (same helper as charges/analysis) for the header.
    _pr = db.execute("SELECT plate FROM vehicles WHERE device = ?", (device,)).fetchone()
    real = _real_consumption(db, _pr["plate"], date_from or None, date_to or None) if _pr and _pr["plate"] else None

    db.close()
    return render_template("trips.html", trips=trips,
                           date_from=date_from, date_to=date_to,
                           filter_mode=filter_mode,
                           location_filter=location_filter,
                           location_options=location_options,
                           uncategorized=uncategorized,
                           short_hidden=short_hidden,
                           show_short=show_short,
                           private_purposes=priv_names,
                           flags=flags,
                           real=real)


# ── Visits view: away from home / on site ─────────────────────────────────

@trips_bp.route("/trips/visits")
def trips_visits():
    """Toggle "Besucht" of the trip log: per outing from leaving home to coming
    back, with the on-site blocks (stops within 10 km) inside. For estimating
    and billing working and project hours. Logic in besuche.py."""
    import besuche
    db = get_db()
    device = active_device()
    if request.args.get("from") or request.args.get("to"):
        date_from, date_to = request.args.get("from", ""), request.args.get("to", "")
        session["date_from"], session["date_to"] = date_from, date_to
        session["date_set_at"] = datetime.now().isoformat()
    else:
        date_from, date_to = effective_date_range()
    # One day of margin on both sides, so an outing across the range edge
    # (out at 23:50, back at 08:04) is complete.
    lo = (datetime.strptime(date_from, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d") if date_from else "0000"
    hi = (datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d") if date_to else "9999"
    trips = [dict(r) for r in db.execute(
        "SELECT * FROM trips WHERE device = ? AND start_time >= ? AND start_time < ? ORDER BY start_time",
        (device, lo, hi))]
    v = db.execute("SELECT plate FROM vehicles WHERE device = ?", (device,)).fetchone()
    charges = [dict(r) for r in db.execute(
        "SELECT start_time, end_time FROM charge_sessions WHERE vehicle_plate = ? AND start_time >= ? AND start_time < ?",
        (v["plate"] if v else "", lo, hi))]
    points, names = besuche.home_points(db)
    uid = current_user.id if current_user.is_authenticated else None
    colors = {m["name"]: m["color"] for m in get_purpose_meta(db, user_id=uid, device=device)}
    db.close()

    outings = [o for o in besuche.build_outings(trips, charges, points, names)
               if o["start"] and (not date_from or o["start"].strftime("%Y-%m-%d") >= date_from)
               and (not date_to or o["start"].strftime("%Y-%m-%d") <= date_to)]

    # Purpose filter (e.g. "Büro GTU" + "Dienstfahrt" to match a project's
    # proof of work): ?p=A&p=B, ?all=1 clears; kept in the session so the
    # date picker's reload keeps it.
    if "all" in request.args:
        session.pop("visits_purposes", None)
    elif request.args.getlist("p"):
        session["visits_purposes"] = [x for x in request.args.getlist("p") if x is not None]
    selected = session.get("visits_purposes") or []

    # Totals per purpose over ALL outings of the range: they double as the
    # filter chips (time on site and days with time on site).
    totals = {}
    for o in outings:
        for s in o["sites"]:
            tt = totals.setdefault(s["purpose"] or "", {"minutes": 0, "days": set()})
            tt["minutes"] += s["minutes"] or 0
            if s["start"]:
                tt["days"].add(s["start"].date())
    totals = sorted(({"purpose": k, "minutes": x["minutes"], "days": len(x["days"])}
                     for k, x in totals.items()), key=lambda x: -x["minutes"])
    sel = set(selected)
    for x in totals:
        x["on"] = x["purpose"] in sel
        nxt = sorted(sel ^ {x["purpose"]})
        x["href"] = "/trips/visits?" + ("&".join("p=" + quote(v) for v in nxt) if nxt else "all=1")

    if sel:
        shown = []
        for o in outings:
            sites = [st for st in o["sites"] if st["purpose"] in sel]
            if not sites and not (not o["sites"] and o["purpose"] in sel):
                continue
            addresses = []
            for st in sites:
                addresses += [a for a in st["addresses"] if a not in addresses]
            shown.append({**o, "sites": sites, "addresses": addresses or o["addresses"]})
        outings = shown
    onsite = sum(st["minutes"] or 0 for o in outings for st in o["sites"])
    days = len({st["start"].date() for o in outings for st in o["sites"] if st["start"]})
    return render_template("trips_visits.html", outings=outings, totals=totals, colors=colors,
                           date_from=date_from, date_to=date_to, selected=selected,
                           onsite_minutes=onsite, onsite_days=days,
                           away_minutes=sum(o["minutes"] or 0 for o in outings))


# ── Monthly logbook (print page + server-rendered PDF) ─────────────────────

def _logbuch_monat():
    """(year, month) from ?year=&month=, default: the month before this one
    (the finished month a logbook is printed for)."""
    heute = datetime.now()
    try:
        jahr = int(request.args.get("year") or 0)
        monat = int(request.args.get("month") or 0)
    except ValueError:
        jahr = monat = 0
    if not (2000 <= jahr <= 2100 and 1 <= monat <= 12):
        jahr, monat = (heute.year, heute.month - 1) if heute.month > 1 else (heute.year - 1, 12)
    return jahr, monat


def _logbuch_kopf(vehicle, jahr, monat, t):
    import calendar as _cal
    monate = t.get("logbook_months") or ["", "Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
                                           "August", "September", "Oktober", "November", "Dezember"]
    name = (vehicle["name"] or "") if vehicle else ""
    plate = vehicle["plate"] if vehicle else ""
    return {"titel": t["logbook_title"], "monat": f"{monate[monat]} {jahr}",
            "fahrzeug": " · ".join(x for x in (plate, name) if x), "kennzeichen": plate,
            "erstellt": datetime.now().strftime("%d.%m.%Y %H:%M"), "version": config.VERSION,
            "tage": _cal.monthrange(jahr, monat)[1]}


@trips_bp.route("/trips/logbook")
@login_required
def logbook_page():
    import fahrtenbuch
    t = _translations.get(get_language(), _translations["DE"])
    jahr, monat = _logbuch_monat()
    db = get_db()
    vehicle = active_vehicle(db)
    device = vehicle["device"] if vehicle and vehicle["device"] else active_device()
    daten = fahrtenbuch.monat_daten(db, device, jahr, monat)
    jahre = sorted({int(r[0]) for r in db.execute(
        "SELECT DISTINCT substr(start_time, 1, 4) FROM trips WHERE device = ? AND start_time GLOB '[0-9][0-9][0-9][0-9]*'",
        (device,))} | {datetime.now().year}, reverse=True)
    db.close()
    offen = sum(1 for r in daten["rows"] if r["art"] == "offen")
    return render_template("logbook.html", daten=daten, jahr=jahr, monat=monat, jahre=jahre,
                           kopf=_logbuch_kopf(vehicle, jahr, monat, t), offen=offen, fb=fahrtenbuch)


@trips_bp.route("/trips/logbook.pdf")
@login_required
def logbook_pdf():
    import fahrtenbuch
    import gpxexport
    t = _translations.get(get_language(), _translations["DE"])
    jahr, monat = _logbuch_monat()
    db = get_db()
    vehicle = active_vehicle(db)
    device = vehicle["device"] if vehicle and vehicle["device"] else active_device()
    daten = fahrtenbuch.monat_daten(db, device, jahr, monat)
    db.close()
    kopf = _logbuch_kopf(vehicle, jahr, monat, t)
    name = gpxexport.dateiname(t["logbook_title"], kopf["kennzeichen"], f"{jahr:04d}-{monat:02d}")[:-4] + ".pdf"
    return Response(fahrtenbuch.pdf_bytes(daten, kopf, t), mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={name}",
                             "Cache-Control": "no-store"})


@trips_bp.route("/api/fahrtenbuch/options")
def fahrtenbuch_options():
    """All selection options for the trip log."""
    db = get_db()
    locations = db.execute(
        "SELECT * FROM locations WHERE alias_of IS NULL AND keep_address = 0 ORDER BY name COLLATE NOCASE").fetchall()
    # FIXES 15.2: vehicle-driven tag visibility — only vocabulary that already
    # appeared on the active vehicle (+ pinned + assigned). Keeps the dropdown
    # small when there are several vehicles/users.
    device = active_device()
    uid = current_user.id if current_user.is_authenticated else None
    pmeta = get_purpose_meta(db, user_id=uid, device=device)
    destinations = get_preset_values(db, "destination", device=device, user_id=uid)
    visit_reasons = get_preset_values(db, "visit_reason", device=device, user_id=uid)
    # How often each value was used on this vehicle in the last 30 days — the
    # batch bar lists the most used ones first.
    since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    usage = {}
    for field in ("purpose", "destination", "visit_reason"):
        usage[field] = {r["v"]: r["n"] for r in db.execute(
            f"""SELECT {field} AS v, COUNT(*) AS n FROM trips
                WHERE device = ? AND start_time >= ? AND COALESCE({field}, '') <> ''
                GROUP BY {field}""", (device, since))}
    db.close()
    return jsonify({
        "usage_30d": usage,
        "purposes": [p["name"] for p in pmeta],
        "purpose_meta": pmeta,
        "destinations": destinations,
        "visit_reasons": visit_reasons,
        "private_purposes": [p["name"] for p in pmeta if p["is_private"]],
        "locations": [dict(r) for r in locations],
    })


@trips_bp.route("/api/trips/<int:trip_id>", methods=["POST"])
def update_trip(trip_id):
    data = request.get_json()
    db = get_db()

    allowed = ("purpose", "destination", "visit_reason", "note")
    sets = []
    params = []
    for field in allowed:
        if field in data:
            sets.append(f"{field} = ?")
            params.append(data[field])

    if not sets:
        _t = _translations.get(get_language(), _translations["DE"])
        return jsonify({"error": _t["err_no_fields"]}), 400

    params.append(trip_id)
    db.execute(f"UPDATE trips SET {', '.join(sets)} WHERE id = ?", params)
    db.commit()
    db.close()
    return jsonify({"ok": True, "id": trip_id})


@trips_bp.route("/api/trips/batch", methods=["POST"])
def batch_update():
    data = request.get_json()
    ids = data.get("ids", [])
    _t = _translations.get(get_language(), _translations["DE"])
    if not ids:
        return jsonify({"error": _t["analysis_no_trips_short"]}), 400

    db = get_db()
    allowed = ("purpose", "destination", "visit_reason")
    sets = []
    params_base = []
    for field in allowed:
        if field in data:
            sets.append(f"{field} = ?")
            v = data[field]
            params_base.append(v if v != "" else None)

    if not sets:
        return jsonify({"error": _t["err_no_fields"]}), 400

    for trip_id in ids:
        db.execute(f"UPDATE trips SET {', '.join(sets)} WHERE id = ?", params_base + [trip_id])
    db.commit()
    db.close()
    return jsonify({"ok": True, "count": len(ids)})


@trips_bp.route("/api/trips/<int:trip_id>/retag", methods=["POST"])
@login_required
def retag_trip(trip_id):
    """Re-detect destination/purpose from end coordinates."""
    db = get_db()
    trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if not trip:
        _t = _translations.get(get_language(), _translations["DE"])
        return jsonify({"error": _t["err_trip_not_found"]}), 404
    start_loc = detector.match_location(db, trip["start_lat"], trip["start_lon"])
    end_loc = detector.match_location(db, trip["end_lat"], trip["end_lon"])
    rule = detector.match_route_rule(db, start_loc, end_loc)
    if rule:
        dest = rule.get("destination") or (end_loc["name"] if end_loc else "")
        purp = rule["purpose"]
        vr = rule.get("visit_reason") or ""
    elif end_loc:
        dest = end_loc["name"]
        purp = ""
        vr = end_loc["default_reason"] or "" if end_loc["default_reason"] else ""
    else:
        dest = ""
        purp = ""
        vr = ""
    db.execute(
        "UPDATE trips SET destination = ?, purpose = ?, visit_reason = ? WHERE id = ?",
        (dest, purp, vr, trip_id),
    )
    db.commit()
    db.close()
    return jsonify({"ok": True, "destination": dest, "purpose": purp, "visit_reason": vr})


@trips_bp.route("/api/trips/merge", methods=["POST"])
@login_required
def merge_trips():
    data = request.get_json()
    ids = data.get("ids", [])
    _t = _translations.get(get_language(), _translations["DE"])
    if len(ids) != 2:
        return jsonify({"error": _t["trip_merge_need_two"]}), 400

    db = get_db()
    rows = [db.execute("SELECT * FROM trips WHERE id = ?", (i,)).fetchone() for i in ids]
    if any(r is None for r in rows):
        db.close()
        return jsonify({"error": _t["err_trip_not_found"]}), 404

    t = [dict(r) for r in rows]
    # Sort chronologically
    t.sort(key=lambda x: x["start_time"])
    a, b = t[0], t[1]

    if a["device"] != b["device"]:
        db.close()
        return jsonify({"error": _t["trip_merge_diff_vehicles"]}), 400

    # Distance: prefer odometer difference, otherwise sum
    dist = None
    if a.get("odo_start") is not None and b.get("odo_end") is not None:
        dist = round(b["odo_end"] - a["odo_start"], 1)
    elif a.get("distance_km") is not None and b.get("distance_km") is not None:
        dist = round(a["distance_km"] + b["distance_km"], 1)
    # Energy: prefer SoC difference of total distance
    energy = None
    soc_s = a.get("soc_start")
    soc_e = b.get("soc_end")
    if soc_s is not None and soc_e is not None and soc_s > soc_e:
        # Capacity of the battery at that time (vehicle model)
        bat_kwh = get_bat_kwh(db, a["device"], a["start_time"])
        energy = round((soc_s - soc_e) / 100.0 * bat_kwh, 2)
    elif a.get("energy_kwh") is not None and b.get("energy_kwh") is not None:
        energy = round(a["energy_kwh"] + b["energy_kwh"], 2)
    consumption = fm.trip_consumption(energy, dist)

    merged = {
        "device":       a["device"],
        "start_time":   a["start_time"],
        "end_time":     b["end_time"],
        "start_lat":    a["start_lat"],
        "start_lon":    a["start_lon"],
        "end_lat":      b["end_lat"],
        "end_lon":      b["end_lon"],
        "odo_start":    a.get("odo_start"),
        "odo_end":      b.get("odo_end"),
        "distance_km":  dist,
        "soc_start":    a.get("soc_start"),
        "soc_end":      b.get("soc_end"),
        "energy_kwh":   energy,
        "consumption":  consumption,
        "purpose":      b.get("purpose") or a.get("purpose") or "",
        "destination":  b.get("destination") or a.get("destination") or "",
        "visit_reason": b.get("visit_reason") or a.get("visit_reason") or "",
    }

    # Insert the merged trip first so new_id exists, then re-point the GPX
    # waypoints and journey memberships of both old trips onto new_id, and only
    # then delete the old trips. Order matters: gpx_waypoints/journey_trips have
    # FK ON DELETE CASCADE on trips(id), so deleting a/b first would wipe their
    # track data and journey links. Re-pointing beforehand preserves both and
    # avoids any FK violation (rows then reference new_id, not a/b).
    try:
        cur = db.execute(
            """INSERT INTO trips
               (device, start_time, end_time, start_lat, start_lon, end_lat, end_lon,
                odo_start, odo_end, distance_km, soc_start, soc_end, energy_kwh, consumption,
                purpose, destination, visit_reason)
               VALUES (:device, :start_time, :end_time, :start_lat, :start_lon, :end_lat, :end_lon,
                       :odo_start, :odo_end, :distance_km, :soc_start, :soc_end,
                       :energy_kwh, :consumption, :purpose, :destination, :visit_reason)""",
            merged,
        )
        new_id = cur.lastrowid

        # Re-hang GPX waypoints of both old trips onto the merged trip.
        db.execute(
            "UPDATE gpx_waypoints SET trip_id = ? WHERE trip_id IN (?, ?)",
            (new_id, a["id"], b["id"]),
        )
        # Renumber seq across the merged track by timestamp so the combined
        # track is ordered consistently (NULL timestamps sort last, stable).
        wps = db.execute(
            "SELECT id FROM gpx_waypoints WHERE trip_id = ? "
            "ORDER BY (timestamp IS NULL), timestamp, seq, id",
            (new_id,),
        ).fetchall()
        for new_seq, w in enumerate(wps):
            db.execute("UPDATE gpx_waypoints SET seq = ? WHERE id = ?", (new_seq, w["id"]))

        # Re-hang journey memberships, deduplicated onto the merged trip. A
        # journey that had removed BOTH halves by hand keeps the merge out too
        # (otherwise its auto-fill would pick the new trip up).
        db.execute(
            "INSERT OR IGNORE INTO journey_trips (journey_id, trip_id) "
            "SELECT DISTINCT journey_id, ? FROM journey_trips WHERE trip_id IN (?, ?)",
            (new_id, a["id"], b["id"]),
        )
        db.execute(
            "INSERT OR IGNORE INTO journey_trip_excluded (journey_id, trip_id) "
            "SELECT journey_id, ? FROM journey_trip_excluded WHERE trip_id IN (?, ?) "
            "GROUP BY journey_id HAVING COUNT(*) = 2",
            (new_id, a["id"], b["id"]),
        )

        # Now drop the old trips. CASCADE clears their remaining journey_trips
        # rows (the originals pointing at a/b); the new_id rows survive.
        db.execute("DELETE FROM trips WHERE id IN (?, ?)", (a["id"], b["id"]))
        db.commit()
    except Exception:
        db.rollback()
        db.close()
        raise
    db.close()
    return jsonify({"ok": True, "id": new_id})


@trips_bp.route("/api/trips/<int:trip_id>", methods=["DELETE"])
@admin_required
def delete_trip(trip_id):
    db = get_db()
    db.execute("DELETE FROM gpx_waypoints WHERE trip_id = ?", (trip_id,))
    db.execute("DELETE FROM journey_trips WHERE trip_id = ?", (trip_id,))
    db.execute("DELETE FROM trips WHERE id = ? AND device = ?", (trip_id, active_device()))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@trips_bp.route("/api/trips/delete-range", methods=["POST"])
@admin_required
def delete_trip_range():
    """Delete trips by ID range."""
    data = request.get_json()
    id_from = data.get("from")
    id_to = data.get("to")
    if id_from is None or id_to is None:
        _t = _translations.get(get_language(), _translations["DE"])
        return jsonify({"error": _t["trip_from_to_required"]}), 400
    db = get_db()
    db.execute("DELETE FROM gpx_waypoints WHERE trip_id IN (SELECT id FROM trips WHERE id >= ? AND id <= ? AND device = ?)",
               (id_from, id_to, active_device()))
    db.execute("DELETE FROM journey_trips WHERE trip_id IN (SELECT id FROM trips WHERE id >= ? AND id <= ? AND device = ?)",
               (id_from, id_to, active_device()))
    cur = db.execute(
        "DELETE FROM trips WHERE id >= ? AND id <= ? AND device = ?",
        (id_from, id_to, active_device()),
    )
    deleted = cur.rowcount
    db.commit()
    db.close()
    return jsonify({"ok": True, "deleted": deleted})


@trips_bp.route("/api/import", methods=["POST"])
@admin_required
def import_csv_upload():
    """Import trips from CSV upload.

    Expected columns:
      created, Datum, Uhrzeit Start, Uhrzeit Ende,
      Start LAT, Start LON, Ende LAT, Ende LON,
      Start ODO, Ende ODO, Start SOC, Ende SOC, Aussen Temp
    """
    device = active_device()
    f = request.files.get("file")
    if not f:
        _t = _translations.get(get_language(), _translations["DE"])
        return jsonify({"error": _t["err_no_file"]}), 400

    text = f.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    db = get_db()
    bat_kwh = get_bat_kwh(db, device)
    count = 0
    skipped = 0

    # Atomic bulk import: all rows commit together or none (with db: commits on
    # success, rolls back on any exception) — a malformed row mid-file no longer
    # leaves a half-imported set behind.
    with db:
        for row in reader:
            datum = row.get("Datum", "").strip()
            if not datum:
                continue

            # Parse date + time -> ISO 8601
            d, m, y = datum.split(".")
            start_h = row.get("Uhrzeit Start", "00:00").strip()
            end_h = row.get("Uhrzeit Ende", "00:00").strip()
            start_time = f"{y}-{m}-{d}T{start_h}:00"
            end_time = f"{y}-{m}-{d}T{end_h}:00"

            # Duplicate check
            existing = db.execute(
                "SELECT id FROM trips WHERE device = ? AND start_time = ?",
                (device, start_time),
            ).fetchone()
            if existing:
                skipped += 1
                continue

            # Coordinates
            start_lat = _parse_coord(row.get("Start LAT", ""))
            start_lon = _parse_coord(row.get("Start LON", ""))
            end_lat = _parse_coord(row.get("Ende LAT", ""))
            end_lon = _parse_coord(row.get("Ende LON", ""))

            # Distance from odometer
            odo_start = _parse_num(row.get("Start ODO", ""))
            odo_end = _parse_num(row.get("Ende ODO", ""))
            distance = round(odo_end - odo_start, 1) if odo_start is not None and odo_end is not None else None

            # SoC
            soc_start = _parse_num(row.get("Start SOC", ""))
            soc_end = _parse_num(row.get("Ende SOC", ""))

            # Estimate energy + consumption from SoC difference
            energy = None
            consumption = None
            if soc_start is not None and soc_end is not None and soc_start > soc_end:
                energy = round((soc_start - soc_end) / 100 * (get_bat_kwh(db, device, start_time) or bat_kwh), 2)
                consumption = fm.trip_consumption(energy, distance)

            db.execute(
                """INSERT INTO trips
                   (device, start_time, end_time,
                    start_lat, start_lon, end_lat, end_lon,
                    odo_start, odo_end,
                    distance_km, soc_start, soc_end, energy_kwh, consumption)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (device, start_time, end_time,
                 start_lat, start_lon, end_lat, end_lon,
                 odo_start, odo_end,
                 distance, soc_start, soc_end, energy, consumption),
            )
            count += 1

    # Journeys covering the imported days take their trips in right away.
    if count > 0:
        sync_journey_trips(db, device=device)

    # Trigger geocoding directly (background thread)
    if count > 0:
        import threading
        def _geocode():
            import geocoder as geo
            gdb = get_db()
            geo.geocode_trips(gdb)
            gdb.close()
        threading.Thread(target=_geocode, daemon=True).start()

    db.close()
    return jsonify({"ok": True, "imported": count, "skipped": skipped})


@trips_bp.route("/api/gpx-import", methods=["POST"])
@admin_required
def gpx_import():
    """Import GPX file (hikes, bike tours, etc.)."""
    # defusedxml hardens the parser against XML billion-laughs / quadratic-blowup
    # DoS and external-entity attacks. Drop-in replacement for stdlib ET.
    import defusedxml.ElementTree as ET

    _t = _translations.get(get_language(), _translations["DE"])
    f = request.files.get("file")
    if not f:
        return jsonify({"error": _t["err_no_file"]}), 400
    vehicle_plate = request.form.get("vehicle", "").strip()
    if not vehicle_plate:
        return jsonify({"error": _t["charges_no_vehicle"]}), 400

    try:
        raw = f.read().decode("utf-8-sig")
        root = ET.fromstring(raw)
    except Exception as e:
        return jsonify({"error": f"{_t['trip_gpx_parse_failed']}: {e}"}), 400

    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    # Fallback for files without namespace
    if root.tag == "gpx":
        ns = {"g": ""}

    def _find(el, tag):
        """Find child element with or without namespace."""
        r = el.find(f"g:{tag}", ns)
        if r is None:
            r = el.find(tag)
        return r

    def _findall(el, tag):
        r = el.findall(f"g:{tag}", ns)
        if not r:
            r = el.findall(tag)
        return r

    db = get_db()
    veh = db.execute("SELECT device, name FROM vehicles WHERE plate = ?",
                     (vehicle_plate,)).fetchone()
    if veh and veh["device"]:
        device = veh["device"]
    elif veh and veh["name"]:
        # Vehicle name as device (DB-compatible: lowercase, no special characters)
        import re
        device = re.sub(r"[^a-z0-9]+", "_", veh["name"].lower()).strip("_")
    else:
        device = "gpx"

    imported = 0
    # Atomic import: the derived device update, every trip insert and its
    # waypoint inserts share one transaction (with db: commits on success, rolls
    # back on any exception) — a failure mid-track no longer leaves a trip row
    # without its waypoints, or a device update without the matching trips.
    with db:
        if veh and not veh["device"] and veh["name"]:
            db.execute("UPDATE vehicles SET device = ? WHERE plate = ?", (device, vehicle_plate))
        for trk in _findall(root, "trk"):
            # Collect all points across segments
            waypoints = []
            for seg in _findall(trk, "trkseg"):
                for pt in _findall(seg, "trkpt"):
                    lat = float(pt.get("lat"))
                    lon = float(pt.get("lon"))
                    ts_el = _find(pt, "time")
                    ele_el = _find(pt, "ele")
                    spd_el = _find(pt, "speed")
                    # Extensions speed (Garmin etc.)
                    if spd_el is None:
                        ext = _find(pt, "extensions")
                        if ext is not None:
                            spd_el = ext.find(".//{*}speed")
                    wp = {
                        "lat": lat, "lon": lon,
                        "ts": ts_el.text.strip() if ts_el is not None else None,
                        "ele": float(ele_el.text) if ele_el is not None else None,
                        "speed": float(spd_el.text) if spd_el is not None else None,
                    }
                    waypoints.append(wp)

            if len(waypoints) < 2:
                continue

            first, last = waypoints[0], waypoints[-1]
            start_time = first["ts"] or "1970-01-01T00:00:00"
            end_time = last["ts"] or start_time

            # GPX <time> is UTC per spec. Convert to local (Europe/Berlin) wall-clock
            # to match the rest of the DB, which stores local time without offset.
            # Previously only the trailing "Z" was stripped, so the UTC wall-clock
            # landed in the DB as if it were local → GPX trips showed 1-2 h too early
            # and midnight drives slipped to the wrong day. Stays robust for
            # timestamps without "Z" (treated as already-local, offset preserved).
            def _gpx_to_local(ts):
                try:
                    norm = ts.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(norm)
                    if dt.tzinfo is None:
                        return dt.strftime("%Y-%m-%dT%H:%M:%S")
                    return dt.astimezone(_LOCAL_TZ).strftime("%Y-%m-%dT%H:%M:%S")
                except (ValueError, TypeError):
                    return ts.replace("Z", "")
            start_time = _gpx_to_local(start_time)
            end_time = _gpx_to_local(end_time)

            # Duplicate check
            dup = db.execute(
                "SELECT id FROM trips WHERE device = ? AND start_time = ? AND is_gpx = 1",
                (device, start_time)
            ).fetchone()
            if dup:
                continue

            # Calculate distance from waypoints (haversine sum)
            total_dist = 0
            for i in range(1, len(waypoints)):
                total_dist += haversine_m(
                    waypoints[i-1]["lat"], waypoints[i-1]["lon"],
                    waypoints[i]["lat"], waypoints[i]["lon"]
                )
            dist_km = round(total_dist / 1000, 2)

            # Track name
            name_el = _find(trk, "name")
            note = name_el.text.strip() if name_el is not None else None

            cur = db.execute(
                """INSERT INTO trips
                   (device, start_time, end_time, start_lat, start_lon,
                    end_lat, end_lon, distance_km, note, is_gpx)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (device, start_time, end_time,
                 first["lat"], first["lon"], last["lat"], last["lon"],
                 dist_km, note)
            )
            trip_id = cur.lastrowid

            # Insert waypoints
            for seq, wp in enumerate(waypoints):
                db.execute(
                    """INSERT INTO gpx_waypoints (trip_id, lat, lon, timestamp, elevation, speed, seq)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (trip_id, wp["lat"], wp["lon"], wp["ts"], wp["ele"], wp["speed"], seq)
                )
            imported += 1

    # Journeys covering the imported days take their trips in right away.
    # The import may have been the moment this vehicle got its device tag —
    # journeys created before that still carry the guessed one.
    if imported > 0:
        repair_orphan_journey_devices(db)
        sync_journey_trips(db, device=device)

    # Geocode start/end addresses
    if imported > 0:
        def _geocode():
            gdb = get_db()
            geo.geocode_trips(gdb)
            gdb.close()
        threading.Thread(target=_geocode, daemon=True).start()

    db.close()
    return jsonify({"ok": True, "imported": imported})


@trips_bp.route("/export/csv")
def export_csv():
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")

    db = get_db()
    query = "SELECT * FROM trips WHERE device = ?"
    params = [active_device()]

    if date_from and date_to:
        query += " AND start_time >= ? AND start_time <= ?"
        params += [date_from, date_to + "T23:59:59"]

    query += " ORDER BY start_time"
    trips = db.execute(query, params).fetchall()
    db.close()

    def _dec(v):
        """Number → German decimal comma for Excel."""
        if v is None or v == "":
            return ""
        return str(v).replace(".", ",")

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow([
        "Datum", "Startzeit", "Endzeit",
        "Start-Adresse", "Ziel-Adresse",
        "Strecke (km)", "SoC Start (%)", "SoC Ende (%)",
        "Verbrauch (kWh)", "kWh/100km",
        "Fahrtzweck", "Fahrziel", "Besuchsgrund", "Bemerkung",
    ])
    for t in trips:
        start = t["start_time"][:10] if t["start_time"] else ""
        stime = t["start_time"][11:16] if t["start_time"] else ""
        etime = t["end_time"][11:16] if t["end_time"] else ""
        writer.writerow([
            start, stime, etime,
            t["start_address"] or "", t["end_address"] or "",
            _dec(t["distance_km"]), _dec(t["soc_start"]), _dec(t["soc_end"]),
            _dec(t["energy_kwh"]), _dec(t["consumption"]),
            t["purpose"] or "Privatfahrt", t["destination"] or "",
            t["visit_reason"] or "", t["note"] or "",
        ])

    label = f"{date_from}_{date_to}" if date_from else "alle"
    filename = f"fahrtenlog_{label}.csv"
    # UTF-8 BOM so Excel correctly recognizes umlauts
    bom = b"\xef\xbb\xbf"
    return Response(
        bom + output.getvalue().encode("utf-8"),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@trips_bp.route("/export/gpx")
def export_gpx():
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")

    _t = _translations.get(get_language(), _translations["DE"])
    if not date_from or not date_to:
        return jsonify({"error": _t["trip_range_required"]}), 400

    from datetime import datetime, timedelta
    d_from = datetime.fromisoformat(date_from)
    d_to = datetime.fromisoformat(date_to)
    if (d_to - d_from).days > GPX_MAX_DAYS:
        return jsonify({"error": _t["trip_gpx_export_max_days"].format(days=GPX_MAX_DAYS)}), 400

    device = active_device()
    client = detector.get_influx()
    points = []
    if not client:
        return jsonify({"error": _t["trip_influx_not_configured"]}), 503
    try:
        query = f'''
        from(bucket: "{config.INFLUX_BUCKET}")
          |> range(start: {date_from}T00:00:00Z, stop: {date_to}T23:59:59Z)
          |> filter(fn: (r) => r._measurement == "v" and r.d == "{device}")
          |> filter(fn: (r) => r._field == "la" or r._field == "lo" or r._field == "v" or r._field == "s")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
          |> filter(fn: (r) => exists r.la and exists r.lo)
          |> sort(columns: ["_time"])
        '''
        tables = client.query_api().query(query, org=config.INFLUX_ORG)
        for table in tables:
            for record in table.records:
                points.append(record.values)
    except Exception as e:
        log.warning("InfluxDB query failed in GPX export: %s", e)
        return jsonify({"error": _t["trip_influx_query_failed"]}), 503
    finally:
        client.close()

    # Build GPX (shared writer: escaped names, speed in <extensions>)
    punkte = [(pt["la"], pt["lo"], pt["_time"].isoformat() if pt.get("_time") else None, pt.get("v"))
              for pt in points if pt.get("la") and pt.get("lo")]
    gpx_text = gpxexport.gpx_dokument(f"IDmate {date_from} - {date_to}",
                                      [(f"IDmate {date_from} - {date_to}", punkte)])

    filename = f"idmate_{date_from}_{date_to}.gpx"
    return Response(
        gpx_text,
        mimetype="application/gpx+xml",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@trips_bp.route("/api/trips/<int:trip_id>/route")
def trip_route(trip_id):
    """Load GPS points of a trip from InfluxDB + matched geofence locations."""
    db = get_db()
    trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if not trip:
        db.close()
        return jsonify({"points": [], "locations": []})

    # Geofence matches for start/end
    locs = []
    start_loc = match_location(db, trip["start_lat"], trip["start_lon"])
    end_loc = match_location(db, trip["end_lat"], trip["end_lon"])
    if start_loc:
        sk = start_loc.keys()
        locs.append({"lat": start_loc["lat"], "lon": start_loc["lon"],
                      "name": start_loc["name"], "icon": start_loc["icon"] or "pin",
                      "color": start_loc["color"] if "color" in sk else None,
                      "icon_color": start_loc["icon_color"] if "icon_color" in sk else None,
                      "pos": "start"})
    if end_loc:
        ek = end_loc.keys()
        locs.append({"lat": end_loc["lat"], "lon": end_loc["lon"],
                      "name": end_loc["name"], "icon": end_loc["icon"] or "pin",
                      "color": end_loc["color"] if "color" in ek else None,
                      "icon_color": end_loc["icon_color"] if "icon_color" in ek else None,
                      "pos": "end"})
    # GPX-imported trips: waypoints from SQLite instead of InfluxDB
    if trip["is_gpx"]:
        wps = db.execute(
            "SELECT lat, lon FROM gpx_waypoints WHERE trip_id = ? ORDER BY seq",
            (trip_id,)
        ).fetchall()
        db.close()
        points = [[round(w["lat"], 6), round(w["lon"], 6)] for w in wps]
        return jsonify({"points": points, "locations": locs})

    db.close()

    points = []
    client = detector.get_influx()
    if client:
        try:
            device = trip["device"] or active_device()

            query = f'''
            from(bucket: "{config.INFLUX_BUCKET}")
              |> range(start: {_to_rfc3339(trip["start_time"])}, stop: {_to_rfc3339_padded(trip["end_time"])})
              |> filter(fn: (r) => r._measurement == "v" and r.d == "{device}")
              |> filter(fn: (r) => r._field == "la" or r._field == "lo")
              |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
              |> sort(columns: ["_time"])
            '''
            tables = client.query_api().query(query, org=config.INFLUX_ORG)
            raw = []
            for table in tables:
                for rec in table.records:
                    raw.append(rec.values)
            raw.sort(key=lambda r: r.get("_time") or datetime.min.replace(tzinfo=timezone.utc))
            raw = detector._merge_rows(raw)
            # Filter GPS jumps
            raw = detector._filter_gps_jumps(raw)
            for rec in raw:
                lat = rec.get("la")
                lon = rec.get("lo")
                if lat and lon and lat != 0 and lon != 0:
                    points.append([round(lat, 6), round(lon, 6)])
        except Exception:
            log.exception("Error loading GPS route for trip %d", trip_id)
        finally:
            client.close()

    flux_start = _to_rfc3339(trip["start_time"])
    flux_stop = _to_rfc3339_padded(trip["end_time"])
    log.info("Route Trip %d: stored=%s→%s flux=%s→%s device=%s points=%d",
             trip_id, trip["start_time"], trip["end_time"],
             flux_start, flux_stop, trip["device"] or active_device(), len(points))

    return jsonify({"points": points, "locations": locs,
                    "_debug": {"stored_start": trip["start_time"], "stored_end": trip["end_time"],
                               "flux_start": flux_start, "flux_stop": flux_stop,
                               "device": trip["device"] or active_device(),
                               "points_count": len(points)}})


@trips_bp.route("/trips/<int:trip_id>")
def trip_detail(trip_id):
    db = get_db()
    trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if not trip:
        db.close()
        return redirect(url_for("trips.trips_list"))

    start_loc = match_location(db, trip["start_lat"], trip["start_lon"])
    end_loc = match_location(db, trip["end_lat"], trip["end_lon"])
    db.close()
    return render_template("trip_detail.html", trip=dict(trip),
                           start_loc=dict(start_loc) if start_loc else None,
                           end_loc=dict(end_loc) if end_loc else None)


@trips_bp.route("/api/trips/<int:trip_id>/chart-data")
def trip_chart_data(trip_id):
    """Telemetry data of a trip from InfluxDB for Chart.js."""
    db = get_db()
    trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if not trip:
        db.close()
        return jsonify({"labels": [], "datasets": {}})
    db.close()

    data = {"labels": [], "speed": [], "power": [], "soc": [],
            "range": [], "bat_temp": [], "ext_temp": [], "elevation": [],
            "odometer": [], "lat": [], "lon": [], "lte": [], "carrier": [],
            "heading": []}
    client = detector.get_influx()
    if client:
        try:
            query = f'''
            from(bucket: "{config.INFLUX_BUCKET}")
              |> range(start: {_to_rfc3339(trip["start_time"])}, stop: {_to_rfc3339_padded(trip["end_time"])})
              |> filter(fn: (r) => r._measurement == "v" and r.d == "{trip["device"] or active_device()}")
              |> filter(fn: (r) => r._field == "v" or r._field == "p" or r._field == "s"
                                or r._field == "r" or r._field == "bt" or r._field == "et"
                                or r._field == "al" or r._field == "od"
                                or r._field == "la" or r._field == "lo"
                                or r._field == "ls" or r._field == "lp"
                                or r._field == "hd")
              |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
              |> sort(columns: ["_time"])
            '''
            tables = client.query_api().query(query, org=config.INFLUX_ORG)
            for table in tables:
                for rec in table.records:
                    t = rec.get_time()
                    if t:
                        from zoneinfo import ZoneInfo
                        t = t.astimezone(config.LOCAL_TZ)
                    data["labels"].append(t.strftime("%H:%M:%S") if t else "")
                    data["speed"].append(rec.values.get("v"))
                    data["power"].append(rec.values.get("p"))
                    data["soc"].append(rec.values.get("s"))
                    data["range"].append(rec.values.get("r"))
                    data["bat_temp"].append(rec.values.get("bt"))
                    data["ext_temp"].append(rec.values.get("et"))
                    data["elevation"].append(rec.values.get("al"))
                    data["odometer"].append(rec.values.get("od"))
                    data["lat"].append(rec.values.get("la"))
                    data["lon"].append(rec.values.get("lo"))
                    data["lte"].append(rec.values.get("ls"))
                    data["carrier"].append(_plmn_name(rec.values.get("lp")))
                    data["heading"].append(rec.values.get("hd"))
            # Python-side forward-fill for null values at the start
            for key in ("speed", "power", "soc", "range", "bat_temp", "ext_temp",
                        "elevation", "odometer", "lat", "lon", "lte", "carrier", "heading"):
                last = None
                for i, v in enumerate(data[key]):
                    if v is not None:
                        last = v
                    elif last is not None:
                        data[key][i] = last
        except Exception:
            log.exception("Error loading chart data for trip %d", trip_id)
        finally:
            client.close()

    return jsonify(data)


@trips_bp.route("/api/trips/<int:trip_id>/split-points")
@login_required
def trip_split_points(trip_id):
    """Candidate split points (time + position + odo/soc) for the split UI."""
    db = get_db()
    row = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    db.close()
    if not row:
        return jsonify({"points": [], "error": "not_found"}), 404
    trip = dict(row)
    if trip.get("is_manual"):
        return jsonify({"points": [], "error": "manual"})
    return jsonify({"points": _trip_telemetry_points(trip)})


SPLIT_STOP_MIN = 3


def _stop_around(pts, i):
    """(first, last) telemetry point of the stop around index i: the run of
    points with the same odometer, at least SPLIT_STOP_MIN long; else None."""
    if i is None or not pts or pts[i].get("odo") is None:
        return None
    odo = pts[i]["odo"]
    same = lambda p: p.get("odo") is not None and abs(p["odo"] - odo) < 0.5
    i0 = i1 = i
    while i0 > 0 and same(pts[i0 - 1]):
        i0 -= 1
    while i1 < len(pts) - 1 and same(pts[i1 + 1]):
        i1 += 1
    try:
        dur = (_parse_local(pts[i1]["t"]) - _parse_local(pts[i0]["t"])).total_seconds() / 60
    except Exception:
        return None
    return (pts[i0], pts[i1]) if dur >= SPLIT_STOP_MIN else None


@trips_bp.route("/api/trips/<int:trip_id>/split", methods=["POST"])
@admin_required
def split_trip(trip_id):
    """Split a trip at the timestamp `at` into two trips. Inverse of merge:
    A = start→at, B = at→end. odo/SoC at the split point come from telemetry
    (nearest sample), with linear interpolation by time fraction as a fallback.
    For normal trips the track splits automatically (InfluxDB by time range);
    for GPX trips the waypoints are distributed across A/B by timestamp."""
    data = request.get_json(silent=True) or {}
    at = (data.get("at") or "").strip()
    _t = _translations.get(get_language(), _translations["DE"])
    if not at:
        return jsonify({"error": _t["trip_split_point_missing"]}), 400

    db = get_db()
    row = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({"error": _t["err_trip_not_found"]}), 404
    t = dict(row)
    if t.get("is_manual"):
        db.close()
        return jsonify({"error": _t["trip_split_manual_no_track"]}), 400

    try:
        at_dt = _parse_local(at)
        st_dt = _parse_local(t["start_time"])
        en_dt = _parse_local(t["end_time"])
    except Exception:
        db.close()
        return jsonify({"error": _t["trip_split_point_invalid"]}), 400
    if not (st_dt < at_dt < en_dt):
        db.close()
        return jsonify({"error": _t["trip_split_point_range"]}), 400

    # Telemetry at the split point: nearest sample
    pts = _trip_telemetry_points(t)
    sp_lat = sp_lon = sp_odo = sp_soc = None
    best, best_d, best_i = None, None, None
    for i, p in enumerate(pts):
        try:
            pd = _parse_local(p["t"])
        except Exception:
            log.debug("split_trip: unparseable telemetry timestamp %r — skipped", p.get("t"), exc_info=True)
            continue
        d = abs((pd - at_dt).total_seconds())
        if best_d is None or d < best_d:
            best_d, best, best_i = d, p, i
    if best:
        sp_lat, sp_lon, sp_odo, sp_soc = best["lat"], best["lon"], best["odo"], best["soc"]

    # Split point inside a stop (odometer unchanged for ≥ SPLIT_STOP_MIN):
    # A ends where the stop began, B starts where the car drove on — so a
    # charge during the stop shows as the SoC rise between A and B and the
    # external-charge detection finds it. A point while driving splits there.
    end_a = start_b = (at, sp_lat, sp_lon, sp_odo, sp_soc)
    stop = _stop_around(pts, best_i)
    if stop:
        p0, p1 = stop
        end_a = (p0["t"], p0["lat"], p0["lon"], p0["odo"], p0["soc"])
        start_b = (p1["t"], p1["lat"], p1["lon"], p1["odo"], p1["soc"])

    total_sec = (en_dt - st_dt).total_seconds() or 1
    frac = (at_dt - st_dt).total_seconds() / total_sec

    odo_s, odo_e = t.get("odo_start"), t.get("odo_end")
    odo_split = sp_odo
    if odo_split is None and odo_s is not None and odo_e is not None:
        odo_split = round(odo_s + (odo_e - odo_s) * frac, 1)
    soc_s, soc_e = t.get("soc_start"), t.get("soc_end")
    soc_split = sp_soc
    if soc_split is None and soc_s is not None and soc_e is not None:
        soc_split = round(soc_s + (soc_e - soc_s) * frac, 1)
    soc_a = end_a[4] if stop and end_a[4] is not None else soc_split
    soc_b = start_b[4] if stop and start_b[4] is not None else soc_split

    bat_kwh = get_bat_kwh(db, t["device"], t["start_time"])

    total_dist = t.get("distance_km")

    def _dist(o1, o2, f):
        if o1 is not None and o2 is not None:
            return round(o2 - o1, 1)
        if total_dist is not None:
            return round(total_dist * f, 1)
        return None
    distA = _dist(odo_s, odo_split, frac)
    distB = _dist(odo_split, odo_e, 1 - frac)

    def _energy_soc(s1, s2):
        if s1 is not None and s2 is not None and s1 > s2:
            return round((s1 - s2) / 100.0 * bat_kwh, 2)
        return None
    enA = _energy_soc(soc_s, soc_a)
    enB = _energy_soc(soc_b, soc_e)
    total_energy = t.get("energy_kwh")
    # Fallback: split total energy by distance share
    if total_energy is not None and distA is not None and distB is not None and (distA + distB) > 0:
        if enA is None:
            enA = round(total_energy * distA / (distA + distB), 2)
        if enB is None:
            enB = round(total_energy * distB / (distA + distB), 2)

    def _cons(en, dist):
        return fm.trip_consumption(en, dist)

    common = {
        "device": t["device"], "purpose": t.get("purpose") or "",
        "destination": t.get("destination") or "", "visit_reason": t.get("visit_reason") or "",
        "note": t.get("note"), "is_manual": t.get("is_manual") or 0, "is_gpx": t.get("is_gpx") or 0,
    }
    tripA = {**common, "start_time": t["start_time"], "end_time": end_a[0],
             "start_lat": t.get("start_lat"), "start_lon": t.get("start_lon"),
             "end_lat": end_a[1], "end_lon": end_a[2],
             "odo_start": odo_s, "odo_end": odo_split, "soc_start": soc_s, "soc_end": soc_a,
             "distance_km": distA, "energy_kwh": enA, "consumption": _cons(enA, distA)}
    tripB = {**common, "start_time": start_b[0], "end_time": t["end_time"],
             "start_lat": start_b[1], "start_lon": start_b[2],
             "end_lat": t.get("end_lat"), "end_lon": t.get("end_lon"),
             "odo_start": odo_split, "odo_end": odo_e, "soc_start": soc_b, "soc_end": soc_e,
             "distance_km": distB, "energy_kwh": enB, "consumption": _cons(enB, distB)}

    jids = [r["journey_id"] for r in
            db.execute("SELECT journey_id FROM journey_trips WHERE trip_id = ?", (trip_id,)).fetchall()]
    excluded_jids = [r["journey_id"] for r in db.execute(
        "SELECT journey_id FROM journey_trip_excluded WHERE trip_id = ?", (trip_id,)).fetchall()]

    insert_sql = """INSERT INTO trips
        (device, start_time, end_time, start_lat, start_lon, end_lat, end_lon,
         odo_start, odo_end, distance_km, soc_start, soc_end, energy_kwh, consumption,
         purpose, destination, visit_reason, note, is_manual, is_gpx)
        VALUES (:device, :start_time, :end_time, :start_lat, :start_lon, :end_lat, :end_lon,
                :odo_start, :odo_end, :distance_km, :soc_start, :soc_end, :energy_kwh, :consumption,
                :purpose, :destination, :visit_reason, :note, :is_manual, :is_gpx)"""
    try:
        with db:
            id_a = db.execute(insert_sql, tripA).lastrowid
            id_b = db.execute(insert_sql, tripB).lastrowid
            # Distribute GPX waypoints across A/B by timestamp (normal trips have
            # none — their track comes live from InfluxDB by time range).
            if t.get("is_gpx"):
                wps = db.execute(
                    "SELECT id, timestamp FROM gpx_waypoints WHERE trip_id = ?", (trip_id,)).fetchall()
                for w in wps:
                    try:
                        in_a = _parse_local(w["timestamp"]) <= at_dt
                    except Exception:
                        in_a = True
                    db.execute("UPDATE gpx_waypoints SET trip_id = ? WHERE id = ?",
                               (id_a if in_a else id_b, w["id"]))
            # Carry journey membership (and a by-hand removal) over to both halves
            for jid in jids:
                db.execute("INSERT OR IGNORE INTO journey_trips (journey_id, trip_id) VALUES (?, ?)", (jid, id_a))
                db.execute("INSERT OR IGNORE INTO journey_trips (journey_id, trip_id) VALUES (?, ?)", (jid, id_b))
            for jid in excluded_jids:
                db.execute("INSERT OR IGNORE INTO journey_trip_excluded (journey_id, trip_id) VALUES (?, ?)", (jid, id_a))
                db.execute("INSERT OR IGNORE INTO journey_trip_excluded (journey_id, trip_id) VALUES (?, ?)", (jid, id_b))
            # Remove the original (cascade clears the original's remaining
            # journey_trips; GPX waypoints were re-pointed above, not deleted).
            db.execute("DELETE FROM trips WHERE id = ?", (trip_id,))
    except Exception:
        log.exception("split_trip failed for %s", trip_id)
        db.close()
        return jsonify({"error": _t["trip_split_failed"]}), 500
    db.close()
    return jsonify({"ok": True, "ids": [id_a, id_b]})
