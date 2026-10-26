"""Live journey for guests.

The owner creates a guest access per person in the journey detail page
("Live teilen"): a link (``/s/<key>``, 16 hex chars) and a four-digit code.
The guest enters the code once (with a name); a cookie then binds that
device to the code. A single-device share lets a second device replace the
first; a family share (max_devices > 1) seats several devices, each listed
for the owner with name, system and browser, and any of them can be
blocked — the share then gets a new code so the blocked one cannot return
while the others stay signed in.

The guest sees only the track and the car, nothing else of the app: no
numbers, no places, no charging locations. A share's delay shifts everything
the guest sees into the past (live, 1 h, 4 h, 24 h): route up to that moment,
car where it was then. Live means live — the car moves while the owner
drives (the page polls every 10 s).

Access is protected twice: the link key is the secret, the code is the
per-guest handshake. Wrong codes lock the share for a while and count per IP
across shares. The link stops working the moment the journey ends.
"""

import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import config
import detector
from flask import (Blueprint, jsonify, redirect, render_template, request,
                   url_for)

from app import (_LOCAL_TZ, _fetch_influx_state, _state_cache, _state_lock,
                 _translations, get_db, get_map_tile_url, kartenauto_fuer, log,
                 roundtrip_tile_relay)
from blueprints.journeys import (_datum_text, _journey_segments, journey_ende_utc,
                                 journey_stand)

share_bp = Blueprint("share", __name__)

CODE_FEHLVERSUCHE = 5         # wrong codes on one share, then it locks …
SPERRE_MIN = 15               # … for this long
IP_VERSUCHE_JE_STUNDE = 10    # wrong codes from one address per hour, all shares
COOKIE_TAGE = 90
SPUR_STUNDEN = 12             # how far back the live trail reaches at most

_ip_versuche = {}             # ip -> [timestamps of wrong codes]


def neuer_key():
    return secrets.token_hex(8)


# Code characters: digits and capitals without the look-alikes 0/O, 1/I/L —
# a code like 58Z3 is read out and typed without doubt.
CODE_ZEICHEN = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LAENGE = 4


def neuer_code():
    return "".join(secrets.choice(CODE_ZEICHEN) for _ in range(CODE_LAENGE))


def code_normieren(eingabe):
    """What the guest typed, as the code is stored: capitals, no spaces or dashes."""
    return re.sub(r"[^A-Z0-9]", "", str(eingabe or "").upper())


def _jetzt():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def gast_sprache():
    """The guest's browser language, among the app's translations; DE otherwise."""
    codes = {c.lower(): c for c in _translations}
    best = request.accept_languages.best_match(list(codes))
    return codes.get(best, "DE") if best else "DE"


def gast_t():
    return _translations.get(gast_sprache(), _translations["DE"])


def freigabe_laden(key):
    if not re.fullmatch(r"[0-9a-f]{16}", key or ""):
        return None
    db = get_db()
    row = db.execute(
        """SELECT s.*, COALESCE(j.share_title, j.title) AS title, j.date_from, j.date_to, j.device
           FROM journey_shares s JOIN journeys j ON j.id = s.journey_id
           WHERE s.key = ?""", (key,)).fetchone()
    db.close()
    return row


def ende_utc(row):
    """When the share stops working: the journey's end, exactly."""
    return journey_ende_utc(row["date_to"])


def beendet(row):
    return _jetzt() >= ende_utc(row)


def stichzeit(row):
    """The moment the guest is looking at: now minus the share's delay."""
    return _jetzt() - timedelta(minutes=row["delay_min"] or 0)


def _cookie_name(key):
    return f"idmate_gast_{key}"


GERAETE_WAHL = (1, 4, 8)      # one device, or a family link with room for several
NAME_MAX = 30


def geraet(row):
    """The device behind the request's cookie, if it is signed in to this
    share and not blocked; else None."""
    token = request.cookies.get(_cookie_name(row["key"]))
    if not token:
        return None
    db = get_db()
    d = db.execute("SELECT * FROM journey_share_devices WHERE share_id = ? AND token = ? AND blocked = 0",
                   (row["id"], token)).fetchone()
    db.close()
    return d


def geraet_ok(row):
    return geraet(row) is not None


def geraete_aktiv(db, share_id):
    return db.execute("SELECT COUNT(*) FROM journey_share_devices WHERE share_id = ? AND blocked = 0",
                      (share_id,)).fetchone()[0]


