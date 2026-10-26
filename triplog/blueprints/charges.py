"""Charging routes: webhook, sessions, readings, import/rebuild (FIXES 6.1).

Moved verbatim out of app.py (``@app.route`` → ``@charges_bp.route``; internal
``url_for("charges_list")`` → ``url_for("charges.charges_list")``). The
``detect_external_from_trips`` helper moved along (used only by
``api_detect_external``). ``@csrf.exempt`` on the webhook keeps the imported
``csrf`` reference. Endpoint names gain the ``charges.`` prefix; templates use
hardcoded paths so they are unaffected.
"""

import calendar
import csv
import hmac
import io

import beleg
import fahrzeugmodell as fm
from datetime import datetime, timedelta, timezone

import config
from detector import sanitize_soc as _sanitize_soc
from flask import (Response, Blueprint, jsonify, redirect, render_template, request,
                   session, url_for)
from flask_login import current_user, login_required

from app import (ENCRYPTED_SETTINGS, _billed_floor, _decrypt_setting,
                 _operator_icon_map, _parse_german_num, _real_consumption,
                 _settings_fernet, _translations, active_device, active_vehicle,
                 admin_required, csrf, effective_date_range, get_bat_kwh, get_db,
                 get_language, haversine_m, log, match_charge_location,
                 match_location, rebuild_charge_sessions)

charges_bp = Blueprint("charges", __name__)


def _vehicle_pos_at_charge(db, sess):
    """Where the vehicle was when this charge happened: the end position of the
    last trip before the charge midpoint (same anchor rebuild_charge_sessions
    uses). Independent of session.lat/lon, which gets overwritten with the
    assigned location's coords once a station is picked. Falls back to the
    session's own coords. Returns (lat, lon) or (None, None)."""
    mid = sess["start_time"]
    if sess["start_time"] and sess["end_time"]:
        try:
            t0 = datetime.fromisoformat(sess["start_time"])
            t1 = datetime.fromisoformat(sess["end_time"])
            mid = (t0 + (t1 - t0) / 2).isoformat()
        except (ValueError, TypeError):
            pass
    dev = db.execute(
        "SELECT device FROM vehicles WHERE plate = ? AND device IS NOT NULL",
        (sess["vehicle_plate"],)
    ).fetchone()
    if dev and mid:
        trip = db.execute(
            "SELECT end_lat, end_lon FROM trips WHERE device = ? AND end_time <= ? "
            "AND end_lat IS NOT NULL AND end_lon IS NOT NULL "
            "ORDER BY end_time DESC LIMIT 1",
            (dev["device"], mid)
        ).fetchone()
        if trip:
            return trip["end_lat"], trip["end_lon"]
    if sess["lat"] and sess["lon"]:
        return sess["lat"], sess["lon"]
    return None, None


