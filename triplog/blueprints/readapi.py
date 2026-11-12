"""Read API for display clients (e.g. the Android Auto app), /api/v1.

Opt-in, off by default: until an admin switches it on (Admin -> Home Assistant
-> Read API) every /api/v1 route answers 404, as if it did not exist.

One access per vehicle. Creating it shows a QR code once; the client scans it
and has base URL, token and vehicle. Only the SHA-256 of the token is stored,
so a lost code cannot be shown again - create a new one, the old one stops
working at once.

Answers are telegrams, the same envelope for every source a client may have:
    {"v": 1, "src": "backend", "ts": "<ISO 8601 UTC>", "seq": n,
     "type": "status", "data": {...}}
A field the server has no value for is left out, never sent as null or 0.
The read API never writes and never talks to InfluxDB or Traccar on behalf of
the client: it serves what the dashboard already has (state cache) and the
trip and charge tables.
"""

import base64
import hashlib
import hmac
import io
import json
import secrets
import threading
from datetime import datetime, timezone

from flask import Blueprint, abort, jsonify, redirect, request, session, url_for
from flask_login import login_user

import detector
from app import (DisplayUser, _is_rate_limited, _record_attempt, _vehicle_state, admin_required,
                 get_db, log, qrcode)

API_VERSION = 1
# Poll interval a client should use, seconds (drive / parked).
INTERVAL_DRIVE_S = 10
INTERVAL_PARK_S = 60
LIMIT_DEFAULT = 10
LIMIT_MAX = 100
MODULES = ("dashboard", "navi", "music", "messenger")
MODULES_DEFAULT = ("dashboard",)

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


def _modules(raw):
    try:
        val = json.loads(raw) if raw else list(MODULES_DEFAULT)
    except ValueError:
        val = list(MODULES_DEFAULT)
    return [m for m in MODULES if m in val]


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
            "SELECT k.id AS key_id, k.token_hash, k.modules, k.last_used_at, v.id, v.plate, v.name, v.model, v.device "
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
        "modules": _modules(v["modules"]),
        "interval": {"drive": INTERVAL_DRIVE_S, "park": INTERVAL_PARK_S},
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


@readapi_bp.route("/charging")
def charging():
    db = get_db()
    v = _vehicle_for_request(db)
    rows = db.execute(
        "SELECT id, start_time, end_time, address, type, soc_start, soc_end, energy_kwh, "
        "max_power_kw FROM charges WHERE device = ? ORDER BY start_time DESC LIMIT ?",
        (v["device"], _limit())).fetchall()
    db.close()
    items = [_clean({
        "id": r["id"], "start": _utc(r["start_time"]), "end": _utc(r["end_time"]),
        "at": r["address"], "dc": (r["type"] == "dc") if r["type"] else None,
        "soc_start": _num(r, "soc_start", 1), "soc_end": _num(r, "soc_end", 1),
        "energy": _num(r, "energy_kwh", 2), "power_max": _num(r, "max_power_kw", 1),
    }) for r in rows]
    return jsonify(_telegram(v["device"], "charge", {"items": items}))


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

def _base_url(db):
    return _setting(db, "read_api_url") or f"{request.scheme}://{request.host}"


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
        "SELECT v.id, v.plate, v.name, v.device, k.created_at, k.last_used_at, k.modules, "
        "k.id AS key_id FROM vehicles v LEFT JOIN read_api_keys k ON k.vehicle_id = v.id "
        "WHERE v.device IS NOT NULL AND v.device != '' ORDER BY v.plate").fetchall()
    out = {
        "enabled": _enabled(db),
        "url": _base_url(db),
        "modules": list(MODULES),
        "vehicles": [{
            "id": r["id"], "name": r["name"] or r["plate"], "plate": r["plate"],
            "device": r["device"], "has_key": r["key_id"] is not None,
            "created_at": r["created_at"], "last_used_at": r["last_used_at"],
            "modules": _modules(r["modules"]),
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
    out = {"ok": True, "enabled": _enabled(db), "url": _base_url(db)}
    db.close()
    return jsonify(out)


@readapi_admin_bp.route("/api/admin/read-api/<int:vehicle_id>/key", methods=["POST"])
@admin_required
def admin_key_create(vehicle_id):
    """Create (or replace) the access of one vehicle. The token and its QR code
    are in this answer only."""
    data = request.get_json(force=True, silent=True) or {}
    db = get_db()
    v = db.execute("SELECT id, plate, name, device FROM vehicles WHERE id = ?",
                   (vehicle_id,)).fetchone()
    if v is None or not v["device"]:
        db.close()
        return jsonify({"error": "vehicle not found"}), 404
    old = db.execute("SELECT modules FROM read_api_keys WHERE vehicle_id = ?",
                     (vehicle_id,)).fetchone()
    wanted = data.get("modules")
    modules = _modules(json.dumps(wanted)) if isinstance(wanted, list) \
        else _modules(old["modules"] if old else "")
    token = secrets.token_urlsafe(32)
    db.execute("DELETE FROM read_api_keys WHERE vehicle_id = ?", (vehicle_id,))
    db.execute("INSERT INTO read_api_keys (vehicle_id, token_hash, modules, created_at) "
               "VALUES (?, ?, ?, ?)", (vehicle_id, _hash(token), json.dumps(modules), _now_iso()))
    db.commit()
    url = _base_url(db) + "/api/v1"
    db.close()
    payload = json.dumps({"v": API_VERSION, "t": "idmate-read", "url": url, "token": token,
                          "vehicle": v["device"], "name": v["name"] or v["plate"]},
                         separators=(",", ":"), ensure_ascii=False)
    log.info("Read API: access created for vehicle %s", v["device"])
    return jsonify({"ok": True, "url": url, "token": token, "modules": modules,
                    "qr": _qr_png_b64(payload)})


@readapi_admin_bp.route("/api/admin/read-api/<int:vehicle_id>/modules", methods=["POST"])
@admin_required
def admin_modules(vehicle_id):
    data = request.get_json(force=True, silent=True) or {}
    wanted = data.get("modules")
    if not isinstance(wanted, list):
        return jsonify({"error": "modules must be a list"}), 400
    modules = _modules(json.dumps(wanted))
    db = get_db()
    cur = db.execute("UPDATE read_api_keys SET modules = ? WHERE vehicle_id = ?",
                     (json.dumps(modules), vehicle_id))
    db.commit()
    db.close()
    if not cur.rowcount:
        return jsonify({"error": "no access for this vehicle"}), 404
    return jsonify({"ok": True, "modules": modules})


@readapi_admin_bp.route("/api/admin/read-api/<int:vehicle_id>/key", methods=["DELETE"])
@admin_required
def admin_key_delete(vehicle_id):
    db = get_db()
    db.execute("DELETE FROM read_api_keys WHERE vehicle_id = ?", (vehicle_id,))
    db.commit()
    db.close()
    return jsonify({"ok": True})
