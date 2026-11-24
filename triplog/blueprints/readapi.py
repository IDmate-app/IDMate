"""Read API for display clients (e.g. the Android Auto app), /api/v1.

Opt-in, off by default: until an admin switches it on (Admin -> Interfaces
-> Read API) every /api/v1 route answers 404, as if it did not exist.

One access per vehicle. Creating it shows a QR code once; the client scans it
and has base URL, token and vehicle. Only the SHA-256 of the token is stored,
so a lost code cannot be shown again - create a new one, the old one stops
working at once.

Answers are telegrams, the same envelope for every source a client may have:
    {"v": 1, "src": "backend", "ts": "<ISO 8601 UTC>", "seq": n,
     "type": "status", "data": {...}}
A field the server has no value for is left out, never sent as null or 0.
The read API never talks to InfluxDB or Traccar on behalf of the client: it
serves what the dashboard already has (state cache) and the trip and charge
tables. It writes in one place only, and only where the admin allowed it for
the vehicle: purpose, partner, occasion and note of a trip in the logbook
(L-012, POST /trips/<id>). The server's own logbook stays as it is.
"""

import base64
import calendar
import hashlib
import hmac
import io
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from flask import Blueprint, abort, jsonify, redirect, request, session, url_for
from flask_login import login_user

import bms_learn as bl
import detector
import fahrzeugmodell as fm
from app import (DisplayUser, _get_charge_efficiency, _is_rate_limited, _record_attempt,
                 _vehicle_state, admin_required, csrf, get_bat_kwh, get_db, get_preset_values,
                 get_purpose_meta, log, qrcode)
from blueprints import bms

API_VERSION = 1
# Poll interval a client should use, seconds (drive / parked).
INTERVAL_DRIVE_S = 10
INTERVAL_PARK_S = 60
LIMIT_DEFAULT = 10
LIMIT_MAX = 100
# What IDmate contributes to a display device: the vehicle data. Navigation,
# music and messenger are the app's own and are neither delivered nor
# switched from here.
MODULES = ("dashboard", "logbook")
# Logbook (L-012): the fields a display app may change, when allowed, and how
# long each may be.
LOGBOOK_FIELDS = {"purpose": 100, "destination": 200, "visit_reason": 200, "note": 1000}
# Energy model (/energy-model): speeds of the consumption table, and the
# windows of "what the car really used lately".
MODEL_SPEEDS = range(30, 161, 10)
RECENT_H = 6
AVERAGE_DAYS = 28

# telegram field -> key of the dashboard state (app._vehicle_state).
# range: IDmate's own figure, the one on the dashboard (energy left ÷ the
# consumption of the last 1000 km). Not the range the stick could read from
# the car: no stick delivers one today, and the value stored from an early
# firmware was the capacity (86.4), not a range.
STATUS_FIELDS = (
    ("soc", "soc"),
    ("range", "calc_range"),
    ("power", "power"),
    ("speed", "speed"),
    ("lat", "lat"),
    ("lon", "lon"),
    ("odometer", "odometer"),
    ("charging", "charging"),
    ("dc", "dc_charging"),
    ("t_ext", "ext_temp"),
    ("t_batt", "bat_temp"),
)
BOOL_FIELDS = {"charging", "dc"}
# charging and dc are bits a telegram carries only while they are true, and
# the state keeps the last one for ever (a car parked for days still read
# "charging" from its last charging stop): true only when the bit came with
# the newest values.
FLAG_FRESH_S = 120
LAST_USED_EVERY_S = 60         # last_used_at: not a database write per poll

# Public, token-authenticated routes. Exempt from the login gate as a whole
# (app.require_login checks the blueprint name), so nothing else belongs here.
readapi_bp = Blueprint("readapi", __name__, url_prefix="/api/v1")
# Admin routes: normal session login + admin_required.
readapi_admin_bp = Blueprint("readapi_admin", __name__)

_seq = {}
_seq_lock = threading.Lock()


# ── Settings ──────────────────────────────────────────────────────────────────

def _setting(db, key):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row and row[0] else ""


def _set_setting(db, key, value):
    if value:
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = ?", (key, value, value))
    else:
        db.execute("DELETE FROM settings WHERE key = ?", (key,))