def geraet_aus_ua(ua):
    """(os, browser, model) as far as a browser tells: iOS/Android/Windows/…,
    Safari/Chrome/Firefox/…, and the model on Android (iPhones say only
    "iPhone"). A device NAME never reaches a website — the guest types it."""
    ua = ua or ""
    os_, model = "", ""
    if "iPhone" in ua:
        os_, model = "iOS", "iPhone"
    elif "iPad" in ua:
        os_, model = "iPadOS", "iPad"
    elif "Android" in ua:
        os_ = "Android"
        m = re.search(r"Android [\d.]+; ([^;)]+)", ua)
        if m:
            model = re.sub(r"\s*Build.*$", "", m.group(1)).strip()
            if model.lower() in ("k", "wv", "mobile"):
                model = ""
    elif "Windows" in ua:
        os_ = "Windows"
    elif "Macintosh" in ua or "Mac OS X" in ua:
        os_ = "macOS"
    elif "CrOS" in ua:
        os_ = "ChromeOS"
    elif "Linux" in ua:
        os_ = "Linux"
    if "Firefox/" in ua or "FxiOS" in ua:
        browser = "Firefox"
    elif "Edg/" in ua or "EdgiOS" in ua or "EdgA/" in ua:
        browser = "Edge"
    elif "SamsungBrowser" in ua:
        browser = "Samsung"
    elif "OPR/" in ua or "Opera" in ua:
        browser = "Opera"
    elif "Chrome/" in ua or "CriOS" in ua:
        browser = "Chrome"
    elif "Safari/" in ua:
        browser = "Safari"
    else:
        browser = ""
    return os_, browser, model


def gesperrt(row):
    lu = row["locked_until"]
    return bool(lu) and _parse(lu) > _jetzt()


def _ip_gesperrt(ip):
    now = time.time()
    liste = [t for t in _ip_versuche.get(ip, []) if now - t < 3600]
    if liste:
        _ip_versuche[ip] = liste
    else:
        _ip_versuche.pop(ip, None)
    return len(liste) >= IP_VERSUCHE_JE_STUNDE


def _ip_merken(ip):
    _ip_versuche.setdefault(ip, []).append(time.time())


def _code_seite(row, t, fehler=None, status=200, name=""):
    # Before the code nothing of the journey is shown — not even its title
    # or whether it is over; the link alone must not tell anything.
    familie = (row["max_devices"] or 1) > 1
    db = get_db()
    belegt = geraete_aktiv(db, row["id"])
    db.close()
    return render_template("share_code.html", t=t, lang=gast_sprache().lower(), key=row["key"],
                           titel="", fehler=fehler, beendet=False, abgelaufen=False, familie=familie,
                           name=name, anderes_geraet=(not familie and belegt > 0)), status


def _ende_seite(row, t):
    return render_template("share_code.html", t=t, lang=gast_sprache().lower(), key=row["key"],
                           titel=row["title"], fehler=None, beendet=True, abgelaufen=False,
                           anderes_geraet=False)


def _abgelaufen_seite(key):
    """A link that was removed (or never existed): a plain landing page, 410."""
    return render_template("share_code.html", t=gast_t(), lang=gast_sprache().lower(), key=key,
                           titel="", fehler=None, beendet=False, abgelaufen=True,
                           anderes_geraet=False), 410


@share_bp.route("/s/<key>")
def gast_seite(key):
    row = freigabe_laden(key)
    if not row:
        return _abgelaufen_seite(key)
    t = gast_t()
    if not geraet_ok(row):
        return _code_seite(row, t)
    if beendet(row):
        return _ende_seite(row, t)
    db = get_db()
    vehicle = db.execute("SELECT * FROM vehicles WHERE device = ?", (row["device"],)).fetchone()
    db.close()
    return render_template(
        "share_map.html", t=t, lang=gast_sprache().lower(), key=key, titel=row["title"],
        von_text=_datum_text(row["date_from"]), bis_text=_datum_text(row["date_to"]),
        versatz_min=row["delay_min"] or 0,
        kartenauto=kartenauto_fuer(dict(vehicle) if vehicle else None),
        map_tile_url=gast_kacheln(key))


def gast_kacheln(key):
    """The guest's tile URL: the Roundtrip world map goes through the
    login-only relay /tiles, so a verified guest gets it under the share's
    own path (the device cookie is scoped to /s/<key>). Raster styles are
    public hosts and stay as they are."""
    if config.ROUNDTRIP_URL:
        return f"/s/{key}/tiles/{{z}}/{{x}}/{{y}}.mvt"
    return get_map_tile_url()