@charges_bp.route("/api/charge/reading", methods=["POST"])
@csrf.exempt  # external Home Assistant caller, auth via Bearer token
def charge_webhook():
    """Receive 15-min readings from Home Assistant."""
    log.info("charge_webhook: incoming from %s, auth-header-present=%s, content-type=%s",
             request.remote_addr,
             bool(request.headers.get("Authorization")),
             request.headers.get("Content-Type", ""))
    # Token is mandatory. Empty CHARGE_WEBHOOK_TOKEN means the operator forgot
    # to configure it — fail closed instead of accepting anonymous writes that
    # would let anyone inject charging records.
    token = config.CHARGE_WEBHOOK_TOKEN
    if not token:
        log.warning("charge_webhook: 503 — CHARGE_WEBHOOK_TOKEN is empty (env not set?)")
        return jsonify({"error": "Webhook not configured"}), 503
    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {token}"
    # compare_digest avoids timing leaks; the ?token= query variant is gone
    # because proxies/access-logs love to capture URLs verbatim.
    if not hmac.compare_digest(auth, expected):
        log.warning("charge_webhook: 401 — Authorization header does not match (len got=%d, expected=%d, starts_with_Bearer=%s)",
                    len(auth), len(expected), auth.startswith("Bearer "))
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True, silent=True)
    if not data:
        log.warning("charge_webhook: 400 — body is not JSON or empty")
        return jsonify({"error": "JSON expected"}), 400

    vehicle = str(data.get("vehicle", "")).strip()
    if not vehicle or vehicle == "free":
        log.info("charge_webhook: skipped — vehicle=%r (HA input_select appears to be 'free' or empty)",
                 vehicle)
        return jsonify({"ok": True, "skipped": True, "reason": "vehicle_free_or_empty"})
    if vehicle in ("error", "unknown"):
        vehicle = "unknown"

    # Energy per interval. Two equivalent contracts:
    #   (a) HA sends "kwh" explicitly (legacy + idmate_charge_tracker template).
    #   (b) HA sends only the absolute meter span (meter_start/meter_end) and we
    #       compute the delta here. This lets HA fire event-driven, contiguous
    #       intervals (e.g. close the old car at 10:07, open the next at 10:08)
    #       without HA having to track its own last_meter — the meter readings
    #       themselves guarantee no gap or double-count at the boundary.
    meter_start = float(data.get("meter_start", 0) or 0)
    meter_end = float(data.get("meter_end", 0) or 0)
    kwh_raw = data.get("kwh", None)
    _kwh_span_derived = False
    if kwh_raw not in ("", None):
        kwh = float(kwh_raw)
    elif meter_start > 0 and meter_end >= meter_start:
        kwh = round(meter_end - meter_start, 3)
        # Span-derived energy gets a plausibility check (meter reset / stale
        # anchor phantom) — but only AFTER the overlap clip below: a resend
        # span keeps growing while responses get lost, and rejecting it here
        # would deadlock the anchor forever although the clipped tail is fine.
        _kwh_span_derived = True
    else:
        kwh = 0.0
    if kwh <= 0:
        log.info("charge_webhook: skipped — kwh=%s (no consumption / meter 0/negative; vehicle=%s)",
                 kwh, vehicle)
        return jsonify({"ok": True, "skipped": True, "reason": "kwh_zero_or_negative"})

    timestamp = data.get("timestamp", "")
    # HA sends UTC — convert to local time (trips are Europe/Berlin)
    if timestamp:
        try:
            from zoneinfo import ZoneInfo
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            timestamp = dt.astimezone(config.LOCAL_TZ).strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            log.warning("charge reading: timestamp normalisation failed for %r — kept as-is",
                        timestamp, exc_info=True)
    odometer_raw = data.get("odometer", "")
    odometer = float(odometer_raw) if odometer_raw not in ("", None) else None

    tibber_price_raw = data.get("tibber_price", 0)
    tibber_grund_raw = data.get("tibber_grundgebuehr", 0)
    soc_raw = data.get("soc", None)
    soc = _sanitize_soc(soc_raw) if soc_raw not in ("", None) else None

    db = get_db()

    # Diagnostic logging (no behavior change): the HA anchor is global (one
    # input_number across all vehicles), so meter_start should equal the last
    # reading's meter_end. If it differs, HA re-anchored or readings are
    # missing — exactly where a charge gap would form. Only warn so it shows up
    # in the log; /api/charge/gaps surfaces it in the UI anyway.
    if meter_start and meter_start > 0:
        _last = db.execute(
            "SELECT meter_end FROM charge_readings "
            "WHERE meter_end IS NOT NULL ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if _last and _last["meter_end"] is not None:
            _disc = meter_start - float(_last["meter_end"])
            if abs(_disc) > 0.05:
                log.warning(
                    "charge_webhook: meter discontinuity %.3f kWh "
                    "(meter_start=%.3f, previous meter_end=%.3f, vehicle=%s) — "
                    "possible gap / HA re-anchoring",
                    _disc, meter_start, float(_last["meter_end"]), vehicle)

    # Idempotency guard against HA at-least-once resends: when the RESPONSE to
    # a stored reading is lost (timeout/proxy/HA restart), HA keeps its meter
    # anchor and re-sends the same span later — with a NEW timestamp and a
    # possibly grown meter_end, so the exact-duplicate dedup can never match
    # it. The wallbox meter is monotonic: any overlap with an already-stored
    # span IS already-billed energy. Clip instead of double-count:
    #   - span fully covered  → confirm without inserting (HA advances its
    #     anchor and the resend loop ends; nothing new to store),
    #   - partial overlap     → store only the uncovered tail [last_end, me).
    # Only recent rows count as overlap candidates, so a meter swap/reset
    # (span numbers starting over) can't false-positive against old rows.
    if meter_end > meter_start > 0:
        _window = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S")
        _ov = db.execute(
            "SELECT MAX(meter_end) AS last_end FROM charge_readings "
            "WHERE timestamp >= ? AND meter_start IS NOT NULL AND meter_end IS NOT NULL "
            "AND meter_end > ? AND meter_start < ?",
            (_window, meter_start, meter_end),
        ).fetchone()
        _last_end = float(_ov["last_end"]) if _ov and _ov["last_end"] is not None else None
        if _last_end is not None:
            _tail = round(meter_end - _last_end, 3)
            if _tail <= 0:
                db.close()
                log.warning("charge_webhook: span [%.3f, %.3f] already stored — "
                            "resend after lost response? Confirmed without insert (vehicle=%s)",
                            meter_start, meter_end, vehicle)
                return jsonify({"ok": True, "deduped": True, "reason": "span_already_stored"})
            log.warning("charge_webhook: overlapping span [%.3f, %.3f] clipped to "
                        "[%.3f, %.3f] — resend after lost response? Storing only the "
                        "new energy (vehicle=%s, kwh %.3f → %.3f)",
                        meter_start, meter_end, _last_end, meter_end, vehicle, kwh, _tail)
            meter_start = _last_end
            kwh = _tail
            _kwh_span_derived = True

    # Plausibility of span-derived energy — checked on the FINAL (possibly
    # clipped) value: a meter reset / stale anchor phantom is still dropped,
    # but a legitimately grown resend span is not (its clipped tail is real).
    if _kwh_span_derived and kwh > 150:
        db.close()
        log.warning("charge_webhook: skipped — computed kwh=%.3f implausible "
                    "(meter_start=%.3f, meter_end=%.3f, vehicle=%s) — meter reset?",
                    kwh, meter_start, meter_end, vehicle)
        return jsonify({"ok": True, "skipped": True, "reason": "kwh_implausible"})

    db.execute("""
        INSERT INTO charge_readings
        (timestamp, vehicle_plate, meter_start, meter_end, kwh,
         tibber_price, tibber_grundgebuehr, odometer, soc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        timestamp,
        vehicle,
        meter_start,
        meter_end,
        kwh,
        float(tibber_price_raw) if tibber_price_raw not in ("", None) else None,
        float(tibber_grund_raw) if tibber_grund_raw not in ("", None) else None,
        odometer,
        soc,
    ))
    db.commit()

    # Scoped rebuild: only the period of this new reading — never touch older
    # (possibly billed) sessions.
    rebuild_charge_sessions(db, since=timestamp)
    db.close()
    log.info("charge_webhook: written — vehicle=%s, kwh=%.3f, ts=%s", vehicle, kwh, timestamp)
    return jsonify({"ok": True})


# ── Charge sessions page ────────────────────────────────────

def _overview_filters():
    """Vehicle and date range of the charge overview — shared by the list and
    its CSV export so both see the same rows."""
    filter_vehicle = request.args.get("vehicle", "")
    # Unified global range (shared with Trips/Analysis), stored via /api/daterange.
    # Explicit URL params override and re-stamp the session; otherwise the range
    # defaults to the last 30 days and auto-reverts after 60 min (perf: never
    # query 5y of full history as the steady state).
    if "from" in request.args or "to" in request.args:
        date_from = request.args.get("from", "")
        date_to = request.args.get("to", "")
        session["date_from"] = date_from
        session["date_to"] = date_to
        session["date_set_at"] = datetime.now().isoformat()
    else:
        date_from, date_to = effective_date_range()

    # Default: active vehicle from global filter
    if not filter_vehicle:
        v = active_vehicle()
        if v:
            filter_vehicle = v["plate"]
    return filter_vehicle, date_from, date_to


def _overview_sessions(db, filter_vehicle, date_from, date_to):
    """The rows of the charge overview, newest first."""
    query = "SELECT * FROM charge_sessions WHERE 1=1"
    params = []

    if filter_vehicle:
        # Always surface unidentified charges (OFF/unknown) alongside the
        # selected vehicle, so none are lost for billing no matter which
        # vehicle is active. They are shown red and excluded from the totals.
        query += " AND (vehicle_plate = ? OR UPPER(COALESCE(vehicle_plate,'')) IN ('OFF','UNKNOWN'))"
        params.append(filter_vehicle)
    if date_from:
        query += " AND start_time >= ?"
        params.append(date_from)
    if date_to:
        query += " AND start_time <= ?"
        params.append(date_to + "T23:59:59")

    query += " ORDER BY datetime(start_time) DESC"
    return db.execute(query, params).fetchall()


def _home_flat(db, sessions, tariffs):
    """HOME = wallbox OR charge location flagged is_home (same rule as the print
    and the analysis charts). Charges reconstructed from trips/SoC before the
    wallbox existed carry no stored flat-rate cost → value them at the tariff
    of their date, so the 'Pauschale' sum is the same figure on every page.
    Returns (home session ids, {session id: flat-rate cost where none is stored})."""
    home_names = {r["name"] for r in db.execute(
        "SELECT name FROM charge_locations WHERE is_home = 1").fetchall()}
    _rate = fm.flat_rates(tariffs)

    home_ids = set()
    home_flat = {}
    for s in sessions:
        if (not s["is_external"]) or s["location_name"] in home_names:
            home_ids.add(s["id"])
            if s["cost_pauschale"] is None and s["total_kwh"]:
                home_flat[s["id"]] = round(s["total_kwh"] * _rate(s["start_time"]), 2)
    return home_ids, home_flat


@charges_bp.route("/charges")
def charges_list():
    filter_vehicle, date_from, date_to = _overview_filters()

    db = get_db()
    sessions = _overview_sessions(db, filter_vehicle, date_from, date_to)

    vehicles = db.execute("SELECT * FROM vehicles ORDER BY plate").fetchall()
    tariffs = db.execute("SELECT * FROM charge_tariffs ORDER BY valid_from DESC").fetchall()

    vehicle_names = {}
    for v in vehicles:
        vehicle_names[v['plate']] = v['name'] or v['plate']

    # Charge location operators (name → operator key + color + icon)
    loc_operators = {}
    for cl in db.execute("""SELECT cl.name, cl.operator, cl.type, cl.color, cl.icon_filename,
                                   op.icon_filename AS op_icon_filename, op.color AS op_color
                            FROM charge_locations cl
                            LEFT JOIN operators op ON cl.operator_id = op.id""").fetchall():
        if cl['icon_filename']:
            icon_url = f"/media/charge-icons/{cl['icon_filename']}"
        elif cl['op_icon_filename']:
            icon_url = f"/media/operator-icons/{cl['op_icon_filename']}"
        else:
            icon_url = None
        loc_operators[cl['name']] = {
            'operator': cl['operator'] or '',
            'type': cl['type'] or 'ac',
            'color': cl['op_color'] or cl['color'] or '#8b949e',
            'icon_url': icon_url,
        }

    # All charge locations with operator names for dropdown
    all_locations_list = db.execute(
        """SELECT cl.name, op.name AS op_name FROM charge_locations cl
           LEFT JOIN operators op ON cl.operator_id = op.id
           ORDER BY cl.name COLLATE NOCASE"""
    ).fetchall()

    # Webhook URL hint
    webhook_url = f"http://<HOST>:{request.host.split(':')[-1] if ':' in request.host else '3004'}"

    # Session start number
    start_row = db.execute("SELECT value FROM settings WHERE key = 'charge_session_start'").fetchone()
    session_start = int(start_row['value']) if start_row else 1

    # Wh/km per row + "Ø real": charge-anchored consumption (metered energy ÷
    # odometer-km between charges, external charges included). One shared helper
    # (_real_consumption) → identical numbers on every page. Battery-side Wh/km
    # for the table; the helper also returns the grid-side figure for the header.
    real = _real_consumption(db, filter_vehicle, date_from or None, date_to or None) if filter_vehicle else None
    wpk_by_id = real["per_row"] if real else {}
    avg_wh_per_km = (real["cons_batt"] * 10) if (real and real["cons_batt"] is not None) else None  # kWh/100km → Wh/km
    total_wpk = avg_wh_per_km

    home_ids, home_flat = _home_flat(db, sessions, tariffs)

    db.close()
    return render_template("charges.html",
                           beleg_schemata=[s.name for s in beleg.SCHEMATA],
                           sessions=sessions,
                           home_flat=home_flat,
                           home_ids=home_ids,
                           vehicles=vehicles,
                           tariffs=tariffs,
                           vehicle_names=vehicle_names,
                           loc_operators=loc_operators,
                           all_locations=all_locations_list,
                           filter_vehicle=filter_vehicle,
                           date_from=date_from,
                           date_to=date_to,
                           webhook_url=webhook_url,
                           session_start=session_start,
                           avg_wh_per_km=avg_wh_per_km,
                           wpk_by_id=wpk_by_id,
                           total_wpk=total_wpk,
                           real=real)


@charges_bp.route("/charges/export/csv")
def charges_export_csv():
    """The charge overview as CSV with the list's filters: vehicle, date range
    (explicit or the shared one) and external charges on/off (ext=0 mirrors
    the list's toggle). Same rows and numbers as the table — same query, same
    home/flat-rate valuation, same Wh/km. German Excel conventions like the
    trip export: semicolon, decimal comma, UTF-8 with BOM."""
    filter_vehicle, date_from, date_to = _overview_filters()
    with_ext = request.args.get("ext", "1") != "0"
    db = get_db()
    sessions = _overview_sessions(db, filter_vehicle, date_from, date_to)
    tariffs = db.execute("SELECT * FROM charge_tariffs ORDER BY valid_from DESC").fetchall()
    home_ids, home_flat = _home_flat(db, sessions, tariffs)
    real = _real_consumption(db, filter_vehicle, date_from or None, date_to or None) if filter_vehicle else None
    wpk_by_id = real["per_row"] if real else {}
    operators = {r["name"]: (r["op_name"] or r["operator"] or "") for r in db.execute(
        """SELECT cl.name, cl.operator, op.name AS op_name FROM charge_locations cl
           LEFT JOIN operators op ON cl.operator_id = op.id""").fetchall()}
    db.close()

    def _dec(v, digits=None):
        """Number → German decimal comma for Excel; None → empty cell."""
        if v is None or v == "":
            return ""
        if digits is not None:
            v = f"{v:.{digits}f}"
        return str(v).replace(".", ",")

    def _col(row, key):
        return row[key] if key in row.keys() else None

    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["Nr", "Datum", "Start", "Ende", "Dauer (min)", "Fahrzeug", "Ort", "Anbieter",
                "km-Stand", "Strecke (km)", "kWh", "Wh/km", "SoC Start (%)", "SoC Ende (%)",
                "Ø kW", "Dynamisch (EUR)", "Ø ct/kWh", "Pauschale (EUR)", "Differenz (EUR)",
                "Extern gesamt (EUR)", "Art", "Notiz"])
    for s in sessions:
        if s["is_external"] and not with_ext:
            continue
        home = s["id"] in home_ids
        ext = bool(s["is_external"])
        art = "Extern" if ext and not home else ("Zuhause" if ext else "Wallbox")
        flat = s["cost_pauschale"] if s["cost_pauschale"] is not None else home_flat.get(s["id"])
        dyn = _col(s, "cost_tibber") if not ext else None
        avg_ct = (dyn / s["total_kwh"] * 100) if (dyn and s["total_kwh"]) else None
        w.writerow([
            s["session_number"] or "",
            (s["start_time"] or "")[:10], (s["start_time"] or "")[11:16], (s["end_time"] or "")[11:16],
            _col(s, "duration_minutes") or "",
            s["vehicle_plate"] or "", s["location_name"] or "", operators.get(s["location_name"], ""),
            _dec(s["odometer"], 0), _dec(_col(s, "distance"), 0), _dec(s["total_kwh"], 2),
            _dec(wpk_by_id.get(s["id"]), 0), _dec(s["soc_start"], 0), _dec(s["soc_end"], 0),
            _dec(_col(s, "avg_kw"), 1),
            _dec(dyn, 2), _dec(avg_ct, 1),
            _dec(flat, 2) if home else "",
            _dec(_col(s, "cost_diff"), 2) if not ext else "",
            _dec(s["cost_total"], 2) if ext and not home else "",
            art, _col(s, "note") or "",
        ])
    label = "_".join(x for x in (filter_vehicle, date_from, date_to) if x) or "alle"
    bom = b"\xef\xbb\xbf"
    return Response(bom + out.getvalue().encode("utf-8"), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=ladelog_{label}.csv"})


# ── Charge print view ────────────────────────────────────

# A charge is a HOME charge when the wallbox metered it (is_external = 0) OR it
# happened at a charge location flagged is_home. Charges reconstructed from
# trips/SoC before the wallbox existed are stored with is_external = 1 but sit at
# the home location — they are home charges on own electricity, not public ones.
# `is_external` keeps its original meaning ("not metered by the wallbox") and is
# still the right filter for meter reconciliation, rebuild and gap detection.
_IS_HOME_SQL = (
    "(is_external = 0 OR COALESCE("
    "(SELECT cl.is_home FROM charge_locations cl WHERE cl.name = location_name), 0) = 1)"
)

# The odometer stamped on a charge is the car's dashboard reading; the charge
# card provider's system (UTA) records the charge about 5 km further on. So the
# external-charges table of the print — the one that gets reconciled against
# the provider's statement — shows the stamp plus this offset. Nothing else
# uses it: the stored value and every km calculation stay untouched.
EXT_PRINT_ODO_OFFSET_KM = 5


def _interval_walk(sessions_ordered):
    """The reference interval walk behind the print's monthly charts.

    Walk all sessions in odometer order; a segment runs from one charge to the
    next. HOME charts count only home→home segments: the current charge's
    metered energy/cost replenishes what was consumed driving the km since the
    previous charge. Segments below 8 kWh/100km are skipped — that signature
    means UNTRACKED external charging in between.

    SoC correction — the home numbers must be REAL consumption, defensible to
    an external reader: a fill-up before/after a road trip lands in a short
    home→home segment while the trip km sit in ext segments, structurally
    inflating the month (seen 08/2026: 28.1 shown vs ~19 real). The battery
    knows the truth: of the charged SoC lift, only the share that was DRIVEN
    down since the previous charge belongs to this segment, so the metered
    energy/cost is scaled by (soc_end(prev) − soc_start(cur)) /
    (soc_end(cur) − soc_start(cur)). No battery capacity involved; charging
    losses stay in proportionally. Guards: all three SoC present, a charge
    lift ≥ 5 points (division noise), ratio within [0.2, 3.0] — otherwise the
    segment books its raw metered value like before.

    The "incl. external" walk counts the SAME (corrected) home segments plus
    every ext-touching segment (odometer stamps bring the trip km into the
    denominator, receipts into the numerator) — and it must CONSERVE MONEY:
    what the correction leaves unbooked in a home segment (the fill-up's
    surplus, paid at the home tariff) is battery inventory that powers the
    trip km right after, so it is carried along and released into the next
    ext-touching segment. Without that carry the incl. value dropped ~36 EUR
    of real spend in 08/2026 and ended BELOW the home value despite 70 ct/kWh
    external energy. A month without external charges never releases the
    carry and therefore yields EXACTLY the home numbers; a trip month lands on
    "all money / all km" and sits ABOVE the home value. Ext-touching segments
    book into the month of the EXT side, never into a home month, and skip
    the 8-kWh floor (a partial DC charge legitimately covers more km than its
    kWh suggest).

    Energy follows the same rules as money: the incl. pot also carries the
    home kWh that the correction left unbooked into the next ext segment, so
    "all kWh / all km" holds for a trip month and a month without external
    charges yields exactly the home kWh/100km.

    Returns (cons_by_month, incl_by_month):
      cons_by_month: ym -> {'kwh':, 'dist':, 'cost':}   home→home only
      incl_by_month: ym -> {'kwh':, 'dist':, 'cost':}   + ext segments
    """
    cons_by_month = {}
    incl_by_month = {}
    carry = 0.0  # EUR paid at home but not yet booked (battery inventory)
    carry_kwh = 0.0  # same for the metered energy
    prev_session = None
    for s in sessions_ordered:
        if prev_session is not None:
            d = s['odometer'] - prev_session['odometer']
            kwh = s['total_kwh'] or 0
            cost = s['session_cost'] or 0
            raw_cost = cost
            raw_kwh = kwh
            home_pair = s['is_home_charge'] and prev_session['is_home_charge']
            if not s['is_home_charge']:
                ym_incl = s['ym']
            elif not prev_session['is_home_charge']:
                ym_incl = prev_session['ym']
            else:
                ym_incl = s['ym']
            if home_pair:
                pe, cs, ce = (prev_session['soc_end'], s['soc_start'],
                              s['soc_end'])
                if (pe is not None and cs is not None and ce is not None
                        and ce - cs >= 5 and pe - cs > 0):
                    ratio = (pe - cs) / (ce - cs)
                    if 0.2 <= ratio <= 3.0:
                        kwh *= ratio
                        cost *= ratio
            if d > 0:
                ym = s['ym']
                plausible_home = home_pair and kwh > 0 and kwh / d * 100 >= 8
                if plausible_home:
                    if ym not in cons_by_month:
                        cons_by_month[ym] = {'kwh': 0, 'dist': 0, 'cost': 0}
                    cons_by_month[ym]['kwh'] += kwh
                    cons_by_month[ym]['dist'] += d
                    cons_by_month[ym]['cost'] += cost
                    # Paid but unbooked (or booked beyond paid when a partial
                    # charge ran on earlier surplus): signed inventory.
                    carry += raw_cost - cost
                    carry_kwh += raw_kwh - kwh
                if plausible_home or not home_pair:
                    if ym_incl not in incl_by_month:
                        incl_by_month[ym_incl] = {'kwh': 0, 'dist': 0, 'cost': 0}
                    incl_by_month[ym_incl]['dist'] += d
                    incl_by_month[ym_incl]['cost'] += cost
                    incl_by_month[ym_incl]['kwh'] += kwh
                    if not home_pair:
                        # The trip km ran on the home inventory: release it
                        # here so the incl. pot holds every EUR really spent.
                        incl_by_month[ym_incl]['cost'] += carry
                        incl_by_month[ym_incl]['kwh'] += carry_kwh
                        carry = 0.0
                        carry_kwh = 0.0
            elif d == 0 and not s['is_home_charge']:
                # Restarted external charge at the same stop: no new km,
                # but the receipt still belongs into the €/100km pot.
                if ym_incl not in incl_by_month:
                    incl_by_month[ym_incl] = {'kwh': 0, 'dist': 0, 'cost': 0}
                incl_by_month[ym_incl]['cost'] += cost + carry
                incl_by_month[ym_incl]['kwh'] += kwh + carry_kwh
                carry = 0.0
                carry_kwh = 0.0
        prev_session = s
    return cons_by_month, incl_by_month


@charges_bp.route("/charges/print")
@login_required
def charges_print():
    MONTH_NAMES_DE = ['', 'Januar', 'Februar', 'März', 'April', 'Mai', 'Juni',
                      'Juli', 'August', 'September', 'Oktober', 'November', 'Dezember']

    month = request.args.get("month", "")
    year = request.args.get("year", "")

    # Vehicle always from global filter
    filter_vehicle = ""
    v = active_vehicle()
    if v:
        filter_vehicle = v["plate"]

    db = get_db()

    # Vehicle data
    vehicle = db.execute("SELECT * FROM vehicles WHERE plate = ?",
                         (filter_vehicle,)).fetchone() if filter_vehicle else None

    # ALL sessions in the month (all vehicles) for excluded hint
    all_query = f"SELECT *, {_IS_HOME_SQL} AS is_home_charge FROM charge_sessions WHERE 1=1"
    all_params = []
    if year:
        all_query += " AND strftime('%Y', start_time) = ?"
        all_params.append(year)
    if month:
        all_query += " AND strftime('%m', start_time) = ?"
        all_params.append(month.zfill(2))
    all_query += " ORDER BY datetime(start_time)"
    all_sessions = db.execute(all_query, all_params).fetchall()

    # Billing reconciliation for the whole month across ALL vehicles (shared
    # wallbox meter): overcount and undercount must both be 0 for the period to
    # be cleanly billed.
    #   overcount  = Sum (session kWh > own meter span)   -> double-count
    #   undercount = Sum (meter jump between sessions)     -> missing charge
    _RTOL = 0.05
    recon_over = 0.0
    own = [s for s in all_sessions if not s["is_external"]]
    for s in own:
        ms, me, tk = s["meter_start"], s["meter_end"], s["total_kwh"]
        if ms is not None and me is not None and tk is not None:
            excess = float(tk) - (float(me) - float(ms))
            if excess > _RTOL:
                recon_over += excess
    # Gaps against the highest meter reached so far (fm.meter_gaps, as in the
    # gaps tab): when both cars charge on one day, one session can span the
    # other's, and "the previous session's end" is then not the meter.
    recon_gap = sum(d for _, _, d in fm.meter_gaps(
        [{"first_meter": s["meter_start"], "last_meter": s["meter_end"]} for s in own], min_kwh=_RTOL))
    recon = {
        "over_kwh": round(recon_over, 3),
        "gap_kwh": round(recon_gap, 3),
        "ok": recon_over <= _RTOL and recon_gap <= _RTOL,
    }

    # Own sessions (this vehicle)
    sessions = [s for s in all_sessions if s['vehicle_plate'] == filter_vehicle] if filter_vehicle else all_sessions

    # Excluded sessions (other vehicles)
    excluded_sessions = [s['session_number'] or str(s['id']) for s in all_sessions
                         if filter_vehicle and s['vehicle_plate'] != filter_vehicle
                         and not s['is_external']]

    # Determine flat rate
    tariff_date = f"{year}-{month.zfill(2)}-01" if year and month else None
    pauschale_kwh = fm.flat_rate(db, tariff_date)

    # Totals for own HOME sessions — including the ones reconstructed from
    # trips/SoC at the home location, which carry no meter span but did consume
    # own electricity. Their flat-rate cost falls out of the cost_pauschale
    # fallback below.
    own_sessions = [s for s in sessions if s['is_home_charge']]
    own_total_kwh = sum(s['total_kwh'] or 0 for s in own_sessions)
    own_total_cost = sum(
        (s['cost_pauschale'] if s['cost_pauschale'] is not None else (s['total_kwh'] or 0) * pauschale_kwh)
        for s in own_sessions
    )

    # Month/year display
    month_int = int(month) if month else datetime.now().month
    year_str = year or str(datetime.now().year)
    month_name = MONTH_NAMES_DE[month_int]
    # First and last day of the month
    last_day = calendar.monthrange(int(year_str), month_int)[1]
    date_from = f"01.{month.zfill(2)}.{year_str}" if month and year else ""
    date_to = f"{last_day}.{month.zfill(2)}.{year_str}" if month and year else ""

    # Date formatting per session
    sessions_fmt = []
    for s in sessions:
        d = dict(s)
        start_dt = None
        try:
            dt = datetime.fromisoformat(s['start_time'])
            start_dt = dt
            d['start_fmt'] = f"{dt.day}. {MONTH_NAMES_DE[dt.month]}"
            d['start_time_fmt'] = dt.strftime("%H:%M")
        except Exception:
            d['start_fmt'] = s['start_time'][:10] if s['start_time'] else '--'
            d['start_time_fmt'] = None
        d['end_next_day'] = False
        try:
            et = datetime.fromisoformat(s['end_time'])
            d['end_time_fmt'] = et.strftime("%H:%M")
            # Charge ends on a later calendar day -> mark the end time
            if start_dt is not None and et.date() != start_dt.date():
                d['end_next_day'] = True
        except Exception:
            d['end_time_fmt'] = None
        sessions_fmt.append(d)
    any_end_next_day = any(d.get('end_next_day') for d in sessions_fmt)

    # Available years
    years_rows = db.execute(
        "SELECT DISTINCT strftime('%Y', start_time) AS y FROM charge_sessions ORDER BY y DESC"
    ).fetchall()

    # Invoice defaults from settings
    settings_rows = db.execute("SELECT key, value FROM settings").fetchall()
    _f = _settings_fernet()
    settings = {}
    for r in settings_rows:
        v = r["value"]
        if r["key"] in ENCRYPTED_SETTINGS:
            v = _decrypt_setting(_f, v)
        settings[r["key"]] = v

    plate = vehicle['plate'] if vehicle else filter_vehicle
    model = vehicle['model'] if vehicle and vehicle['model'] else ''

    lang = get_language()
    if lang == "EN":
        _def_sender = 'Company / Name<br>Street No.<br>ZIP City'
        _def_recipient = 'Recipient Name<br>Street No.<br>ZIP City'
        _def_intro = (
            f'Dear Sir or Madam,<br><br>'
            f'please find below the charging cost statement for the above-mentioned '
            f'electric vehicle for the period from {date_from} to {date_to}.')
        _def_meter = (
            'Charging sessions are assigned to the vehicle by '
            'technical identification at the charging location.')
        _def_meter_info = 'Meter type / serial number'
        _def_tariff_ref = 'Electricity tariff as agreed'
        _def_data_info = (
            'Energy consumption is recorded by the electricity meter at the charging station. '
            'Meter readings are collected automatically and assigned to the vehicle. '
            'Costs are calculated based on the agreed electricity rate.')
    else:
        _def_sender = 'Firma / Name<br>Straße Nr.<br>PLZ Ort'
        _def_recipient = 'Empfänger Name<br>Straße Nr.<br>PLZ Ort'
        _def_intro = (
            'Sehr geehrte Damen und Herren,<br><br>'
            'hiermit erfolgt die Abrechnung der Stromkosten für das oben genannte '
            f'Elektrofahrzeug im Zeitraum vom {date_from} bis zum {date_to}.')
        _def_meter = (
            'Die Zuordnung der Ladevorgänge erfolgt durch technische '
            'Identifizierung des Fahrzeugs am Ladestandort.')
        _def_meter_info = 'Zähler Typ / Seriennummer'
        _def_tariff_ref = 'Stromtarif laut Vereinbarung'
        _def_data_info = (
            'Die Energiemenge wird über den Stromzähler an der Ladestation erfasst. '
            'Zählerstände werden automatisch ausgelesen und dem Fahrzeug zugeordnet. '
            'Die Kosten werden auf Basis des vereinbarten Strompreises berechnet.')

    invoice_sender = settings.get('invoice_sender', _def_sender)
    invoice_recipient = settings.get('invoice_recipient', _def_recipient)
    invoice_intro = settings.get('invoice_intro', _def_intro)
    invoice_meter_text = settings.get('invoice_meter_text', _def_meter)
    invoice_meter_info = settings.get('invoice_meter_info', _def_meter_info)
    invoice_tariff_ref = settings.get('invoice_tariff_ref', _def_tariff_ref)
    invoice_data_info = settings.get('invoice_data_info', _def_data_info)

    # Replace variables in stored texts
    replacements = {
        '{date_from}': date_from, '{date_to}': date_to,
        '{month}': month_name, '{year}': year_str,
        '{plate}': plate or '', '{model}': model,
        '{kwh}': f"{own_total_kwh:.2f}", '{cost}': f"{own_total_cost:.2f}",
        '{pauschale}': f"{pauschale_kwh:.2f}",
    }
    for key, val in replacements.items():
        invoice_intro = invoice_intro.replace(key, val)
        invoice_meter_text = invoice_meter_text.replace(key, val)

    # ── Optional print sections ──
    show_infos = request.args.get("infos") == "1"
    show_stats = request.args.get("stats") == "1"
    show_ext_stats = request.args.get("ext_stats") == "1"
    stats_data = {}
    if (show_stats or show_ext_stats) and filter_vehicle and year and month:
        device = active_device()
        end_ym = f"{year_str}-{month.zfill(2)}"

        # Find earliest month with data (trips or charges)
        first_trip = db.execute(
            "SELECT strftime('%Y-%m', start_time) AS ym FROM trips "
            "WHERE device = ? AND odo_end IS NOT NULL ORDER BY start_time LIMIT 1",
            (device,)
        ).fetchone()
        first_charge = db.execute(
            "SELECT strftime('%Y-%m', start_time) AS ym FROM charge_sessions "
            "WHERE vehicle_plate = ? AND " + _IS_HOME_SQL + " ORDER BY start_time LIMIT 1",
            (filter_vehicle,)
        ).fetchone()
        earliest_yms = [r['ym'] for r in [first_trip, first_charge] if r and r['ym']]
        # At most 12 months back
        end_val = int(year_str) * 12 + int(month)
        start_12 = end_val - 11
        start_12_ym = f"{(start_12 - 1) // 12}-{(start_12 - 1) % 12 + 1:02d}"
        first_ym = min(earliest_yms) if earliest_yms else start_12_ym
        if first_ym < start_12_ym:
            first_ym = start_12_ym

        # Build month list from first_ym to end_ym
        stats_months = []
        fy, fmo = int(first_ym[:4]), int(first_ym[5:7])
        ey, em = int(end_ym[:4]), int(end_ym[5:7])
        cy, cm = fy, fmo
        while cy * 12 + cm <= ey * 12 + em:
            stats_months.append((cy, cm))
            cm += 1
            if cm > 12:
                cm = 1
                cy += 1

        # Monthly odometer high-water mark from BOTH sources: the trips' odo_end
        # AND the odometer stamped on every charge. Trip tracking has gaps (the
        # telemetry stick is offline on long trips), the odometer does not — so
        # every distance below is derived from the odometer, never from
        # SUM(trips.distance_km), which silently undercounts a travel month.
        odo_rows = db.execute(
            "SELECT ym, MAX(o) AS max_odo FROM ("
            "  SELECT strftime('%Y-%m', start_time) AS ym, odo_end AS o FROM trips "
            "   WHERE device = ? AND odo_end IS NOT NULL "
            "  UNION ALL "
            "  SELECT strftime('%Y-%m', start_time) AS ym, odometer AS o FROM charge_sessions "
            "   WHERE vehicle_plate = ? AND odometer IS NOT NULL "
            ") GROUP BY ym ORDER BY ym",
            (device, filter_vehicle)
        ).fetchall()
        odo_by_month = {r['ym']: r['max_odo'] for r in odo_rows}

        # Driven km per month = odometer high-water delta vs the previous month
        # that has a reading. Months before the first reading stay unknown.
        driven_by_month = {}
        _prev_odo = None
        for _ym in sorted(odo_by_month):
            _cur_odo = odo_by_month[_ym]
            if _prev_odo is not None and _cur_odo > _prev_odo:
                driven_by_month[_ym] = _cur_odo - _prev_odo
            if _prev_odo is None or _cur_odo > _prev_odo:
                _prev_odo = _cur_odo

        # Charge energy from charge_sessions (meter-based, non-external)
        charge_rows = db.execute(
            "SELECT strftime('%Y-%m', start_time) AS ym, "
            "SUM(meter_end - meter_start) AS meter_kwh, "
            "SUM(total_kwh) AS sum_kwh, "
            "SUM(CASE WHEN cost_pauschale IS NOT NULL THEN cost_pauschale "
            "    ELSE COALESCE(total_kwh, 0) * ? END) AS sum_cost "
            "FROM charge_sessions WHERE vehicle_plate = ? AND " + _IS_HOME_SQL + " "
            "GROUP BY ym ORDER BY ym",
            (pauschale_kwh, filter_vehicle)
        ).fetchall()
        charge_by_month = {}
        for r in charge_rows:
            charge_by_month[r['ym']] = {'kwh': r['sum_kwh'] or 0, 'cost': r['sum_cost'] or 0}

        # Consumption per 100km via the reference interval walk (see
        # _interval_walk: home→home segments, SoC-corrected to REAL
        # consumption, 8-kWh floor against untracked external charging).
        all_sessions_ordered = db.execute(
            "SELECT strftime('%Y-%m', start_time) AS ym, odometer, " + _IS_HOME_SQL + " AS is_home_charge, "
            "total_kwh, soc_start, soc_end, "
            "CASE WHEN " + _IS_HOME_SQL + " THEN "
            "  CASE WHEN cost_pauschale IS NOT NULL THEN cost_pauschale "
            "       ELSE COALESCE(total_kwh, 0) * ? END "
            "ELSE COALESCE(cost_total, 0) END AS session_cost "
            "FROM charge_sessions "
            "WHERE vehicle_plate = ? AND odometer IS NOT NULL "
            "ORDER BY datetime(start_time)",
            (pauschale_kwh, filter_vehicle)
        ).fetchall()
        cons_by_month, incl_by_month = _interval_walk(all_sessions_ordered)

        # External-charge sums per month — for the ext-stats section below.
        ext_rows = db.execute(
            "SELECT strftime('%Y-%m', start_time) AS ym, "
            "SUM(total_kwh) AS kwh, SUM(cost_total) AS cost, COUNT(*) AS n "
            "FROM charge_sessions WHERE vehicle_plate = ? AND NOT " + _IS_HOME_SQL + " "
            "GROUP BY ym ORDER BY ym",
            (filter_vehicle,)
        ).fetchall()
        ext_by_month = {r['ym']: {'kwh': r['kwh'] or 0, 'cost': r['cost'] or 0, 'n': r['n']}
                        for r in ext_rows}

        monthly = []
        for sy, sm in stats_months:
            ym_key = f"{sy}-{sm:02d}"
            label = f"{sm:02d}/{sy}"
            m_kwh = charge_by_month.get(ym_key, {}).get('kwh', 0)
            m_cost = charge_by_month.get(ym_key, {}).get('cost', 0)
            m_odo = odo_by_month.get(ym_key)
            cm = cons_by_month.get(ym_key, {})
            cm_dist = cm.get('dist', 0)
            cm_kwh = cm.get('kwh', 0)
            cm_cost = cm.get('cost', 0)
            cons_100 = (cm_kwh / cm_dist * 100) if cm_dist > 0 and cm_kwh > 0 else None
            cost_100 = (cm_cost / cm_dist * 100) if cm_dist > 0 and cm_cost > 0 else None
            # No home charge in the month yet → no energy VALUE (the chart
            # draws an X); a literal 0 would claim a measured zero.
            monthly.append({
                'label': label,
                'kwh': round(m_kwh, 2) if ym_key in charge_by_month else None,
                'dist': round(cm_dist, 1),
                'cost': round(m_cost, 2), 'odo': round(m_odo) if m_odo else None,
                'cons_100': round(cons_100, 2) if cons_100 is not None else None,
                'cost_100': round(cost_100, 2) if cost_100 is not None else None,
            })

        # Current month values for tiles
        cur_ym = f"{year_str}-{month.zfill(2)}"
        cur_month = charge_by_month.get(cur_ym, {})

        # ── External-charge stats (table for current month) ──
        # €/100 km incl. external charges comes from the SAME interval walk as
        # the home chart (incl_by_month), just extended by every segment that
        # touches a tracked external charge. Invariant: a month without
        # external charges shows EXACTLY the home €/100km bar — the home model
        # is the reference, the ext chart adapts to it, never the reverse.
        # The externally driven km enter via the odometer stamps on the
        # external sessions, so a travel month keeps a sane value. Two earlier
        # attempts failed differently: home-interval km with FULL ext cost on
        # top (~61 EUR/100km), and SUM(trips.distance_km), which undercounts
        # when the telemetry has gaps (462 trip-km vs 2430 real km).
        monthly_with_ext = []
        for sy, sm in stats_months:
            ym_key = f"{sy}-{sm:02d}"
            label = f"{sm:02d}/{sy}"
            im = incl_by_month.get(ym_key, {})
            im_dist = im.get('dist', 0)
            im_cost = im.get('cost', 0)
            im_kwh = im.get('kwh', 0)
            cost_100_ext = (im_cost / im_dist * 100) if im_dist > 0 and im_cost > 0 else None
            cons_100_ext = (im_kwh / im_dist * 100) if im_dist > 0 and im_kwh > 0 else None
            monthly_with_ext.append({
                'label': label,
                'cost_100_ext': round(cost_100_ext, 2) if cost_100_ext is not None else None,
                'cons_100_ext': round(cons_100_ext, 2) if cons_100_ext is not None else None,
            })

        # Resolve operator (canonical name + icon) per external session.
        # The display name comes ONLY from the operators table — session.operator
        # is used as a lookup key but is otherwise too sparse to display directly.
        # Chain: 1) session.operator → operators by name (case-insensitive)
        #        2) session.location_name → charge_locations → operator (joined)
        op_resolve_map = {}  # name_lower → {'name': canonical, 'icon_url': ...}
        for r in db.execute("SELECT name, icon_filename FROM operators").fetchall():
            icon_url = f"/media/operator-icons/{r['icon_filename']}" if r['icon_filename'] else None
            op_resolve_map[r['name'].lower()] = {'name': r['name'], 'icon_url': icon_url}

        loc_resolve_map = {}  # location_name → {'name': operator-canonical-or-None, 'icon_url': ...}
        for cl in db.execute(
            "SELECT cl.name, cl.icon_filename, op.name AS op_name, "
            "op.icon_filename AS op_icon "
            "FROM charge_locations cl "
            "LEFT JOIN operators op ON cl.operator_id = op.id"
        ).fetchall():
            if cl['icon_filename']:
                icon_url = f"/media/charge-icons/{cl['icon_filename']}"
            elif cl['op_icon']:
                icon_url = f"/media/operator-icons/{cl['op_icon']}"
            else:
                icon_url = None
            loc_resolve_map[cl['name']] = {'name': cl['op_name'], 'icon_url': icon_url}

        ext_sessions_rows = db.execute(
            "SELECT id, start_time, location_name, operator, total_kwh, cost_total, odometer "
            "FROM charge_sessions "
            "WHERE vehicle_plate = ? AND NOT " + _IS_HOME_SQL + " "
            "AND strftime('%Y-%m', start_time) = ? "
            "ORDER BY datetime(start_time)",
            (filter_vehicle, cur_ym)
        ).fetchall()
        ext_sessions_fmt = []
        for s in ext_sessions_rows:
            d = dict(s)
            try:
                dt = datetime.fromisoformat(s['start_time'])
                d['date_fmt'] = dt.strftime("%d.%m.%Y")
                d['time_fmt'] = dt.strftime("%H:%M")
            except Exception:
                d['date_fmt'] = (s['start_time'] or '')[:10]
                d['time_fmt'] = ''

            icon_url = None
            operator_display = None
            if s['operator']:
                m = op_resolve_map.get(s['operator'].lower())
                if m:
                    icon_url = m.get('icon_url')
                    operator_display = m.get('name')
            if (icon_url is None and operator_display is None) and s['location_name']:
                m = loc_resolve_map.get(s['location_name'])
                if m:
                    icon_url = m.get('icon_url')
                    operator_display = m.get('name')

            d['icon_url'] = icon_url
            d['operator_display'] = operator_display
            d['odometer_print'] = (s['odometer'] + EXT_PRINT_ODO_OFFSET_KM
                                   if s['odometer'] is not None else None)
            ext_sessions_fmt.append(d)

        cur_ext = ext_by_month.get(cur_ym, {})

        # Driven distance for the current-month tile — odometer-based, like the
        # chart denominator. SUM(trips.distance_km) is NOT usable here: it drops
        # every kilometre the telemetry missed.
        month_total_dist = driven_by_month.get(cur_ym, 0)

        # Tariff comparison: home flat-rate vs. external avg in current month
        home_ct = pauschale_kwh * 100
        ext_kwh_cur = cur_ext.get('kwh', 0) or 0
        ext_cost_cur = cur_ext.get('cost', 0) or 0
        ext_ct = (ext_cost_cur / ext_kwh_cur * 100) if ext_kwh_cur > 0 else None
        home_kwh_cur = cur_month.get('kwh', 0) or 0
        diff_eur = (home_kwh_cur * (ext_ct - home_ct) / 100) if (ext_ct is not None and home_kwh_cur > 0) else None

        stats_data = {
            'monthly': monthly,
            'month_kwh': round(cur_month.get('kwh', 0), 2),
            # Tile = REAL km driven this month (odometer high-water delta,
            # matches the chart denominators) — NOT the charge-interval sum,
            # which leaks the previous month's km.
            'month_dist': round(month_total_dist, 1),
            'month_cost': round(cur_month.get('cost', 0), 2),
            'monthly_with_ext': monthly_with_ext,
            'ext_sessions': ext_sessions_fmt,
            'month_ext_kwh': round(cur_ext.get('kwh', 0), 2),
            'month_ext_cost': round(cur_ext.get('cost', 0), 2),
            'month_ext_count': cur_ext.get('n', 0),
            'month_total_dist': round(month_total_dist, 0),
            'home_ct_fmt': f"{home_ct:.1f}",
            'ext_ct_fmt': f"{ext_ct:.1f}" if ext_ct is not None else None,
            'home_kwh_cur_fmt': f"{home_kwh_cur:.1f}",
            'diff_eur_fmt': f"{diff_eur:+.2f}" if diff_eur is not None else None,
        }

    db.close()
    return render_template("charge_print.html",
                           sessions=sessions_fmt,
                           any_end_next_day=any_end_next_day,
                           vehicle=vehicle,
                           filter_vehicle=filter_vehicle,
                           month=month,
                           year=year_str,
                           years=[r['y'] for r in years_rows],
                           month_name=month_name,
                           date_from=date_from,
                           date_to=date_to,
                           pauschale_kwh=pauschale_kwh,
                           own_total_kwh=own_total_kwh,
                           own_total_cost=own_total_cost,
                           excluded_sessions=excluded_sessions,
                           invoice_sender=invoice_sender,
                           invoice_recipient=invoice_recipient,
                           invoice_intro=invoice_intro,
                           invoice_meter_text=invoice_meter_text,
                           invoice_meter_info=invoice_meter_info,
                           invoice_tariff_ref=invoice_tariff_ref,
                           invoice_data_info=invoice_data_info,
                           show_infos=show_infos,
                           show_stats=show_stats,
                           show_ext_stats=show_ext_stats,
                           stats_data=stats_data,
                           recon=recon,
                           now=datetime.now().strftime("%d.%m.%Y"))




# ── Charge session detail ───────────────────────────────────

@charges_bp.route("/charges/<int:session_id>")
def charge_detail(session_id):
    db = get_db()
    sess = db.execute("SELECT * FROM charge_sessions WHERE id = ?", (session_id,)).fetchone()
    if not sess:
        db.close()
        return redirect(url_for("charges.charges_list"))

    readings = fm.order_readings(db.execute(
        "SELECT * FROM charge_readings WHERE session_id = ? ORDER BY timestamp",
        (session_id,)
    ).fetchall())

    vehicles = db.execute("SELECT * FROM vehicles ORDER BY plate").fetchall()

    vehicle_name = sess['vehicle_plate']
    for v in vehicles:
        if v['plate'] == sess['vehicle_plate']:
            vehicle_name = v['name'] or v['plate']
            break

    # Current flat rate for cost display
    pauschale = fm.flat_rate(db, sess['start_time'])

    # All charge locations for the dropdown. Sort by distance from where the
    # vehicle actually was at charge time (end of the last trip before the
    # charge) so the most likely station sits on top; fall back to alphabetical.
    loc_rows = db.execute(
        """SELECT cl.name, cl.lat, cl.lon, op.name AS op_name FROM charge_locations cl
           LEFT JOIN operators op ON cl.operator_id = op.id"""
    ).fetchall()
    anchor_lat, anchor_lon = _vehicle_pos_at_charge(db, sess)
    if anchor_lat is not None and anchor_lon is not None:
        all_locations = sorted(
            ({"name": r["name"], "op_name": r["op_name"],
              "dist_km": haversine_m(anchor_lat, anchor_lon, r["lat"], r["lon"]) / 1000.0}
             for r in loc_rows),
            key=lambda x: x["dist_km"]
        )
    else:
        all_locations = sorted(
            ({"name": r["name"], "op_name": r["op_name"], "dist_km": None} for r in loc_rows),
            key=lambda x: (x["name"] or "").casefold()
        )

    # All known operators for datalist
    all_operators = db.execute(
        """SELECT DISTINCT operator FROM charge_locations WHERE operator IS NOT NULL AND operator != ''
           UNION
           SELECT DISTINCT operator FROM charge_sessions WHERE operator IS NOT NULL AND operator != ''
           ORDER BY operator"""
    ).fetchall()

    op_icon_map = _operator_icon_map(db)

    # Operator icon for this session: first session.operator → op_icon_map,
    # then fallback via location_name → charge_locations → operators JOIN
    session_op_info = op_icon_map.get((sess['operator'] or '').lower())
    if not session_op_info and sess['location_name']:
        loc_row = db.execute(
            """SELECT op.color, op.icon_filename
               FROM charge_locations cl
               LEFT JOIN operators op ON cl.operator_id = op.id
               WHERE cl.name = ? AND op.id IS NOT NULL LIMIT 1""",
            (sess['location_name'],)
        ).fetchone()
        if loc_row:
            icon_url = f"/media/operator-icons/{loc_row['icon_filename']}" if loc_row['icon_filename'] else None
            session_op_info = {"color": loc_row['color'] or '#8b949e', "icon_url": icon_url}

    # Location type (ac/dc) for warning indicator
    loc_type = 'ac'
    if sess['location_name']:
        lt_row = db.execute("SELECT type FROM charge_locations WHERE name = ?", (sess['location_name'],)).fetchone()
        if lt_row:
            loc_type = lt_row['type'] or 'ac'

    db.close()
    return render_template("charge_detail.html",
                           beleg_schemata=[s.name for s in beleg.SCHEMATA],
                           session=sess,
                           readings=readings,
                           vehicles=vehicles,
                           vehicle_name=vehicle_name,
                           pauschale=pauschale,
                           all_locations=all_locations,
                           all_operators=all_operators,
                           op_icon_map=op_icon_map,
                           session_op_info=session_op_info,
                           loc_type=loc_type)


# ── Charge readings JSON API ──────────────────────────────────

@charges_bp.route("/api/charge/sessions/<int:session_id>/readings")
@login_required
def charge_session_readings(session_id):
    """Return readings for a session as JSON."""
    db = get_db()
    rows = fm.order_readings(db.execute(
        "SELECT * FROM charge_readings WHERE session_id = ? ORDER BY timestamp",
        (session_id,)
    ).fetchall())
    db.close()
    return jsonify([{k: v for k, v in r.items() if k != "_utc"} for r in rows])


# ── Edit charge readings ─────────────────────────────────────

@charges_bp.route("/api/charge/readings", methods=["PUT"])
def insert_charge_reading():
    """Manually insert a single reading (fill data gaps)."""
    data = request.get_json()
    ts = data.get("timestamp")
    plate = data.get("vehicle_plate")
    if not ts or not plate:
        lang = get_language()
        _t = _translations.get(lang, _translations["DE"])
        return jsonify({"error": _t["charge_reading_ts_plate_required"]}), 400

    m_start = float(data["meter_start"]) if data.get("meter_start") not in ("", None) else None
    m_end = float(data["meter_end"]) if data.get("meter_end") not in ("", None) else None

    db = get_db()
    # Idempotent: do NOT re-insert an identical reading (same timestamp +
    # vehicle + meter values). Otherwise every repeated backfill/paste click
    # creates duplicates -> overcount + slow rebuild.
    dup = db.execute(
        "SELECT id FROM charge_readings WHERE timestamp = ? AND vehicle_plate = ? "
        "AND COALESCE(meter_start,-1) = COALESCE(?,-1) "
        "AND COALESCE(meter_end,-1) = COALESCE(?,-1) LIMIT 1",
        (ts, plate, m_start, m_end),
    ).fetchone()
    if dup:
        db.close()
        return jsonify({"ok": True, "id": dup["id"], "skipped": "duplicate"})
    # The wallbox meter is shared and only grows: a span another reading
    # already holds (of any vehicle) is energy that is counted already.
    if m_start is not None and m_end is not None and m_end > m_start:
        ov = db.execute(
            "SELECT id, timestamp, vehicle_plate, meter_start, meter_end FROM charge_readings "
            "WHERE meter_start IS NOT NULL AND meter_end IS NOT NULL "
            "AND meter_end > ? + 0.000001 AND meter_start < ? - 0.000001 ORDER BY meter_start LIMIT 5",
            (m_start, m_end)).fetchall()
        if ov:
            db.close()
            lang = get_language()
            _t = _translations.get(lang, _translations["DE"])
            return jsonify({"error": _t["charge_reading_meter_overlap"].format(
                            ", ".join(f"{r['vehicle_plate']} {r['timestamp'][:16]} "
                                      f"({r['meter_start']}–{r['meter_end']})" for r in ov)),
                            "overlaps": [dict(r) for r in ov]}), 409

    db.execute(
        """INSERT INTO charge_readings
           (timestamp, vehicle_plate, meter_start, meter_end, kwh,
            tibber_price, tibber_grundgebuehr, odometer, soc)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ts, plate, m_start, m_end,
         float(data.get("kwh") or 0),
         float(data["tibber_price"]) if data.get("tibber_price") not in ("", None) else None,
         float(data["tibber_grundgebuehr"]) if data.get("tibber_grundgebuehr") not in ("", None) else None,
         float(data["odometer"]) if data.get("odometer") not in ("", None) else None,
         _sanitize_soc(data.get("soc")) if data.get("soc") not in ("", None) else None)
    )
    db.commit()
    rid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    resp = _reading_rebuild_response(db, ts)
    resp["id"] = rid
    db.close()
    return jsonify(resp)


@charges_bp.route("/api/charge/readings/dedup", methods=["POST"])
@admin_required
def charge_readings_dedup():
    """Remove EXACT duplicate readings (same timestamp + vehicle + meter
    values), keeping the oldest (smallest id) per group, and readings whose
    meter span older readings already cover (shared meter, any vehicle). Such duplicates arise
    from repeated backfill/paste clicks and inflate the session kWh (overcount).
    Rebuild afterwards (respects the billing cutoff).
    """
    db = get_db()
    before = db.execute("SELECT COUNT(*) AS n FROM charge_readings").fetchone()["n"]
    db.execute(
        "DELETE FROM charge_readings WHERE id NOT IN ("
        "  SELECT MIN(id) FROM charge_readings"
        "  GROUP BY timestamp, vehicle_plate, meter_start, meter_end)"
    )
    # Also readings whose meter span earlier readings already cover (any
    # vehicle): energy counted twice, e.g. a filled-in phantom gap.
    covered = fm.covered_readings([dict(r) for r in db.execute(
        "SELECT id, meter_start, meter_end FROM charge_readings")])
    for i in range(0, len(covered), 500):
        part = covered[i:i + 500]
        db.execute(f"DELETE FROM charge_readings WHERE id IN ({','.join('?' * len(part))})", part)
    db.commit()
    removed = before - db.execute("SELECT COUNT(*) AS n FROM charge_readings").fetchone()["n"]
    if removed:
        rebuild_charge_sessions(db)   # fix session sums (with cutoff protection)
    db.close()
    return jsonify({"ok": True, "removed": removed})


@charges_bp.route("/api/charge/readings/relink", methods=["POST"])
@admin_required
def charge_readings_relink():
    """Repair orphaned readings whose session_id points at a DELETED session
    (dangling) — these otherwise slip through every filter: visible neither in a
    session nor as 'unassigned' (session_id IS NULL), and a normal rebuild does
    not touch them. We set their session_id to NULL and rebuild from the earliest
    affected reading. If that lies before the billing cutoff, it is reported (the
    cutoff must then be lowered first)."""
    db = get_db()
    # A reading is "broken-linked" if its session
    #   (a) no longer exists (deleted), OR
    #   (b) has a broken start_time: NULL/empty OR NOT between the earliest and
    #       latest reading of the session itself.
    # Case (b) also covers a ghost session with a WRONG (non-empty) date: it
    # escapes the rebuild DELETE (start_time >= floor) and does not sort into the
    # timeline -> readings stay stuck invisibly, the gap remains.
    _bad = (
        "cr.session_id IS NOT NULL AND ("
        " cs.id IS NULL"
        " OR cs.start_time IS NULL OR TRIM(COALESCE(cs.start_time,'')) = ''"
        " OR cs.start_time < (SELECT MIN(timestamp) FROM charge_readings WHERE session_id = cs.id)"
        " OR cs.start_time > (SELECT MAX(timestamp) FROM charge_readings WHERE session_id = cs.id))"
    )
    orphans = db.execute(
        "SELECT cr.id, cr.timestamp FROM charge_readings cr "
        "LEFT JOIN charge_sessions cs ON cs.id = cr.session_id "
        "WHERE " + _bad + " ORDER BY cr.timestamp"
    ).fetchall()
    n = len(orphans)
    earliest = orphans[0]["timestamp"] if orphans else None
    if n:
        db.execute(
            "UPDATE charge_readings SET session_id = NULL WHERE id IN ("
            "SELECT cr.id FROM charge_readings cr "
            "LEFT JOIN charge_sessions cs ON cs.id = cr.session_id WHERE " + _bad + ")"
        )
        # Remove empty/broken shells: automatic sessions without readings.
        db.execute(
            "DELETE FROM charge_sessions WHERE is_external = 0 AND id NOT IN "
            "(SELECT session_id FROM charge_readings WHERE session_id IS NOT NULL)"
        )
        db.commit()
    floor = _billed_floor(db)
    blocked = bool(floor and earliest and earliest < floor)
    if earliest and not blocked:
        rebuild_charge_sessions(db, since=earliest)
    db.close()
    return jsonify({"ok": True, "dangling": n, "earliest": earliest,
                    "billed_floor": floor, "blocked": blocked})


def _reading_rebuild_response(db, since):
    """Shared tail for the reading-mutating endpoints: propagate the change
    into the session aggregates via a scoped rebuild — without it the parent
    session keeps its stale total_kwh/cost_pauschale, which is exactly how
    deleted duplicate readings kept inflating the charge print. ``?rebuild=0``
    skips it for batch flows (admin editor / gap fill), which rebuild ONCE at
    the end; the response's ``since`` tells the caller what to rebuild from.
    A reading before the billed freeze is reported as frozen instead of
    churning every unbilled session for nothing."""
    resp = {"ok": True, "since": since}
    if request.args.get("rebuild") != "0":
        floor = _billed_floor(db)
        if floor and since and since < floor:
            resp["frozen"] = True
            resp["billed_floor"] = floor
        else:
            rebuild_charge_sessions(db, since=since)
            resp["rebuilt"] = True
    return resp


@charges_bp.route("/api/charge/readings/<int:reading_id>", methods=["DELETE"])
def delete_charge_reading(reading_id):
    """Delete a single reading."""
    db = get_db()
    row = db.execute("SELECT id, timestamp FROM charge_readings WHERE id = ?",
                     (reading_id,)).fetchone()
    if not row:
        db.close()
        lang = get_language()
        _t = _translations.get(lang, _translations["DE"])
        return jsonify({"error": _t["err_not_found"]}), 404
    ts = row["timestamp"]
    db.execute("DELETE FROM charge_readings WHERE id = ?", (reading_id,))
    db.commit()
    resp = _reading_rebuild_response(db, ts)
    db.close()
    return jsonify(resp)


@charges_bp.route("/api/charge/readings/<int:reading_id>", methods=["POST"])
def update_charge_reading(reading_id):
    data = request.get_json()
    db = get_db()

    old = db.execute("SELECT timestamp FROM charge_readings WHERE id = ?",
                     (reading_id,)).fetchone()

    allowed = ("vehicle_plate", "odometer", "meter_start", "meter_end",
               "kwh", "tibber_price", "tibber_grundgebuehr", "timestamp", "soc")
    sets = []
    params = []
    for field in allowed:
        if field in data:
            val = data[field]
            if field in ("odometer", "meter_start", "meter_end", "kwh",
                         "tibber_price", "tibber_grundgebuehr", "soc"):
                val = float(val) if val not in ("", None) else None
            sets.append(f"{field} = ?")
            params.append(val)

    if not sets:
        db.close()
        lang = get_language()
        _t = _translations.get(lang, _translations["DE"])
        return jsonify({"error": _t["err_no_fields"]}), 400

    params.append(reading_id)
    db.execute(f"UPDATE charge_readings SET {', '.join(sets)} WHERE id = ?", params)
    db.commit()
    # A timestamp edit moves the reading — both the old and the new position's
    # sessions are affected, so rebuild from the earlier of the two.
    _ts_candidates = [t for t in (old["timestamp"] if old else None,
                                  data.get("timestamp")) if t]
    resp = _reading_rebuild_response(db, min(_ts_candidates) if _ts_candidates else None)
    db.close()
    return jsonify(resp)


# ── Edit / delete session ─────────────────────────────────────

@charges_bp.route("/api/charge/sessions/<int:session_id>", methods=["POST"])
@login_required
def update_charge_session(session_id):
    """Edit charge session."""
    data = request.get_json()
    db = get_db()
    lang = get_language()
    _t = _translations.get(lang, _translations["DE"])
    sess = db.execute("SELECT * FROM charge_sessions WHERE id = ?", (session_id,)).fetchone()
    if not sess:
        db.close()
        return jsonify({"error": _t["err_session_not_found"]}), 404

    # External sessions are user-owned and stay editable by any logged-in user.
    # Automatic sessions are worker-built — only admins may backfill/correct them
    # (the data is otherwise authoritative); also keeps random users out.
    if not sess["is_external"] and not current_user.is_admin:
        db.close()
        return jsonify({"error": _t["charge_admin_rights_required"]}), 403

    allowed = ("start_time", "end_time", "total_kwh", "cost_total", "odometer", "note", "operator", "soc_start", "soc_end")
    sets = []
    params = []
    for field in allowed:
        if field in data:
            val = data[field]
            if field in ("total_kwh", "cost_total", "odometer", "soc_start", "soc_end"):
                val = float(val) if val not in ("", None) else None
            sets.append(f"{field} = ?")
            params.append(val)

    # Mark manually corrected odometer/SoC so a later rebuild keeps them instead
    # of reverting to NULL (the worker still backfills fields left empty). Clearing
    # a field drops its manual mark again.
    if any(f in data for f in ("odometer", "soc_start", "soc_end")):
        existing = sess["manual_fields"] if "manual_fields" in sess.keys() else None
        manual = {f for f in (existing or "").split(",") if f}
        for field in ("odometer", "soc_start", "soc_end"):
            if field in data:
                v = data[field]
                v = float(v) if v not in ("", None) else None
                manual.add(field) if v is not None else manual.discard(field)
        sets.append("manual_fields = ?")
        params.append(",".join(sorted(manual)) if manual else None)

    # An edited total_kwh must re-derive the flat-rate cost: the print's home
    # EUR/100km reads cost_pauschale, and a stale one silently diverges from
    # the corrected energy. External sessions carry user-entered cost_total
    # instead, and reconstructed home charges deliberately keep
    # cost_pauschale = NULL (the print falls back to kwh × tariff).
    if ("total_kwh" in data and not sess["is_external"]
            and sess["cost_pauschale"] is not None):
        _kwh_val = data["total_kwh"]
        _kwh_val = float(_kwh_val) if _kwh_val not in ("", None) else None
        if _kwh_val is not None:
            _pauschale = fm.flat_rate(db, data.get("start_time") or sess["start_time"])
            _new_cp = round(_kwh_val * _pauschale, 4)
            sets.append("cost_pauschale = ?")
            params.append(_new_cp)
            if sess["cost_tibber"] is not None:
                sets.append("cost_diff = ?")
                params.append(round(_new_cp - sess["cost_tibber"], 4))

    # Recalculate duration + avg_kw
    start = data.get("start_time", sess["start_time"])
    end = data.get("end_time", sess["end_time"])
    kwh = data.get("total_kwh", sess["total_kwh"])
    if start and end:
        try:
            dt_start = datetime.fromisoformat(start)
            dt_end = datetime.fromisoformat(end)
            dur = int((dt_end - dt_start).total_seconds() / 60)
            if dur > 0:
                sets.append("duration_minutes = ?")
                params.append(dur)
                if kwh:
                    avg = float(kwh) / (dur / 60)
                    sets.append("avg_kw = ?")
                    params.append(round(avg, 2))
        except Exception:
            log.warning("charge session edit: duration/avg_kw recalc failed (start=%r end=%r kwh=%r) — left unchanged",
                        start, end, kwh, exc_info=True)

    if sets:
        params.append(session_id)
        db.execute(f"UPDATE charge_sessions SET {', '.join(sets)} WHERE id = ?", params)
        db.commit()

    db.close()
    return jsonify({"ok": True})


@charges_bp.route("/api/charge/sessions/<int:session_id>/location", methods=["POST"])
@login_required
def update_charge_session_location(session_id):
    """Manually change charge session location, auto-assign operator from charge location."""
    data = request.get_json()
    name = data.get("location_name", "").strip() or None
    db = get_db()
    operator_name = None
    lat = None
    lon = None
    if name:
        row = db.execute(
            """SELECT cl.lat, cl.lon, op.name AS op_name FROM charge_locations cl
               LEFT JOIN operators op ON cl.operator_id = op.id
               WHERE cl.name = ? LIMIT 1""",
            (name,)
        ).fetchone()
        if row:
            operator_name = row["op_name"]
            lat = row["lat"]
            lon = row["lon"]
    sets = ["location_name = ?"]
    params = [name]
    if operator_name:
        sets.append("operator = ?")
        params.append(operator_name)
    if lat is not None and lon is not None:
        sets.append("lat = ?")
        sets.append("lon = ?")
        params += [lat, lon]
    params.append(session_id)
    db.execute(f"UPDATE charge_sessions SET {', '.join(sets)} WHERE id = ?", params)
    db.commit()
    db.close()
    return jsonify({"ok": True, "operator": operator_name})


@charges_bp.route("/api/charge/sessions/batch-location", methods=["POST"])
@login_required
def batch_update_charge_session_location():
    """Set location for multiple charge sessions at once."""
    data = request.get_json()
    ids = data.get("ids", [])
    name = data.get("location_name", "").strip() or None
    if not ids:
        lang = get_language()
        _t = _translations.get(lang, _translations["DE"])
        return jsonify({"ok": False, "error": _t["err_no_ids"]}), 400
    db = get_db()
    operator_name = None
    lat = None
    lon = None
    if name:
        row = db.execute(
            """SELECT cl.lat, cl.lon, op.name AS op_name FROM charge_locations cl
               LEFT JOIN operators op ON cl.operator_id = op.id
               WHERE cl.name = ? LIMIT 1""",
            (name,)
        ).fetchone()
        if row:
            operator_name = row["op_name"]
            lat = row["lat"]
            lon = row["lon"]
    sets = ["location_name = ?"]
    base_params = [name]
    if operator_name:
        sets.append("operator = ?")
        base_params.append(operator_name)
    if lat is not None and lon is not None:
        sets.append("lat = ?")
        sets.append("lon = ?")
        base_params += [lat, lon]
    for sid in ids:
        db.execute(f"UPDATE charge_sessions SET {', '.join(sets)} WHERE id = ?", base_params + [sid])
    db.commit()
    db.close()
    return jsonify({"ok": True, "updated": len(ids), "operator": operator_name})


def _nummern_label(nummern):
    """"246-249" for a run of consecutive numbers, else "246,248"."""
    try:
        ints = [int(n) for n in nummern]
    except (TypeError, ValueError):
        return ",".join(str(n) for n in nummern)
    if ints == list(range(ints[0], ints[0] + len(ints))):
        return f"{ints[0]}-{ints[-1]}"
    return ",".join(str(n) for n in ints)


def _fehler(schluessel, status):
    _t = _translations.get(get_language(), _translations["DE"])
    return jsonify({"ok": False, "error": _t[schluessel]}), status


@charges_bp.route("/api/charge/sessions/merge", methods=["POST"])
@login_required
def merge_charge_sessions():
    """Join consecutive charges of one vehicle into one — the car stayed
    plugged in for days and the >12 h gap rule split it. Stored as a bridge
    so every rebuild keeps them together; the merged charge is numbered with
    the range of the old numbers ("246-249")."""
    import json as _json
    data = request.get_json(silent=True) or {}
    ids = [int(i) for i in data.get("ids", []) if str(i).isdigit()]
    if len(ids) < 2:
        return _fehler("charges_merge_need_two", 400)
    db = get_db()
    try:
        rows = db.execute(
            f"SELECT id, vehicle_plate, start_time, end_time, session_number, is_external FROM charge_sessions "
            f"WHERE id IN ({','.join('?' * len(ids))}) ORDER BY datetime(start_time)", ids).fetchall()
        if len(rows) != len(ids) or any(r["is_external"] for r in rows) or len({r["vehicle_plate"] for r in rows}) != 1:
            return _fehler("charges_merge_not_adjacent", 409)
        plate = rows[0]["vehicle_plate"]
        floor = _billed_floor(db)
        if floor and rows[0]["start_time"] < floor:
            return _fehler("charges_merge_billed", 409)
        dazwischen = db.execute(
            "SELECT COUNT(*) FROM charge_sessions WHERE vehicle_plate = ? AND is_external = 0 "
            "AND datetime(start_time) >= datetime(?) AND datetime(start_time) <= datetime(?)",
            (plate, rows[0]["start_time"], rows[-1]["start_time"])).fetchone()[0]
        if dazwischen != len(rows):
            return _fehler("charges_merge_not_adjacent", 409)
        span = db.execute(
            f"SELECT MIN(timestamp) AS a, MAX(timestamp) AS b FROM charge_readings "
            f"WHERE session_id IN ({','.join('?' * len(ids))})", ids).fetchone()
        von, bis = span["a"] or rows[0]["start_time"], span["b"] or rows[-1]["end_time"]
        nummern = [r["session_number"] or str(r["id"]) for r in rows]
        label = _nummern_label(nummern)
        db.execute("INSERT INTO charge_session_bridges (vehicle_plate, from_ts, to_ts, label, numbers) VALUES (?, ?, ?, ?, ?)",
                   (plate, von, bis, label, _json.dumps(nummern)))
        db.commit()
        rebuild_charge_sessions(db, since=rows[0]["start_time"])
        neu = db.execute("SELECT id FROM charge_sessions WHERE vehicle_plate = ? AND session_number = ?",
                         (plate, label)).fetchone()
        return jsonify({"ok": True, "id": neu["id"] if neu else None, "session_number": label})
    finally:
        db.close()


@charges_bp.route("/api/charge/sessions/<int:session_id>/unmerge", methods=["POST"])
@login_required
def unmerge_charge_session(session_id):
    """Undo a merge: drop the bridge, rebuild, give the parts their old numbers."""
    import json as _json
    db = get_db()
    try:
        s = db.execute("SELECT * FROM charge_sessions WHERE id = ?", (session_id,)).fetchone()
        if not s:
            return _fehler("err_not_found", 404) if "err_not_found" in _translations["DE"] else (jsonify({"ok": False}), 404)
        b = db.execute("SELECT * FROM charge_session_bridges WHERE vehicle_plate = ? AND label = ?",
                       (s["vehicle_plate"], s["session_number"])).fetchone()
        if not b:
            return _fehler("charges_unmerge_none", 409)
        floor = _billed_floor(db)
        if floor and s["start_time"] < floor:
            return _fehler("charges_merge_billed", 409)
        db.execute("DELETE FROM charge_session_bridges WHERE id = ?", (b["id"],))
        db.commit()
        rebuild_charge_sessions(db, since=s["start_time"])
        teile = db.execute(
            "SELECT DISTINCT cs.id FROM charge_sessions cs JOIN charge_readings cr ON cr.session_id = cs.id "
            "WHERE cs.vehicle_plate = ? AND cs.is_external = 0 AND cr.timestamp >= ? AND cr.timestamp <= ? "
            "ORDER BY datetime(cs.start_time)", (s["vehicle_plate"], b["from_ts"], b["to_ts"])).fetchall()
        nummern = _json.loads(b["numbers"] or "[]")
        if len(nummern) == len(teile):
            for teil, nummer in zip(teile, nummern):
                db.execute("UPDATE charge_sessions SET session_number = ? WHERE id = ?", (nummer, teil["id"]))
            db.commit()
        return jsonify({"ok": True, "ids": [t["id"] for t in teile]})
    finally:
        db.close()


@charges_bp.route("/api/charge/sessions/list")
@login_required
def list_charge_sessions():
    """List charge sessions with optional date filter for admin reassignment."""
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")
    db = get_db()
    sql = """SELECT cs.id, cs.start_time, cs.end_time, cs.total_kwh, cs.vehicle_plate,
                    cs.location_name, cs.odometer, cs.is_external, cs.session_number
             FROM charge_sessions cs WHERE 1=1"""
    params = []
    if date_from:
        sql += " AND cs.start_time >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND cs.start_time <= ?"
        params.append(date_to + "T23:59:59")
    sql += " ORDER BY cs.start_time DESC"
    rows = db.execute(sql, params).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


def _with_meters(db, sessions):
    """Sessions as dicts with first_meter / last_meter of their readings in
    true time order (the double hour when the clocks go back included)."""
    m = fm.session_meters(db.execute(
        "SELECT id, session_id, timestamp, created_at, meter_start, meter_end "
        "FROM charge_readings WHERE session_id IS NOT NULL").fetchall())
    out = []
    for s in sessions:
        d = dict(s)
        d["first_meter"], d["last_meter"] = m.get(d["id"], (None, None))
        out.append(d)
    return out


@charges_bp.route("/api/charge/meter-gaps")
@login_required
def charge_meter_gaps():
    """Quantify lost energy from the cleanup_db data-loss bug: the wallbox
    meter is cumulative, so any positive jump from one session's last
    meter_end to the next session's first meter_start is kWh that was metered
    (and billed) but no longer has readings in IDMate."""
    db = get_db()
    sessions = db.execute("""
        SELECT cs.id, cs.start_time, cs.end_time, cs.vehicle_plate, cs.session_number
        FROM charge_sessions cs
        WHERE cs.is_external = 0
        ORDER BY datetime(cs.start_time)
    """).fetchall()
    sessions = _with_meters(db, sessions)
    gaps = []
    total = 0.0
    # > 50 Wh — ignore rounding noise; against the highest meter so far, so a
    # charge of the other car spanning this one opens no phantom gap
    for prev, s, delta in fm.meter_gaps(sessions, min_kwh=0.0501):
        gaps.append({
            "from": prev["end_time"], "to": s["start_time"],
            "kwh_missing": delta,
            "between": f"#{prev['session_number'] or prev['id']} → #{s['session_number'] or s['id']}",
            "plates": f"{prev['vehicle_plate']} → {s['vehicle_plate']}",
        })
        total += delta
    db.close()
    return jsonify({
        "gaps": gaps, "total_kwh_missing": round(total, 3), "count": len(gaps),
    })


@charges_bp.route("/api/charge/raw")
@admin_required
def charge_raw():
    """Raw dump for diagnostics: all charge_readings + charge_sessions in a time
    range (unfiltered, no grouping). Call:
    /api/charge/raw?from=2026-06-10&to=2026-06-10  (date or ISO timestamp).
    Admin-only, read-only."""
    date_from = request.args.get("from", "").strip() or "0000"
    date_to = request.args.get("to", "").strip()
    to_bound = (date_to + "T23:59:59") if date_to else "9999"
    db = get_db()
    readings = db.execute(
        "SELECT * FROM charge_readings WHERE timestamp >= ? AND timestamp <= ? "
        "ORDER BY timestamp", (date_from, to_bound)
    ).fetchall()
    sessions = db.execute(
        "SELECT * FROM charge_sessions WHERE start_time >= ? AND start_time <= ? "
        "ORDER BY datetime(start_time)", (date_from, to_bound)
    ).fetchall()
    db.close()
    return jsonify({
        "readings": [dict(r) for r in readings],
        "sessions": [dict(s) for s in sessions],
        "n_readings": len(readings),
        "n_sessions": len(sessions),
    })


@charges_bp.route("/api/charge/gaps")
@login_required
def charge_gaps():
    """Real charge gaps for the gaps tab — a WHOLE missing charge.

    The wallbox meter is cumulative and SHARED across all vehicles, so a gap is a
    property of the global meter timeline, NOT of one car: a positive jump from
    one session's last meter_end to the *next* session's first meter_start (over
    ALL vehicles, by time) = kWh that was metered (and billed) but has no readings
    in IDMate. Detection therefore must NOT filter by vehicle — otherwise car A
    would see car B's perfectly normal charge as a "missing gap". before/after
    show whichever cars actually charged around the hole. min_kwh filters
    small/large; the date range filters by the gap's to_time.
    """
    date_from = request.args.get("from", "").strip()
    date_to = request.args.get("to", "").strip()
    try:
        min_kwh = float(request.args.get("min_kwh", "0.05") or 0.05)
    except (TypeError, ValueError):
        min_kwh = 0.05
    to_bound = (date_to + "T23:59:59") if date_to else ""
    db = get_db()

    # All non-external sessions across all vehicles, by time (no vehicle filter so
    # a shared-meter charge by the other car is never mistaken for a gap). No date
    # filter on the SQL either — the "before" session just outside the range still
    # provides context; the resulting gaps are filtered by to_time below.
    sessions = db.execute("""
        SELECT cs.id, cs.start_time, cs.end_time, cs.vehicle_plate, cs.session_number,
               cs.location_name, cs.total_kwh
        FROM charge_sessions cs
        WHERE cs.is_external = 0
        ORDER BY datetime(cs.start_time)
    """).fetchall()
    sessions = _with_meters(db, sessions)

    def _ctx(s, meter_key):
        return {"id": s["id"], "num": s["session_number"], "plate": s["vehicle_plate"],
                "time": s["end_time"] if meter_key == "last_meter" else s["start_time"],
                "meter": s[meter_key], "location": s["location_name"], "kwh": s["total_kwh"]}

    # Hard lower bound: everything BEFORE the billing cutoff is billed/frozen
    # (often broken legacy import data) and no longer correctable — so don't even
    # show it as a gap/overcount.
    bfloor = _billed_floor(db) or ""

    def _in_range(t):
        return ((not bfloor or (t or "") >= bfloor)
                and (not date_from or (t or "") >= date_from)
                and (not to_bound or (t or "") <= to_bound))

    gaps = []
    overcounts = []
    total_missing = 0.0
    for s in sessions:
        # Overcount guard: a session's kWh (sum of reading deltas) must not
        # exceed its own meter span (last meter_end - first meter_start). If it
        # does, there are duplicate/overlapping readings -> too much would be
        # billed. (Structurally impossible otherwise, since the kWh come from
        # exactly these meter values.)
        if (s["first_meter"] is not None and s["last_meter"] is not None
                and s["total_kwh"] is not None and _in_range(s["start_time"])):
            span = float(s["last_meter"]) - float(s["first_meter"])
            excess = float(s["total_kwh"]) - span
            if excess > 0.05:
                overcounts.append({
                    "id": s["id"], "num": s["session_number"], "plate": s["vehicle_plate"],
                    "time": s["start_time"], "total_kwh": round(float(s["total_kwh"]), 3),
                    "span": round(span, 3), "excess": round(excess, 3),
                })


    # Gaps against the highest meter reached so far (fm.meter_gaps): the
    # 10.10. ID.7 charge 01:15-17:10 around a Model 3 charge at 11:15 made the
    # Model 3's next charge look 53.93 kWh short — filling that phantom gap
    # counted the ID.7's energy twice.
    for prev, s, delta in fm.meter_gaps(sessions, min_kwh=min_kwh):
        if _in_range(s["start_time"]):
            gaps.append({
                "kwh_missing": delta,
                "from_time": prev["end_time"], "to_time": s["start_time"],
                "before": _ctx(prev, "last_meter"), "after": _ctx(s, "first_meter"),
            })
            total_missing += delta

    # Orphan readings (exist but linked to no session) — surfaced as a hint with a
    # one-click rebuild; not vehicle-scoped.
    unassigned = db.execute(
        "SELECT COUNT(*) AS n FROM charge_readings WHERE session_id IS NULL"
    ).fetchone()["n"]

    # Billing cutoff: sessions/readings before this date are NOT touched by the
    # rebuild (frozen). If a gap lies before it, backfilling cannot take effect —
    # the UI must say so clearly instead of failing silently.
    bu = db.execute("SELECT value FROM settings WHERE key = 'charge_billed_until'").fetchone()
    billed_until = (bu["value"] if bu and bu["value"] else "")

    db.close()
    return jsonify({
        "gaps": gaps,
        "overcounts": overcounts,
        "total_kwh_missing": round(total_missing, 3),
        "unassigned": unassigned,
        "billed_until": billed_until,
    })


@charges_bp.route("/api/charge/readings/unassigned")
@login_required
def list_unassigned_readings():
    """Readings in the date range whose session_id is NULL — orphans that no
    session points at. Lets the admin see whether a missing reading (e.g. an
    8 a.m. handshake) is in the DB at all and just unlinked, or never arrived."""
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")
    db = get_db()
    sql = "SELECT * FROM charge_readings WHERE session_id IS NULL"
    params = []
    if date_from:
        sql += " AND timestamp >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND timestamp <= ?"
        params.append(date_to + "T23:59:59")
    sql += " ORDER BY timestamp"
    rows = db.execute(sql, params).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@charges_bp.route("/api/charge/sessions/batch-vehicle", methods=["POST"])
@login_required
def batch_update_charge_session_vehicle():
    """Reassign vehicle_plate for multiple charge sessions."""
    data = request.get_json()
    ids = data.get("ids", [])
    plate = data.get("vehicle_plate", "").strip()
    lang = get_language()
    _t = _translations.get(lang, _translations["DE"])
    if not ids:
        return jsonify({"ok": False, "error": _t["err_no_ids"]}), 400
    if not plate:
        return jsonify({"ok": False, "error": _t["charge_no_plate"]}), 400
    db = get_db()
    veh = db.execute("SELECT plate FROM vehicles WHERE plate = ?", (plate,)).fetchone()
    if not veh:
        db.close()
        return jsonify({"ok": False, "error": _t["err_vehicle_not_found"]}), 404
    for sid in ids:
        db.execute("UPDATE charge_sessions SET vehicle_plate = ? WHERE id = ?", (plate, sid))
    db.commit()
    db.close()
    return jsonify({"ok": True, "updated": len(ids)})


@charges_bp.route("/api/charge/sessions/<int:session_id>", methods=["DELETE"])
@login_required
def delete_charge_session(session_id):
    """Delete charge session (external only)."""
    db = get_db()
    sess = db.execute("SELECT * FROM charge_sessions WHERE id = ?", (session_id,)).fetchone()
    lang = get_language()
    _t = _translations.get(lang, _translations["DE"])
    if not sess:
        db.close()
        return jsonify({"error": _t["err_session_not_found"]}), 404
    if not sess["is_external"]:
        db.close()
        return jsonify({"error": _t["charge_only_external_deletable"]}), 400

    db.execute("DELETE FROM charge_sessions WHERE id = ?", (session_id,))
    db.commit()
    db.close()
    return jsonify({"ok": True})


# ── Recalculate sessions ─────────────────────────────────────

@charges_bp.route("/api/charge/session-start", methods=["POST"])
@login_required
def charge_session_start():
    data = request.get_json()
    val = int(data.get("value", 1))
    db = get_db()
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('charge_session_start', ?)", (str(val),))
    db.commit()
    rebuild_charge_sessions(db)
    db.close()
    return jsonify({"ok": True})


@charges_bp.route("/api/charge/billed-until", methods=["POST"])
@login_required
def charge_billed_until():
    """Set (or clear) the 'billed until' cutoff date. Sessions on/before this
    date are frozen and never rebuilt. Strictly manual — never auto-set."""
    data = request.get_json() or {}
    val = str(data.get("value", "")).strip()[:10]  # 'YYYY-MM-DD' or '' to clear
    if val:
        try:
            datetime.fromisoformat(val)
        except ValueError:
            lang = get_language()
            _t = _translations.get(lang, _translations["DE"])
            return jsonify({"error": _t["err_invalid_date"]}), 400
    db = get_db()
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('charge_billed_until', ?)", (val,))
    db.commit()
    db.close()
    return jsonify({"ok": True, "value": val})


@charges_bp.route("/api/charge/rebuild", methods=["POST"])
@admin_required  # FIXES 15.2: globally rebuilding sessions is system maintenance — admin only
def charge_rebuild():
    # Optional scoped rebuild: 'since' (ISO timestamp) rebuilds only from this
    # point on (like the webhook) — drastically faster than the full rebuild,
    # because not every session of the whole period + its Influx SoC queries run
    # again. Used by gap-filling, which only backfills one specific charge.
    data = request.get_json(silent=True) or {}
    since = (data.get("since") or "").strip() or None
    db = get_db()
    # The rebuild itself never touches billed sessions — but the CALLER asked
    # for `since`; when part of that range lies before the freeze, say so
    # instead of reporting an unqualified success (the classic trap: deleted
    # duplicate readings, rebuild "ok", inflated frozen session lives on).
    floor = _billed_floor(db)
    blocked = bool(floor and since and since < floor)
    rebuild_charge_sessions(db, since=since)
    # Return all session IDs so client can find the right one
    rows = db.execute("SELECT id, vehicle_plate, odometer FROM charge_sessions ORDER BY id").fetchall()
    sessions = [{"id": r["id"], "plate": r["vehicle_plate"], "odo": r["odometer"]} for r in rows]
    db.close()
    return jsonify({"ok": True, "sessions": sessions,
                    "billed_floor": floor, "blocked": blocked})


@charges_bp.route("/api/charge/recalc", methods=["POST"])
@login_required
def charge_recalc():
    """Recalculate session numbers and distances without full rebuild.

    Respects the billed freeze exactly like rebuild_charge_sessions: numbers
    already assigned to sessions stay fixed (stable for invoices), only freshly
    built sessions (session_number IS NULL) get the next free number; the
    distance recalc skips billed sessions before the freeze floor."""
    db = get_db()

    floor = _billed_floor(db)

    # Assign session numbers — PERSISTENT: keep existing numbers (incl. billed
    # ones), only NULL/unnumbered non-external sessions get the next free number.
    start_row = db.execute("SELECT value FROM settings WHERE key = 'charge_session_start'").fetchone()
    start_num = int(start_row['value']) if start_row else 1
    maxn_row = db.execute(
        "SELECT MAX(CAST(session_number AS INTEGER)) AS m FROM charge_sessions "
        "WHERE is_external = 0 AND session_number IS NOT NULL AND session_number GLOB '[0-9]*'"
    ).fetchone()
    next_num = (maxn_row["m"] + 1) if (maxn_row and maxn_row["m"] is not None) else start_num
    for s in db.execute(
        "SELECT id FROM charge_sessions WHERE is_external = 0 AND session_number IS NULL "
        "ORDER BY datetime(start_time)"
    ).fetchall():
        db.execute("UPDATE charge_sessions SET session_number = ? WHERE id = ?",
                   (str(next_num), s['id']))
        next_num += 1
    # External sessions never carry a number — except the "TM-…" marker of a
    # TeslaMate import, which tells the energy model its kWh are battery side.
    db.execute("UPDATE charge_sessions SET session_number = NULL WHERE is_external = 1 "
               "AND (session_number IS NULL OR session_number NOT LIKE 'TM-%')")

    # Recalculate distances — read the full chain so each session sees its
    # successor, but never overwrite the distance of a frozen/billed session.
    vehicles = db.execute("SELECT DISTINCT vehicle_plate FROM charge_sessions").fetchall()
    for v in vehicles:
        sessions = db.execute(
            "SELECT id, odometer, start_time FROM charge_sessions WHERE vehicle_plate = ? "
            "ORDER BY datetime(start_time)",
            (v['vehicle_plate'],)
        ).fetchall()
        for i, s in enumerate(sessions):
            if floor and s['start_time'] and s['start_time'] < floor:
                continue  # preserved / billed → don't touch its distance
            if s['odometer'] is not None and i + 1 < len(sessions):
                next_s = sessions[i + 1]
                if next_s['odometer'] is not None:
                    dist = next_s['odometer'] - s['odometer']
                    db.execute("UPDATE charge_sessions SET distance = ? WHERE id = ?",
                               (round(dist, 1), s['id']))
                    continue
            db.execute("UPDATE charge_sessions SET distance = NULL WHERE id = ?", (s['id'],))

    db.commit()
    db.close()
    return jsonify({"ok": True})


# ── CSV import for charge data ───────────────────────────────

@charges_bp.route("/api/charge/import", methods=["POST"])
@login_required
def charge_import():
    """Import charge readings from CSV (German number format).

    Expected columns:
      Datum, Auto, Zaehlerstand_Anfang, Zaehlerstand_Ende,
      Verbrauch_kWh, Tibber_Preis_kWh, Tibber_Grundgebuehr_15m, Odometer
    """
    f = request.files.get("file")
    if not f:
        lang = get_language()
        _t = _translations.get(lang, _translations["DE"])
        return jsonify({"error": _t["err_no_file"]}), 400

    text = f.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    db = get_db()
    count = 0
    skipped = 0
    errors = []
    min_ts = None  # earliest imported reading → scope the rebuild

    for i, row in enumerate(reader, start=2):
        timestamp = row.get("Datum", "").strip()
        if not timestamp:
            continue

        vehicle = row.get("Auto", "").strip()
        if not vehicle or vehicle.lower() in ("free", "error"):
            skipped += 1
            continue

        kwh = _parse_german_num(row.get("Verbrauch_kWh", ""))
        if kwh is None or kwh <= 0:
            skipped += 1
            continue

        # Duplicate check: same timestamp + vehicle
        existing = db.execute(
            "SELECT id FROM charge_readings WHERE timestamp = ? AND vehicle_plate = ?",
            (timestamp, vehicle)
        ).fetchone()
        if existing:
            skipped += 1
            continue

        meter_start = _parse_german_num(row.get("Zaehlerstand_Anfang", ""))
        meter_end = _parse_german_num(row.get("Zaehlerstand_Ende", ""))
        tibber_price = _parse_german_num(row.get("Tibber_Preis_kWh", ""))
        tibber_grund = _parse_german_num(row.get("Tibber_Grundgebuehr_15m", ""))
        odometer = _parse_german_num(row.get("Odometer", ""))

        try:
            db.execute("""
                INSERT INTO charge_readings
                (timestamp, vehicle_plate, meter_start, meter_end, kwh,
                 tibber_price, tibber_grundgebuehr, odometer)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (timestamp, vehicle, meter_start, meter_end,
                  round(kwh, 4), tibber_price, tibber_grund, odometer))
            count += 1
            if min_ts is None or timestamp < min_ts:
                min_ts = timestamp
        except Exception as e:
            errors.append(f"Row {i}: {e}")

    db.commit()

    # Rebuild sessions after import — scoped to the imported range (never touches
    # billed/older sessions). Falls back to full when nothing usable was found.
    rebuild_charge_sessions(db, since=min_ts)
    db.close()

    return jsonify({
        "ok": True,
        "imported": count,
        "skipped": skipped,
        "errors": errors[:10]
    })


# ── Manually record external charge ──────────────────────────

def soc_um_ladung(db, plate, start_time, end_time):
    """What the trips around an external charge know about it: the SoC the car
    was parked at (soc_end of the last trip ending before the charge), the SoC
    it drove off with (soc_start of the first trip after), plus the odometer
    from the same trips. Same anchors rebuild_charge_sessions uses for wallbox
    sessions — an external charge deserves the same prefill instead of two
    empty fields. Every value may be None."""
    leer = {"soc_start": None, "soc_end": None, "odometer": None}
    if not plate or not start_time:
        return leer
    dev = db.execute("SELECT device FROM vehicles WHERE plate = ?", (plate,)).fetchone()
    device = dev["device"] if dev else None
    if not device:
        return leer
    vor = db.execute(
        "SELECT odo_end, soc_end FROM trips WHERE device = ? "
        "AND end_time IS NOT NULL AND datetime(end_time) <= datetime(?) "
        "ORDER BY datetime(end_time) DESC LIMIT 1",
        (device, start_time),
    ).fetchone()
    nach = db.execute(
        "SELECT odo_start, soc_start FROM trips WHERE device = ? "
        "AND start_time IS NOT NULL AND datetime(start_time) >= datetime(?) "
        "ORDER BY datetime(start_time) ASC LIMIT 1",
        (device, end_time or start_time),
    ).fetchone()
    return {
        "soc_start": vor["soc_end"] if vor and vor["soc_end"] is not None else None,
        "soc_end": nach["soc_start"] if nach and nach["soc_start"] is not None else None,
        "odometer": (round(vor["odo_end"]) if vor and vor["odo_end"] is not None
                     else (round(nach["odo_start"]) if nach and nach["odo_start"] is not None else None)),
    }


@charges_bp.route("/api/charge/external/hint")
@login_required
def charge_external_hint():
    """Prefill for the external-charge form once start and end are typed:
    SoC before/after and odometer from the neighbouring trips, plus the kWh
    those SoC values imply (a hint, not a value — the meter of the charger
    is the truth)."""
    start = (request.args.get("start") or "").replace("T", " ")[:16]
    end = (request.args.get("end") or "").replace("T", " ")[:16] or start
    db = get_db()
    try:
        v = active_vehicle(db)
        plate = v["plate"] if v else ""
        hint = soc_um_ladung(db, plate, start, end)
        hint["kwh_hint"] = None
        if hint["soc_start"] is not None and hint["soc_end"] is not None and hint["soc_end"] > hint["soc_start"]:
            cap = get_bat_kwh(db, v.get("device") if v else None, at_time=start.replace(" ", "T")) if v else None
            if cap:
                hint["kwh_hint"] = round((hint["soc_end"] - hint["soc_start"]) / 100.0 * cap, 1)
    finally:
        db.close()
    return jsonify(hint)


@charges_bp.route("/api/charge/external/beleg", methods=["POST"])
@login_required
def charge_external_beleg():
    """Screenshot (Datei) oder Text eines Ladebelegs -> Felder fürs Formular.
    Die Erkennung selbst steht in beleg.py; hier nur Ein- und Ausgang."""
    lang = get_language()
    _t = _translations.get(lang, _translations["DE"])
    f = request.files.get("file")
    if f is not None and f.filename is not None:
        if not beleg.ocr_verfuegbar():
            return jsonify({"error": _t["charges_beleg_ocr_fehlt"]}), 503
        bild = f.read()
        try:
            text = beleg.text_aus_bild(bild)
        except Exception as fehler:  # noqa: BLE001 – Bildformat, Tesseract: alles ein "nicht lesbar"
            log.warning("Ladebeleg: Bild nicht lesbar – %s", fehler)
            return jsonify({"error": _t["charges_beleg_unlesbar"]}), 400
    else:
        bild = None
        data = request.get_json(silent=True) or {}
        text = str(data.get("text") or request.form.get("text") or "")
    if not text.strip():
        return jsonify({"error": _t["err_no_file"]}), 400
    db = get_db()
    try:
        bekannte = [r["name"] for r in db.execute("SELECT name FROM operators ORDER BY name").fetchall()]
    finally:
        db.close()
    beschriftung = {"kwh": "kWh", "start": _t["common_start_time"], "end": _t["common_end_time"]}
    felder = beleg.felder_aus_text(text, bekannte, beschriftung)
    if bild is not None and (felder.get("cost_total") is None or felder.get("kwh") is None):
        # second reading at another scale: Tesseract tears a digit apart at
        # one size and reads it cleanly at the next
        try:
            text2 = beleg.text_aus_bild(bild, breite=1600)
            felder2 = beleg.felder_aus_text(text2, bekannte, beschriftung)
            if (felder2.get("cost_total") is not None, felder2.get("kwh") is not None) > \
               (felder.get("cost_total") is not None, felder.get("kwh") is not None):
                text, felder = text2, felder2
        except Exception as fehler:  # noqa: BLE001
            log.warning("Ladebeleg: zweite Lesung fehlgeschlagen – %s", fehler)
    return jsonify({"text": text, "felder": felder})


def _soc_oder_none(wert):
    if wert in ("", None):
        return None
    soc = float(wert)
    return soc if 0 <= soc <= 100 else None


@charges_bp.route("/api/charge/external", methods=["POST"])
@login_required
def charge_external():
    """Create an external charge session (e.g. DC fast charger on the road)."""
    data = request.get_json()
    lang = get_language()
    _t = _translations.get(lang, _translations["DE"])
    if not data:
        return jsonify({"error": "JSON expected"}), 400

    vehicle = str(data.get("vehicle_plate", "")).strip()
    kwh = float(data.get("kwh", 0) or 0)
    if not vehicle or kwh <= 0:
        return jsonify({"error": _t["charge_vehicle_kwh_required"]}), 400

    timestamp = data.get("timestamp", "")
    end_time = data.get("end_time", "") or timestamp
    odometer = float(data["odometer"]) if data.get("odometer") not in ("", None) else None
    cost_total = float(data["cost_total"]) if data.get("cost_total") not in ("", None) else None
    note = str(data.get("note", "")).strip() or None
    location_name = str(data.get("location_name") or "").strip()[:120] or None
    try:
        soc_start = _soc_oder_none(data.get("soc_start"))
        soc_end = _soc_oder_none(data.get("soc_end"))
    except (TypeError, ValueError):
        soc_start = soc_end = None

    # Calculate duration and average power
    duration_minutes = None
    avg_kw = None
    if timestamp and end_time:
        from datetime import datetime
        fmt = "%Y-%m-%d %H:%M"
        try:
            dt_start = datetime.strptime(timestamp[:16], fmt)
            dt_end = datetime.strptime(end_time[:16], fmt)
            duration_minutes = max(0, int((dt_end - dt_start).total_seconds() / 60))
            if duration_minutes > 0:
                avg_kw = round(kwh / (duration_minutes / 60), 2)
        except ValueError:
            log.warning("external charge: duration/avg_kw calc failed (start=%r end=%r) — left unset",
                        timestamp, end_time)

    db = get_db()

    # Derselbe Beleg zweimal eingefügt: gleiche Ladung (Fahrzeug, Start auf
    # zehn Minuten, kWh) legt keine zweite Sitzung an.
    doppelt = db.execute(
        """SELECT id FROM charge_sessions WHERE is_external = 1 AND vehicle_plate = ?
           AND abs(strftime('%s', start_time) - strftime('%s', ?)) < 600
           AND abs(total_kwh - ?) < 0.05""",
        (vehicle, timestamp, round(kwh, 3))).fetchone() if timestamp else None
    if doppelt:
        db.close()
        return jsonify({"ok": True, "duplicate": True, "id": doppelt["id"]})

    # Interpolate odometer from trip if not provided
    if odometer is None and timestamp:
        dev = db.execute(
            "SELECT device FROM vehicles WHERE plate = ?", (vehicle,)
        ).fetchone()
        if dev and dev["device"]:
            trip_row = db.execute(
                """SELECT odo_end FROM trips WHERE device = ?
                   AND odo_end IS NOT NULL
                   AND datetime(end_time) <= datetime(?)
                   ORDER BY end_time DESC LIMIT 1""",
                (dev["device"], timestamp),
            ).fetchone()
            if trip_row:
                odometer = trip_row["odo_end"]

    # SoC the form left empty: what the trips around the charge know (the
    # same values the form offers as prefill, so an API caller or a browser
    # without the hint ends up with the same row).
    if soc_start is None or soc_end is None:
        umfeld = soc_um_ladung(db, vehicle, timestamp, end_time)
        if soc_start is None:
            soc_start = umfeld["soc_start"]
        if soc_end is None:
            soc_end = umfeld["soc_end"]

    db.execute("""
        INSERT INTO charge_sessions
        (session_number, is_external, vehicle_plate, start_time, end_time,
         total_kwh, duration_minutes, avg_kw, odometer, cost_total, note, soc_start, soc_end,
         location_name)
        VALUES (NULL, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (vehicle, timestamp, end_time,
          round(kwh, 3), duration_minutes, avg_kw, odometer, cost_total, note, soc_start, soc_end,
          location_name))

    # Auto-create vehicle if needed
    db.execute("INSERT OR IGNORE INTO vehicles (plate) VALUES (?)", (vehicle,))

    # Recalc distances — scoped to this charge's period (never touches billed ones)
    rebuild_charge_sessions(db, since=timestamp)
    db.close()

    return jsonify({"ok": True, "session_number": "ext"})


# ── Auto-detect external charges from trip SoC ───────────────

def detect_external_from_trips(db):
    """Detect external charges from SoC increases between trips.

    If after a trip the SoC has increased by the next trip and
    no charge session covers this period, an external session
    is created (charge on the road).
    """
    vehicles = db.execute("SELECT plate, device FROM vehicles WHERE device IS NOT NULL AND device != ''").fetchall()
    created = 0

    for vehicle in vehicles:
        plate = vehicle['plate']
        device = vehicle['device']
        bat_kwh = get_bat_kwh(db, device)

        trips = db.execute("""
            SELECT start_time, end_time, soc_start, soc_end,
                   end_lat, end_lon, odo_end
            FROM trips
            WHERE device = ?
              AND soc_start IS NOT NULL AND soc_end IS NOT NULL
              AND end_time IS NOT NULL
              AND datetime(end_time) >= datetime('now', '-7 days')
            ORDER BY start_time
        """, (device,)).fetchall()

        for i in range(len(trips) - 1):
            t_before = trips[i]
            t_after  = trips[i + 1]

            soc_before = t_before['soc_end']
            soc_after  = t_after['soc_start']

            # Same threshold as the detector, the charge cycles and "between
            # charges" (was 3 % here: overnight BMS drift made phantom charges)
            if soc_after - soc_before < config.SOC_JUMP_MIN:
                continue

            window_start = t_before['end_time']
            window_end   = t_after['start_time']

            # Already a session in this time window? (datetime() normalizes T/Z formats)
            existing = db.execute("""
                SELECT id FROM charge_sessions
                WHERE vehicle_plate = ?
                  AND datetime(start_time) < datetime(?)
                  AND (end_time IS NULL OR datetime(end_time) > datetime(?))
            """, (plate, window_end, window_start)).fetchone()
            if existing:
                continue

            soc_diff = soc_after - soc_before
            # Time-appropriate calibrated capacity (not the fixed anchor) so the
            # estimated kWh tracks degradation and isn't systematically too high.
            cap = get_bat_kwh(db, device, at_time=window_start) or bat_kwh
            kwh = round(soc_diff / 100.0 * cap, 3)

            lat = t_before['end_lat']
            lon = t_before['end_lon']
            loc = match_charge_location(db, lat, lon) or match_location(db, lat, lon)
            loc_name = loc['name'] if loc else None
            odometer = t_before['odo_end']

            duration_min = None
            avg_kw = None
            try:
                t0 = datetime.fromisoformat(window_start)
                t1 = datetime.fromisoformat(window_end)
                duration_min = max(1, int((t1 - t0).total_seconds() / 60))
                avg_kw = round(kwh / (duration_min / 60.0), 2) if duration_min > 0 else None
            except Exception:
                log.debug("auto-charge detect: duration/avg_kw calc failed (%r..%r) — left unset",
                          window_start, window_end, exc_info=True)

            lang = get_language()
            _t = _translations.get(lang, _translations["DE"])
            note = _t["charge_auto_detected_note"].format(f"{soc_before:.0f}", f"{soc_after:.0f}")

            # The SoC pair is what the detection is built on — it belongs in
            # the row, not only in the note, so the list and the detail show
            # "62 -> 80 %" like for any wallbox session.
            db.execute("""
                INSERT INTO charge_sessions
                (session_number, is_external, vehicle_plate, start_time, end_time,
                 total_kwh, duration_minutes, avg_kw, odometer, lat, lon, location_name, note,
                 soc_start, soc_end)
                VALUES (NULL, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (plate, window_start, window_end, kwh, duration_min, avg_kw,
                  odometer, lat, lon, loc_name, note, soc_before, soc_after))
            created += 1

    if created:
        db.commit()

    # Clean up duplicates: external sessions with overlapping time windows (same plate)
    # → delete the newer one (higher ID)
    dupes = db.execute("""
        SELECT a.id FROM charge_sessions a
        JOIN charge_sessions b ON a.vehicle_plate = b.vehicle_plate
          AND a.id > b.id
          AND a.is_external = 1 AND b.is_external = 1
          AND datetime(a.start_time) < datetime(b.end_time)
          AND (a.end_time IS NULL OR datetime(a.end_time) > datetime(b.start_time))
    """).fetchall()
    if dupes:
        ids = [r['id'] for r in dupes]
        db.execute(f"DELETE FROM charge_sessions WHERE id IN ({','.join('?'*len(ids))})", ids)
        db.commit()
        log.info("detect_external: %d duplicates removed", len(ids))

    return created


@charges_bp.route("/api/charge/detect-external", methods=["POST"])
@login_required
def api_detect_external():
    """Detect external charges from SoC changes between trips."""
    db = get_db()
    count = detect_external_from_trips(db)
    if count:
        rebuild_charge_sessions(db)
    db.close()
    return jsonify({"ok": True, "created": count})