def _enabled(db):
    return _setting(db, "read_api_enabled") == "1"


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


# ── Authentication ────────────────────────────────────────────────────────────

def _client_ip():
    return request.remote_addr or "?"


def _vehicle_for_request(db):
    """The vehicle the bearer token belongs to. 404 while the API is off,
    429 after too many wrong tokens, 401 without a valid one."""
    if not _enabled(db):
        db.close()
        abort(404)
    ip = _client_ip()
    if _is_rate_limited(ip):
        db.close()
        abort(429)
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else ""
    row = None
    if token:
        row = db.execute(
            "SELECT k.id AS key_id, k.token_hash, k.last_used_at, k.logbook_edit, v.id, v.plate, v.name, v.model, v.device "
            "FROM read_api_keys k JOIN vehicles v ON v.id = k.vehicle_id "
            "WHERE k.token_hash = ?", (_hash(token),)).fetchone()
    if row is None or not hmac.compare_digest(row["token_hash"], _hash(token)):
        _record_attempt(ip)
        db.close()
        abort(401)
    now = _now_iso()
    if _age_s(row["last_used_at"], now) is None or _age_s(row["last_used_at"], now) >= LAST_USED_EVERY_S:
        db.execute("UPDATE read_api_keys SET last_used_at = ? WHERE id = ?", (now, row["key_id"]))
        db.commit()
    return row


@readapi_bp.errorhandler(401)
def _unauthorized(_e):
    return jsonify({"error": "unauthorized"}), 401, {"WWW-Authenticate": "Bearer"}


@readapi_bp.errorhandler(404)
def _not_found(_e):
    return jsonify({"error": "not found"}), 404


@readapi_bp.errorhandler(429)
def _too_many(_e):
    return jsonify({"error": "too many attempts"}), 429


# ── Telegrams ─────────────────────────────────────────────────────────────────