@share_bp.route("/s/<key>/tiles/<int:z>/<int:x>/<int:y>.mvt")
def gast_kachel(key, z, x, y):
    row = freigabe_laden(key)
    if not row or not geraet_ok(row) or beendet(row):
        return jsonify({"error": "device"}), 401
    return roundtrip_tile_relay(z, x, y)


@share_bp.route("/s/<key>", methods=["POST"])
def gast_code(key):
    row = freigabe_laden(key)
    if not row:
        return _abgelaufen_seite(key)
    t = gast_t()
    ip = request.remote_addr or "?"
    if _ip_gesperrt(ip) or gesperrt(row):
        return _code_seite(row, t, t["share_guest_locked"], 429)
    code = code_normieren(request.form.get("code", ""))
    name = str(request.form.get("name") or "").strip()[:NAME_MAX]
    familie = (row["max_devices"] or 1) > 1
    db = get_db()
    if code and code == row["code"]:
        if not name:
            db.close()
            return _code_seite(row, t, t["share_guest_name_required"], 400)
        now = _iso(_jetzt())
        os_, browser, model = geraet_aus_ua(request.user_agent.string)
        alt = request.cookies.get(_cookie_name(key))
        eigenes = db.execute("SELECT id FROM journey_share_devices WHERE share_id = ? AND token = ? AND blocked = 0",
                             (row["id"], alt)).fetchone() if alt else None
        if eigenes:
            # the same device again (new name, expired session): keep its seat
            token = alt
            db.execute("UPDATE journey_share_devices SET name = ?, os = ?, browser = ?, model = ?, last_seen = ? WHERE id = ?",
                       (name, os_, browser, model, now, eigenes["id"]))
        else:
            if not familie:
                # one device per code: the newcomer replaces the old one
                db.execute("DELETE FROM journey_share_devices WHERE share_id = ?", (row["id"],))
            elif geraete_aktiv(db, row["id"]) >= row["max_devices"]:
                db.close()
                return _code_seite(row, t, t["share_guest_full"], 403, name)
            token = secrets.token_urlsafe(24)
            db.execute("""INSERT INTO journey_share_devices (share_id, token, name, os, browser, model, first_seen, last_seen)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (row["id"], token, name, os_, browser, model, now, now))
        db.execute("""UPDATE journey_shares SET device_token = NULL, device_since = ?, last_seen_at = ?,
                      failed_attempts = 0, locked_until = NULL WHERE id = ?""", (now, now, row["id"]))
        db.commit()
        db.close()
        resp = redirect(url_for("share.gast_seite", key=key))
        resp.set_cookie(_cookie_name(key), token, max_age=COOKIE_TAGE * 86400, httponly=True,
                        samesite="Lax", secure=request.is_secure, path=f"/s/{key}")
        return resp
    _ip_merken(ip)
    fehl = (row["failed_attempts"] or 0) + 1
    if fehl >= CODE_FEHLVERSUCHE:
        db.execute("UPDATE journey_shares SET failed_attempts = 0, locked_until = ? WHERE id = ?",
                   (_iso(_jetzt() + timedelta(minutes=SPERRE_MIN)), row["id"]))
    else:
        db.execute("UPDATE journey_shares SET failed_attempts = ? WHERE id = ?", (fehl, row["id"]))
    db.commit()
    db.close()
    if fehl >= CODE_FEHLVERSUCHE:
        return _code_seite(row, t, t["share_guest_locked"], 429, name)
    return _code_seite(row, t, t["share_guest_wrong"], 403, name)


@share_bp.route("/s/<key>/data")
def gast_daten(key):
    """What the guest page polls: route (only when its trip set changed),
    the live trail since the last poll, the car's position."""
    row = freigabe_laden(key)
    if not row:
        return jsonify({"abgelaufen": True}), 410
    d = geraet(row)
    if d is None:
        return jsonify({"error": "device"}), 401
    if beendet(row):
        return jsonify({"beendet": True})
    voll = request.args.get("stand") is None
    now = _iso(_jetzt())
    db = get_db()
    db.execute("UPDATE journey_shares SET last_seen_at = ?, views = views + ? WHERE id = ?",
               (now, 1 if voll else 0, row["id"]))
    db.execute("UPDATE journey_share_devices SET last_seen = ? WHERE id = ?", (now, d["id"]))
    db.commit()
    db.close()
    return jsonify(live_daten(row, stichzeit(row), request.args.get("stand"), request.args.get("since")))


def live_daten(row, stich, stand_alt=None, since=None):
    # Cheap first: has the set of trips changed? Only then the GPS points
    # (one InfluxDB query per trip — a long journey has dozens) are loaded
    # again; a 10 s poll must not redo that every time.
    stand, letztes_ende = journey_stand(row["journey_id"], stich)
    out = {"stand": stand, "bis": _iso(stich), "versatz_min": row["delay_min"] or 0,
           "jetzt": _iso(_jetzt())}
    neu = stand_alt != stand
    if neu:
        route = _journey_segments(row["journey_id"], bis=stich)
        out["segments"] = route["segments"]
        out["source"] = route.get("source")
        stand, letztes_ende = route["stand"], route["letztes_ende"]
        out["stand"] = stand
    # The live trail: what the car drove after its last finished trip (the
    # trips themselves are in the segments), at most SPUR_STUNDEN back, and
    # on a repeat poll only what is new since the last one.
    von = stich - timedelta(hours=SPUR_STUNDEN)
    if letztes_ende:
        von = max(von, _parse(letztes_ende))
    else:
        start = datetime.fromisoformat(str(row["date_from"])[:10]).replace(tzinfo=_LOCAL_TZ)
        von = max(von, start.astimezone(timezone.utc))
    if since and not neu:
        try:
            von = max(von, _parse(since))
        except ValueError:
            pass
    out["trail"] = _spur(row["device"], von, stich) if von < stich else []
    out["pos"] = _position(row["device"], stich, row["delay_min"] or 0)
    return out


def _spur(device, von, bis):
    client = detector.get_influx()
    if not client or not device:
        return []
    try:
        query = f'''
        from(bucket: "{config.INFLUX_BUCKET}")
          |> range(start: {_iso(von)}, stop: {_iso(bis)})
          |> filter(fn: (r) => r._measurement == "v" and r.d == "{device}")
          |> filter(fn: (r) => r._field == "la" or r._field == "lo")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
          |> sort(columns: ["_time"])
        '''
        raw = [rec.values for table in client.query_api().query(query, org=config.INFLUX_ORG)
               for rec in table.records]
        raw.sort(key=lambda r: r.get("_time") or datetime.min.replace(tzinfo=timezone.utc))
        raw = detector._filter_gps_jumps(detector._merge_rows(raw))
        return [[round(r["la"], 6), round(r["lo"], 6)] for r in raw
                if r.get("la") and r.get("lo")]
    except Exception:
        log.exception("guest trail failed for device %s", device)
        return []
    finally:
        client.close()


def _position(device, stich, versatz_min):
    """Where the car is (live: the MQTT state cache) or was at ``stich``
    (delayed: the last fix in InfluxDB before that moment)."""
    if not device:
        return None
    if not versatz_min:
        with _state_lock:
            st = dict(_state_cache.get(device, {}))
        if not st:
            # first sight of this device since start: one InfluxDB read,
            # then cached like the dashboard does (not on every poll)
            st = _fetch_influx_state(device) or {}
            if st:
                with _state_lock:
                    _state_cache[device] = dict(st)
        if st.get("lat") is None or st.get("lon") is None:
            return None
        return {"lat": st["lat"], "lon": st["lon"], "heading": st.get("heading"),
                "speed": st.get("speed"), "time": st.get("time")}
    client = detector.get_influx()
    if not client:
        return None
    try:
        query = f'''
        from(bucket: "{config.INFLUX_BUCKET}")
          |> range(start: {_iso(stich - timedelta(days=7))}, stop: {_iso(stich)})
          |> filter(fn: (r) => r._measurement == "v" and r.d == "{device}")
          |> filter(fn: (r) => r._field == "la" or r._field == "lo" or r._field == "hd" or r._field == "v")
          |> group(columns: ["_field"])
          |> last()
        '''
        werte, zeit = {}, None
        for table in client.query_api().query(query, org=config.INFLUX_ORG):
            for rec in table.records:
                werte[rec.get_field()] = rec.get_value()
                t = rec.get_time()
                if t is not None and (zeit is None or t > zeit):
                    zeit = t
        if not werte.get("la") or not werte.get("lo"):
            return None
        if zeit is not None and zeit.tzinfo is None:
            zeit = zeit.replace(tzinfo=timezone.utc)
        return {"lat": werte["la"], "lon": werte["lo"], "heading": werte.get("hd"),
                "speed": werte.get("v"), "time": _iso(zeit) if zeit else None}
    except Exception:
        log.exception("guest position failed for device %s", device)
        return None
    finally:
        client.close()