def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_s(then_iso, now_iso):
    """Seconds from one "…Z" time to another, None when either is missing."""
    try:
        a = datetime.strptime(then_iso, "%Y-%m-%dT%H:%M:%SZ")
        b = datetime.strptime(now_iso, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    return (b - a).total_seconds()


def _utc(local_iso):
    """Trip and charge times are stored as local time without offset; the API
    speaks UTC. Values that already carry an offset are converted as given."""
    if not local_iso:
        return None
    try:
        dt = datetime.fromisoformat(str(local_iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=detector.LOCAL_TZ)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _telegram(device, kind, data, ts=None):
    with _seq_lock:
        n = _seq.get(device, 0) + 1
        _seq[device] = n
    return {"v": API_VERSION, "src": "backend", "ts": ts or _now_iso(), "seq": n,
            "type": kind, "data": data}


def _state(device):
    """What the dashboard shows for this vehicle: the live values and the
    figures IDmate works out itself (range)."""
    return _vehicle_state(device) if device else {}


def _status_data(state):
    data = {}
    times = state.get("field_times") or {}
    for field, key in STATUS_FIELDS:
        val = state.get(key)
        if val is None:
            continue
        if field in BOOL_FIELDS:
            age = _age_s(times.get(key), state.get("time"))
            val = bool(val) and age is not None and age <= FLAG_FRESH_S
        data[field] = val
    return data


def _limit():
    try:
        n = int(request.args.get("limit", LIMIT_DEFAULT))
    except ValueError:
        n = LIMIT_DEFAULT
    return max(1, min(LIMIT_MAX, n))


def _num(row, key, digits):
    val = row[key]
    return None if val is None else round(val, digits)


def _clean(d):
    return {k: v for k, v in d.items() if v is not None and v != ""}


# ── Routes ────────────────────────────────────────────────────────────────────

@readapi_bp.route("/health")
def health():
    """Reachability, without a token. Tells nothing but 'on' (404 while off)."""
    db = get_db()
    on = _enabled(db)
    db.close()
    if not on:
        abort(404)
    return jsonify({"ok": True, "api": API_VERSION})


@readapi_bp.route("/capabilities")
def capabilities():
    """What this server can deliver for the vehicle right now: every field it
    has a value for, with the interval in seconds at which it changes."""
    db = get_db()
    v = _vehicle_for_request(db)
    db.close()
    fields = _status_data(_state(v["device"]))
    return jsonify(_telegram(v["device"], "caps", {
        "status": {f: INTERVAL_DRIVE_S for f in fields},
        "trip": {"list": 0},
        "charge": {"list": 0},
        "logbook": {"list": 0, "edit": bool(v["logbook_edit"])},
    }))


@readapi_bp.route("/config")
def config_():
    db = get_db()
    v = _vehicle_for_request(db)
    db.close()
    return jsonify({
        "v": API_VERSION,
        "vehicle": _clean({"id": v["device"], "name": v["name"] or v["plate"],
                           "model": v["model"]}),
        "modules": list(MODULES),
        "interval": {"drive": INTERVAL_DRIVE_S, "park": INTERVAL_PARK_S},
        "logbook": {"edit": bool(v["logbook_edit"])},
    })


@readapi_bp.route("/status")
def status():
    db = get_db()
    v = _vehicle_for_request(db)
    db.close()
    state = _state(v["device"])
    return jsonify(_telegram(v["device"], "status", _status_data(state), state.get("time")))


@readapi_bp.route("/trips")
def trips():
    db = get_db()
    v = _vehicle_for_request(db)
    rows = db.execute(
        "SELECT id, start_time, end_time, start_address, end_address, distance_km, "
        "soc_start, soc_end, energy_kwh, consumption FROM trips "
        "WHERE device = ? ORDER BY start_time DESC LIMIT ?", (v["device"], _limit())).fetchall()
    db.close()
    items = [_clean({
        "id": r["id"], "start": _utc(r["start_time"]), "end": _utc(r["end_time"]),
        "from": r["start_address"], "to": r["end_address"],
        "distance": _num(r, "distance_km", 1),
        "soc_start": _num(r, "soc_start", 1), "soc_end": _num(r, "soc_end", 1),
        "energy": _num(r, "energy_kwh", 2), "consumption": _num(r, "consumption", 1),
    }) for r in rows]
    return jsonify(_telegram(v["device"], "trip", {"items": items}))


# ── Logbook (L-012) ───────────────────────────────────────────────────────────

def _month():
    """year/month from the query, else the current month."""
    now = datetime.now()
    try:
        year = int(request.args.get("year", now.year))
        month = int(request.args.get("month", now.month))
    except ValueError:
        abort(400)
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        abort(400)
    return year, month


def _round(v, digits=1):
    return None if v is None else round(v, digits)


def _trip_item(r, private):
    """One trip with its logbook fields, for the app to show and edit."""
    purpose = r["purpose"] or ""
    return _clean({
        "id": r["id"], "start": _utc(r["start_time"]), "end": _utc(r["end_time"]),
        "from": r["start_address"], "to": r["end_address"],
        "odo_start": _round(r["odo_start"]), "odo_end": _round(r["odo_end"]),
        "distance": _round(r["distance_km"]),
        "soc_start": _round(r["soc_start"]), "soc_end": _round(r["soc_end"]),
        "purpose": purpose, "destination": r["destination"], "visit_reason": r["visit_reason"],
        "note": r["note"], "private": purpose in private if purpose else None,
    })


def _logbook_row(r):
    """A row of the server's monthly logbook (fahrtenbuch.monat_daten), with
    English keys: kind trip (business, with purpose) / open (no purpose yet) /
    private (private trips in a row as one line) / gap (odometer jump)."""
    kind = {"fahrt": "trip", "offen": "open", "privat": "private", "luecke": "gap"}[r["art"]]
    row = {"kind": kind, "odo_start": _round(r.get("odo_start")), "odo_end": _round(r.get("odo_end")),
           "distance": _round(r.get("km"))}
    if kind != "gap":
        row.update({"start": _utc(r.get("von")), "end": _utc(r.get("bis")), "trips": r.get("ids", [])})
    if kind == "private":
        row["count"] = r.get("anzahl")
    if kind in ("trip", "open"):
        row.update({"from": r.get("start"), "to": r.get("ziel"), "purpose": r.get("zweck"),
                    "destination": r.get("partner"), "visit_reason": r.get("anlass"), "note": r.get("notiz")})
    return _clean(row)


@readapi_bp.route("/logbook")
def logbook():
    """The month of the server's logbook (/trips/logbook) as the app shows it:
    its rows and totals, every trip of the month with its logbook fields, the
    months that have trips, and whether the app may edit."""
    import fahrtenbuch
    year, month = _month()
    db = get_db()
    v = _vehicle_for_request(db)
    device = v["device"]
    data = fahrtenbuch.monat_daten(db, device, year, month)
    private = {r[0] for r in db.execute("SELECT name FROM purpose_meta WHERE is_private = 1")}
    last = calendar.monthrange(year, month)[1]
    trips_ = db.execute(
        "SELECT * FROM trips WHERE device = ? AND start_time >= ? AND start_time <= ? ORDER BY start_time",
        (device, f"{year:04d}-{month:02d}-01T00:00:00", f"{year:04d}-{month:02d}-{last:02d}T23:59:59")).fetchall()
    months = [r[0] for r in db.execute(
        "SELECT DISTINCT substr(start_time, 1, 7) FROM trips WHERE device = ? "
        "AND start_time GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]*' ORDER BY 1 DESC", (device,))]
    db.close()
    s = data["summen"]
    totals = _clean({
        "odo_start": _round(s.get("odo_anfang")), "odo_end": _round(s.get("odo_ende")),
        "trips": s.get("fahrten"), "total": _round(s.get("gesamt")), "business": _round(s.get("dienstlich")),
        "private": _round(s.get("privat")), "open": _round(s.get("offen")), "gaps": _round(s.get("luecken")),
        "purposes": {k: _round(km) for k, km in sorted(s.get("zwecke", {}).items())},
    })
    return jsonify(_telegram(device, "logbook", {
        "year": year, "month": month, "edit": bool(v["logbook_edit"]), "months": months,
        "rows": [_logbook_row(r) for r in data["rows"]], "totals": totals,
        "items": [_trip_item(r, private) for r in trips_],
    }))


@readapi_bp.route("/logbook/options")
def logbook_options():
    """What the app offers to pick from: the vehicle's purposes (with private
    ones marked), partners and occasions - the same lists as the trip log."""
    db = get_db()
    v = _vehicle_for_request(db)
    device = v["device"]
    purposes = [{"name": p["name"], "private": bool(p["is_private"])}
                for p in get_purpose_meta(db, device=device)]
    out = {"edit": bool(v["logbook_edit"]), "purposes": purposes,
           "destinations": get_preset_values(db, "destination", device=device),
           "visit_reasons": get_preset_values(db, "visit_reason", device=device)}
    db.close()
    return jsonify(_telegram(device, "logbook_options", out))


@readapi_bp.route("/trips/<int:trip_id>", methods=["POST"])
@csrf.exempt
def logbook_edit(trip_id):
    """Purpose, partner (destination), occasion (visit_reason) and note of one
    trip of the token's vehicle - only where the admin allowed the app to edit
    the logbook. Body: any of those fields; "" clears one. Answers the trip as
    the logbook serves it."""
    db = get_db()
    v = _vehicle_for_request(db)
    if not v["logbook_edit"]:
        db.close()
        return jsonify({"error": "logbook editing not allowed for this access"}), 403
    trip = db.execute("SELECT id FROM trips WHERE id = ? AND device = ?", (trip_id, v["device"])).fetchone()
    if trip is None:
        db.close()
        abort(404)
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        db.close()
        return jsonify({"error": "JSON object expected"}), 400
    sets, params = [], []
    for field, most in LOGBOOK_FIELDS.items():
        if field not in body:
            continue
        val = body[field]
        if val is not None and not isinstance(val, str):
            db.close()
            return jsonify({"error": f"{field}: text expected"}), 400
        val = (val or "").strip()
        if len(val) > most:
            db.close()
            return jsonify({"error": f"{field}: at most {most} characters"}), 400
        sets.append(f"{field} = ?")
        params.append(val or None)
    if not sets:
        db.close()
        return jsonify({"error": "no fields"}), 400
    db.execute(f"UPDATE trips SET {', '.join(sets)} WHERE id = ?", params + [trip_id])
    db.commit()
    log.info("Read API: logbook of trip %s changed from the app (%s)", trip_id, v["device"])
    private = {r[0] for r in db.execute("SELECT name FROM purpose_meta WHERE is_private = 1")}
    row = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    db.close()
    return jsonify(_telegram(v["device"], "trip", _trip_item(row, private)))


@readapi_bp.route("/charging")
def charging():
    """The vehicle's charges as the charge overview shows them: the charge
    book (charge_sessions — wallbox meter, receipts, imports, detected
    external charges). Not the detector's raw table (charges): that one knows
    no wallbox meter and no receipt, splits a charge at every pause and is
    shown nowhere in IDmate, so a client read other charges and other
    energies than the pages do.
    energy is the grid side (metered or billed, what the overview lists),
    energy_battery what reached the battery (energy book)."""
    db = get_db()
    v = _vehicle_for_request(db)
    rows = db.execute(
        "SELECT id, session_number, is_external, start_time, end_time, total_kwh, avg_kw, "
        "soc_start, soc_end, location_name, cost_total, note FROM charge_sessions "
        "WHERE vehicle_plate = ? ORDER BY datetime(start_time) DESC LIMIT ?",
        (v["plate"], _limit())).fetchall()
    eff_ac = _get_charge_efficiency(db, v["plate"])
    eff_dc = _get_charge_efficiency(db, v["plate"], dc=True)
    db.close()
    items = []
    for r in rows:
        s = dict(r)
        e = fm.session_energy(s, eff_ac, eff_dc) or {}
        # A wallbox charge is AC; an external one counts as DC by its power,
        # and without a power nobody knows.
        dc = False if not s["is_external"] else (fm.is_dc(s) if s["avg_kw"] is not None else None)
        items.append(_clean({
            "id": s["id"], "start": _utc(s["start_time"]), "end": _utc(s["end_time"]),
            "at": s["location_name"], "dc": dc,
            "soc_start": _num(r, "soc_start", 1), "soc_end": _num(r, "soc_end", 1),
            "energy": round(e["grid"], 2) if e.get("grid") else None,
            "energy_battery": round(e["batt"], 2) if e.get("batt") else None,
            "power_avg": _num(r, "avg_kw", 1),
        }))
    return jsonify(_telegram(v["device"], "charge", {"items": items}))


def _driven(trips, since):
    """What the trips ended since then add up to: km, kWh/100 km and the
    average speed over the time driven (standing between trips is not in
    it). None below a distance that says something."""
    rows = []
    for t in trips:
        t0, t1 = bms._utc(t["start"]), bms._utc(t["end"])
        if t0 and t1 and t1 >= since and t1 > t0:
            rows.append((t["km"], t["kwh"], (t1 - t0).total_seconds() / 3600))
    km, kwh, hours = (sum(r[i] for r in rows) for i in range(3))
    if km < bl.DRIVE_MIN_KM or not hours:
        return None
    return {"distance": round(km, 1), "consumption": round(kwh / km * 100, 1), "speed": round(km / hours)}


def _energy_model(db, v):
    """The vehicle's own consumption model, the one the range on the
    dashboard and the range area on the map are computed with (bms_learn):
    kWh/100 km by speed at the outside temperature of now, fitted on the
    trips of the last year. Speed means the average actually driven over a
    stretch — the model learns from trip averages."""
    device = v["device"]
    now = datetime.now(timezone.utc)
    trips = bms._trips(db, device)
    model = bms._model(device, trips)
    out = {"capacity": bms._r1(get_bat_kwh(db, device, datetime.now().isoformat()))}
    for key, window, span in (("recent", timedelta(hours=RECENT_H), {"hours": RECENT_H}),
                              ("average", timedelta(days=AVERAGE_DAYS), {"days": AVERAGE_DAYS})):
        d = _driven(trips, now - window)
        if d:
            out[key] = {**span, **d}
    if not model:                    # too few trips yet (bms_learn.FIT_MIN_*)
        return _clean(out)
    live = bms._live(device)
    parked_h = bl.trip_context(bms._series(device), now.timestamp())["parked_h"]
    temp, _src = bms._outside_temp(db, device, live, parked_h)
    out["t_ext"] = bms._r1(temp)
    out["consumption"] = [{"v": s, "kwh100": round(bl.consumption(model, s, temp), 1)} for s in MODEL_SPEEDS]
    if model.get("e_start"):
        out["start"] = round(model["e_start"], 2)
    if model.get("rain"):            # extra kWh/100 km per rain grade (rain_radar), as learned from the trips
        out["rain"] = [{"level": int(k), "kwh100": v, "trips": (model.get("rain_n") or {}).get(k)}
                       for k, v in sorted(model["rain"].items())]
    fast = [t["v"] for t in trips if t.get("v") and t["km"] >= bl.FRESH_TRIP_KM]
    out["basis"] = _clean({"trips": model["n"], "distance": round(model["km"]),
                           "speed_max": round(max(fast)) if fast else None})
    return _clean(out)


@readapi_bp.route("/energy-model")
def energy_model():
    db = get_db()
    v = _vehicle_for_request(db)
    try:
        data = _energy_model(db, v)
    finally:
        db.close()
    return jsonify(_telegram(v["device"], "energy_model", data))


@readapi_bp.route("/rain")
def rain():
    """Rain raster around the vehicle's range (L-005, E-002): the same data as
    the rain layer on the map — DWD and RainViewer folded into one raster of
    grades. Empty frames while it is off, has no area yet or is still working."""
    import range_area
    import rain_radar
    db = get_db()
    v = _vehicle_for_request(db)
    try:
        rec = range_area.load_last(db, v["device"]) if rain_radar.enabled() else None
    finally:
        db.close()
    res = rain_radar.area(rec["lat"], rec["lon"], max(rec["kms"])) if rec else None
    return jsonify(_telegram(v["device"], "rain", rain_radar.public(res)))


@readapi_bp.route("/display")
def display():
    """The dashboard map for a display device: the original page, map only.

    The token opens a display session (cookie) and the browser view is sent to
    the dashboard, which then shows nothing but its map with the range display.
    That session can only GET the dashboard and what its map reads, for the
    vehicle of the token (app.DISPLAY_ENDPOINTS); it ends when the access is
    deleted or the read API is switched off."""
    db = get_db()
    v = _vehicle_for_request(db)
    db.close()
    session.clear()
    login_user(DisplayUser(v["id"]), remember=False)
    return redirect(url_for("dashboard"))


# ── Admin ─────────────────────────────────────────────────────────────────────

def _own_url():
    """The address this admin page is open at. Behind a reverse proxy that
    does not pass the protocol on (X-Forwarded-Proto), the server sees http
    for an https page; the browser knows better, so its Origin / Referer
    decides when it names this very host."""
    for header in ("Origin", "Referer"):
        u = urlsplit(request.headers.get(header) or "")
        if u.scheme in ("http", "https") and u.netloc == request.host:
            return f"{u.scheme}://{u.netloc}"
    return f"{request.scheme}://{request.host}"


def _saved_url(db):
    """The address somebody entered, "" for none. Up to 1.2.339 the page sent
    the address it showed along with the on/off switch, so the server's own
    guess got stored as if entered — behind such a proxy "http://<host>" for a
    page that is https. That value is nobody's entry: http for this very host
    while the browser is here over https counts as not entered."""
    saved = _setting(db, "read_api_url") or ""
    u = urlsplit(saved)
    if u.scheme == "http" and u.netloc == request.host and _own_url().startswith("https://"):
        return ""
    return saved


def _base_url(db):
    """The address that goes into the QR code: the one entered, else the one
    this admin page is open at."""
    return _saved_url(db) or _own_url()


def _qr_png_b64(text):
    if qrcode is None:
        return ""
    buf = io.BytesIO()
    qrcode.make(text).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


@readapi_admin_bp.route("/api/admin/read-api")
@admin_required
def admin_get():
    db = get_db()
    rows = db.execute(
        "SELECT v.id, v.plate, v.name, v.device, k.created_at, k.last_used_at, "
        "k.id AS key_id, k.logbook_edit FROM vehicles v LEFT JOIN read_api_keys k ON k.vehicle_id = v.id "
        "WHERE v.device IS NOT NULL AND v.device != '' ORDER BY v.plate").fetchall()
    out = {
        "enabled": _enabled(db),
        "url": _base_url(db),
        "url_saved": _saved_url(db),
        "vehicles": [{
            "id": r["id"], "name": r["name"] or r["plate"], "plate": r["plate"],
            "device": r["device"], "has_key": r["key_id"] is not None,
            "created_at": r["created_at"], "last_used_at": r["last_used_at"],
            "logbook_edit": bool(r["logbook_edit"]),
        } for r in rows],
    }
    db.close()
    return jsonify(out)


@readapi_admin_bp.route("/api/admin/read-api", methods=["POST"])
@admin_required
def admin_save():
    """Body: {"enabled": bool, "url": "https://host"} - both optional."""
    data = request.get_json(force=True, silent=True) or {}
    db = get_db()
    if "enabled" in data:
        _set_setting(db, "read_api_enabled", "1" if data.get("enabled") else "")
        log.info("Read API %s", "enabled" if data.get("enabled") else "disabled")
    if "url" in data:
        _set_setting(db, "read_api_url", str(data.get("url") or "").strip().rstrip("/"))
    db.commit()
    out = {"ok": True, "enabled": _enabled(db), "url": _base_url(db), "url_saved": _saved_url(db)}
    db.close()
    return jsonify(out)


@readapi_admin_bp.route("/api/admin/read-api/<int:vehicle_id>/key", methods=["POST"])
@admin_required
def admin_key_create(vehicle_id):
    """Create (or replace) the access of one vehicle. The token and its QR code
    are in this answer only."""
    db = get_db()
    v = db.execute("SELECT id, plate, name, device FROM vehicles WHERE id = ?",
                   (vehicle_id,)).fetchone()
    if v is None or not v["device"]:
        db.close()
        return jsonify({"error": "vehicle not found"}), 404
    token = secrets.token_urlsafe(32)
    # a new code keeps whether the app may edit the logbook
    old = db.execute("SELECT logbook_edit FROM read_api_keys WHERE vehicle_id = ?", (vehicle_id,)).fetchone()
    db.execute("DELETE FROM read_api_keys WHERE vehicle_id = ?", (vehicle_id,))
    db.execute("INSERT INTO read_api_keys (vehicle_id, token_hash, created_at, logbook_edit) "
               "VALUES (?, ?, ?, ?)", (vehicle_id, _hash(token), _now_iso(), old["logbook_edit"] if old else 0))
    db.commit()
    url = _base_url(db) + "/api/v1"
    db.close()
    payload = json.dumps({"v": API_VERSION, "t": "idmate-read", "url": url, "token": token,
                          "vehicle": v["device"], "name": v["name"] or v["plate"]},
                         separators=(",", ":"), ensure_ascii=False)
    log.info("Read API: access created for vehicle %s", v["device"])
    return jsonify({"ok": True, "url": url, "token": token, "qr": _qr_png_b64(payload)})


@readapi_admin_bp.route("/api/admin/read-api/<int:vehicle_id>/key", methods=["DELETE"])
@admin_required
def admin_key_delete(vehicle_id):
    db = get_db()
    db.execute("DELETE FROM read_api_keys WHERE vehicle_id = ?", (vehicle_id,))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@readapi_admin_bp.route("/api/admin/read-api/<int:vehicle_id>/logbook-edit", methods=["POST"])
@admin_required
def admin_logbook_edit(vehicle_id):
    """Body: {"on": bool} - whether the app with this vehicle's access may edit
    the logbook (L-012). Needs an access."""
    data = request.get_json(force=True, silent=True) or {}
    on = 1 if data.get("on") else 0
    db = get_db()
    n = db.execute("UPDATE read_api_keys SET logbook_edit = ? WHERE vehicle_id = ?", (on, vehicle_id)).rowcount
    db.commit()
    db.close()
    if not n:
        return jsonify({"error": "no access for this vehicle"}), 404
    log.info("Read API: logbook editing %s for vehicle %s", "allowed" if on else "off", vehicle_id)
    return jsonify({"ok": True, "logbook_edit": bool(on)})
