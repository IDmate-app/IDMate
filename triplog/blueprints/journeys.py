"""Journey (multi-trip travel) routes (FIXES 6.1).

Moved verbatim out of app.py. Handler bodies unchanged except
``@app.route`` → ``@journeys_bp.route`` and the one internal
``url_for("journeys_list")`` → ``url_for("journeys.journeys_list")`` (endpoint
names gain the blueprint prefix; templates use hardcoded paths, so unaffected).
Shared helpers come from app at blueprint-import time (bottom of app.py).
"""

import glob
import io
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import config
import detector
import fahrzeugmodell as fm
import gpxexport
import kartenbild
from flask import (Blueprint, Response, jsonify, redirect, render_template,
                   request, url_for)
from flask_login import login_required

from app import (MEDIA_DIR, _LOCAL_TZ, _operator_icon_map, _to_rfc3339, _to_rfc3339_padded,
                 _translations, active_device, get_db, get_language, log)
from app import app as flask_app
from journey_sync import IN_RANGE, sync_journey_trips

journeys_bp = Blueprint("journeys", __name__)

# A journey holds every trip of its vehicle whose start lies in the journey's
# days (date_from..date_to, whole days) — also trips detected after the journey
# was created — except the ones removed by hand (journey_trip_excluded).
# journey_trips stays the materialised membership: sync_journey_trips() (in
# journey_sync.py — the detector and the importers call it right after new
# trips are written, and every view below calls it again) tops it up, so all
# queries here keep joining it. IN_RANGE is that period rule as SQL.


def _set_journey_trips(db, journey_id, excluded=None, included=None):
    """Membership after the create/edit dialog: trips in range minus the
    unticked ones (``excluded``). Older clients send the ticked ones
    (``included``) instead; everything else in range counts as unticked."""
    in_range = {r["id"] for r in db.execute(
        f"SELECT t.id FROM journeys j JOIN trips t ON {IN_RANGE} WHERE j.id = ?", (journey_id,))}
    if excluded is None:
        keep = {int(t) for t in (included or [])}
        excluded = in_range - keep
    else:
        excluded = {int(t) for t in excluded} & in_range
    db.execute("DELETE FROM journey_trip_excluded WHERE journey_id = ?", (journey_id,))
    db.executemany("INSERT INTO journey_trip_excluded (journey_id, trip_id) VALUES (?, ?)",
                   [(journey_id, t) for t in excluded])
    db.execute("DELETE FROM journey_trips WHERE journey_id = ?", (journey_id,))
    sync_journey_trips(db, journey_id=journey_id)


def _datum_text(wert):
    """``2026-06-14T08:00`` → ``14.06.2026`` for the list; anything else verbatim."""
    s = str(wert or "")
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return s


def journey_ende_utc(date_to):
    """The journey's end as aware UTC: its date_to exactly; a date without a
    time counts until the end of that day. Guest links live until then."""
    roh = str(date_to or "").strip()
    try:
        ende = datetime.fromisoformat(roh.replace(" ", "T"))
    except ValueError:
        ende = datetime.fromisoformat(roh[:10])
    if len(roh) <= 10:
        ende += timedelta(days=1)
    if ende.tzinfo is None:
        ende = ende.replace(tzinfo=_LOCAL_TZ)
    return ende.astimezone(timezone.utc)


def _thumb_key(row):
    """Cache key of a journey's mini map: changes whenever its trips change
    (count, newest trip, total km), so the browser keeps the old image until
    then."""
    return f"{THUMB_STAND}-{row['trip_count']}-{row['letzte_fahrt']}-{int(row['total_km'] or 0)}"


THUMB_STAND = 2  # bump when the rendering changes: new URLs, old files replaced


_LISTE_SQL = """SELECT j.*, COUNT(jt.trip_id) AS trip_count,
                  COALESCE(SUM(t.distance_km), 0) AS total_km,
                  COALESCE(MAX(t.id), 0) AS letzte_fahrt,
                  (SELECT COUNT(*) FROM journey_shares s WHERE s.journey_id = j.id) AS gaeste
           FROM journeys j
           LEFT JOIN journey_trips jt ON jt.journey_id = j.id
           LEFT JOIN trips t ON t.id = jt.trip_id
           WHERE {wo}
           GROUP BY j.id ORDER BY j.date_from DESC"""


@journeys_bp.route("/journeys")
@login_required
def journeys_list():
    device = active_device()
    db = get_db()
    sync_journey_trips(db, device=device)
    rows = db.execute(_LISTE_SQL.format(wo="j.device = ?"), (device,)).fetchall()
    db.close()
    journeys = []
    for r in rows:
        j = dict(r)
        j["thumb_key"] = _thumb_key(r)
        j["von_text"] = _datum_text(r["date_from"])
        j["bis_text"] = _datum_text(r["date_to"])
        # the red lamp: guests hold a link and the journey is still on
        j["gaeste_live"] = r["gaeste"] if r["gaeste"] and journey_ende_utc(r["date_to"]) > datetime.now(timezone.utc) else 0
        journeys.append(j)
    return render_template("journeys.html", journeys=journeys)


# --- Mini map of a journey (list thumbnail) ---------------------------------
# A square picture of the route on a map background (kartenbild.py), kept as
# a WebP file under media/journey-thumbs/ per journey state: the list shows it
# as a plain <img>. The URL carries the thumb key and the response is
# immutable, so the browser caches it until the journey's trips change.
# Pictures are rendered ahead of time — after each new trip, on journey edits
# and once at start-up for anything missing — because a render loads the GPS
# points of every trip (InfluxDB) and takes seconds on a long journey.

THUMB_PX = kartenbild.GROESSE
THUMB_RAND = kartenbild.RAND
THUMB_FARBE = kartenbild.ROUTE
THUMB_HINTERGRUND = kartenbild.HINTERGRUND
THUMB_STAND = 6               # bump when the rendering changes: new URLs, old files replaced

_thumb_laufend = set()        # journey ids a background render is working on
_thumb_sperre = threading.Lock()


def _vektorkachel_laden(z, x, y):
    """One Roundtrip vector tile: its bytes, b"" for an empty tile (sea),
    None when the Roundtrip host does not answer."""
    url = f"{config.ROUNDTRIP_URL}/tiles/{z}/{x}/{y}.mvt"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "IDmate"}),
                                    timeout=8) as r:
            return b"" if r.status == 204 else r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return b""
        log.warning("thumb vector tile %d/%d/%d: upstream %s", z, x, y, e.code)
        return None
    except Exception as e:
        log.warning("thumb vector tile %d/%d/%d: %s", z, x, y, e)
        return None


def _thumb_bild(segments):
    """(image, complete): the route on the Roundtrip world map when it is
    configured (no labels), else on the plain dark background."""
    if config.ROUNDTRIP_URL:
        return kartenbild.bild(segments, vektor_kacheln=_vektorkachel_laden)
    return kartenbild.bild(segments)


def _thumb_bytes(bild):
    puffer = io.BytesIO()
    bild.save(puffer, "WEBP", quality=82, method=4)
    return puffer.getvalue()


def _thumb_dir():
    return os.path.join(MEDIA_DIR, "journey-thumbs")


def _thumb_datei(journey_id):
    """(path, key) of the journey's current thumbnail file, or (None, None)
    for an unknown journey."""
    db = get_db()
    if not db.execute("SELECT id FROM journeys WHERE id = ?", (journey_id,)).fetchone():
        db.close()
        return None, None
    sync_journey_trips(db, journey_id=journey_id)
    row = db.execute(_LISTE_SQL.format(wo="j.id = ?"), (journey_id,)).fetchone()
    db.close()
    key = re.sub(r"[^0-9-]", "", _thumb_key(row))
    return os.path.join(_thumb_dir(), f"{journey_id}-{key}.webp"), key


def _thumb_rendern(journey_id, datei):
    """Render the picture, keep it as ``datei`` when complete (older files of
    the journey go), return (bytes, complete)."""
    bild, fertig = _thumb_bild(_journey_segments(journey_id)["segments"])
    daten = _thumb_bytes(bild)
    if fertig:
        try:
            ordner = os.path.dirname(datei)
            os.makedirs(ordner, exist_ok=True)
            for alt in glob.glob(os.path.join(ordner, f"{journey_id}-*.*")):
                if alt != datei:
                    os.remove(alt)
            with open(datei, "wb") as f:
                f.write(daten)
        except OSError:
            log.exception("journey thumb %d could not be cached", journey_id)
    return daten, fertig


def thumb_vorrendern(journey_id):
    """Make sure the journey's current picture exists (background use)."""
    with _thumb_sperre:
        if journey_id in _thumb_laufend:
            return
        _thumb_laufend.add(journey_id)
    try:
        datei, _ = _thumb_datei(journey_id)
        if datei and not os.path.exists(datei):
            _thumb_rendern(journey_id, datei)
    except Exception:
        log.exception("journey thumb %d: pre-render failed", journey_id)
    finally:
        with _thumb_sperre:
            _thumb_laufend.discard(journey_id)


def thumbs_im_hintergrund(journey_ids):
    """Pre-render these journeys' pictures in a daemon thread (no-op in
    tests, which would otherwise reach for real map tiles)."""
    ids = [int(j) for j in journey_ids if j]
    if not ids or flask_app.testing:
        return
    threading.Thread(target=lambda: [thumb_vorrendern(j) for j in ids],
                     name="journey-thumbs", daemon=True).start()


def thumbs_alle_im_hintergrund():
    """At start-up: every journey whose picture for its current state is
    missing (first run, or after a rendering change)."""
    try:
        db = get_db()
        ids = [r[0] for r in db.execute("SELECT id FROM journeys ORDER BY date_from DESC")]
        db.close()
    except Exception:
        log.exception("journey thumbs: could not list journeys")
        return
    thumbs_im_hintergrund(ids)


@journeys_bp.route("/journeys/<int:journey_id>/thumb.webp")
@login_required
def journey_thumb(journey_id):
    datei, key = _thumb_datei(journey_id)
    if not datei:
        return Response("", status=404)
    # a background render for this journey is on its way: wait for it a while
    # instead of loading every trip's GPS points a second time
    for _ in range(50):
        with _thumb_sperre:
            laeuft = journey_id in _thumb_laufend
        if not laeuft or os.path.exists(datei):
            break
        time.sleep(0.5)
    fertig = True
    try:
        with open(datei, "rb") as f:
            daten = f.read()
    except OSError:
        daten, fertig = _thumb_rendern(journey_id, datei)
    # a picture with a missing tile is shown but fetched again next time
    cache = "private, max-age=31536000, immutable" if fertig else "no-cache"
    return Response(daten, mimetype="image/webp", headers={"Cache-Control": cache})


@journeys_bp.route("/journeys/<int:journey_id>")
@login_required
def journey_detail(journey_id):
    db = get_db()
    journey = db.execute("SELECT * FROM journeys WHERE id = ?", (journey_id,)).fetchone()
    if not journey:
        db.close()
        return redirect(url_for("journeys.journeys_list"))
    sync_journey_trips(db, journey_id=journey_id)
    excluded_ids = [r["trip_id"] for r in db.execute(
        "SELECT trip_id FROM journey_trip_excluded WHERE journey_id = ?", (journey_id,))]
    trips = db.execute(
        """SELECT t.* FROM trips t
           JOIN journey_trips jt ON jt.trip_id = t.id
           WHERE jt.journey_id = ?
           ORDER BY t.start_time""",
        (journey_id,),
    ).fetchall()
    # Filter charge sessions: link vehicle via device -> plate
    veh = db.execute("SELECT * FROM vehicles WHERE device = ?",
                     (journey["device"],)).fetchone()
    if veh:
        # date(?) on both sides: date_from carries a time ("2026-07-15T08:00"),
        # and "2026-07-15" >= "2026-07-15T08:00" is false as text — the first
        # day's charges went missing
        charges = db.execute(
            """SELECT * FROM charge_sessions
               WHERE vehicle_plate = ? AND date(start_time) >= date(?) AND date(start_time) <= date(?)
               ORDER BY start_time""",
            (veh["plate"], journey["date_from"], journey["date_to"]),
        ).fetchall()
    else:
        charges = db.execute(
            """SELECT * FROM charge_sessions
               WHERE date(start_time) >= date(?) AND date(start_time) <= date(?)
               ORDER BY start_time""",
            (journey["date_from"], journey["date_to"]),
        ).fetchall()
    vehicles = db.execute("SELECT plate, name FROM vehicles").fetchall()
    vehicle_names = {v["plate"]: v["name"] or v["plate"] for v in vehicles}
    op_icon_map = _operator_icon_map(db)
    # loc_operators for charge icons (like charges.html)
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
            'color': cl['op_color'] or cl['color'] or '#8b949e',
            'icon_url': icon_url,
        }
    # Calculate drive time, charge time, average speed
    drive_minutes = 0
    for t in trips:
        if t["start_time"] and t["end_time"]:
            try:
                t0 = datetime.fromisoformat(t["start_time"])
                t1 = datetime.fromisoformat(t["end_time"])
                drive_minutes += (t1 - t0).total_seconds() / 60
            except Exception:
                log.debug("drive-time: bad timestamps on trip %s — skipped", t["id"] if "id" in t.keys() else "?", exc_info=True)
    # Split charge times by AC/DC
    loc_types = {}
    for cl in db.execute("SELECT name, type FROM charge_locations").fetchall():
        loc_types[cl["name"]] = (cl["type"] or "ac").lower()
    ac_minutes = 0
    dc_minutes = 0
    for c in charges:
        dur = c["duration_minutes"] or 0
        if loc_types.get(c["location_name"]) == "dc":
            dc_minutes += dur
        else:
            ac_minutes += dur
    total_km = sum(t["distance_km"] or 0 for t in trips)
    avg_speed = round(total_km / (drive_minutes / 60), 1) if drive_minutes > 0 else None
    preis = preis_je_kwh(charges)
    # Consumption by the one rule (trips ≥ MIN_CONS_KM, km-weighted) and
    # the charged energy on the grid side of the energy book
    from app import _get_charge_efficiency
    cons = fm.avg_consumption([dict(t) for t in trips])
    eff = {}
    charged_kwh = 0.0
    for c in charges:
        p = c["vehicle_plate"]
        if p not in eff:
            eff[p] = (_get_charge_efficiency(db, p), _get_charge_efficiency(db, p, dc=True))
        e = fm.session_energy(dict(c), *eff[p])
        charged_kwh += e["grid"] if e else 0

    db.close()
    return render_template("journey_detail.html",
                           preis=preis,
                           cons=cons,
                           charged_kwh=charged_kwh,
                           journey=dict(journey),
                           trips=[dict(t) for t in trips],
                           excluded_ids=excluded_ids,
                           charges=[dict(c) for c in charges],
                           vehicle_names=vehicle_names,
                           op_icon_map=op_icon_map,
                           loc_operators=loc_operators,
                           drive_minutes=round(drive_minutes),
                           ac_minutes=round(ac_minutes),
                           dc_minutes=round(dc_minutes),
                           avg_speed=avg_speed)


def preis_je_kwh(charges):
    """ct/kWh over the journey's paid charges: the average is total cost over
    total energy (not the mean of the per-charge prices), min and max are the
    cheapest and dearest single charge. None without a paid charge."""
    paare = []
    for c in charges:
        kosten = c["cost_total"] if c["cost_total"] is not None else c["cost_pauschale"]
        kwh = c["total_kwh"] or 0
        if kosten is not None and kosten > 0 and kwh > 0:
            paare.append((float(kosten), float(kwh)))
    if not paare:
        return None
    return {"avg": round(sum(k for k, _ in paare) / sum(e for _, e in paare) * 100, 1),
            "min": round(min(k / e for k, e in paare) * 100, 1),
            "max": round(max(k / e for k, e in paare) * 100, 1),
            "n": len(paare)}


@journeys_bp.route("/api/journeys", methods=["POST"])
@login_required
def create_journey():
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    date_from = (data.get("date_from") or "").strip()
    date_to = (data.get("date_to") or "").strip()
    notes = (data.get("notes") or "").strip()
    if not title or not date_from or not date_to:
        lang = get_language()
        _t = _translations.get(lang, _translations["DE"])
        return jsonify({"error": _t["journeys_required_fields"]}), 400
    device = active_device()
    db = get_db()
    cur = db.execute(
        "INSERT INTO journeys (device, title, date_from, date_to, notes) VALUES (?,?,?,?,?)",
        (device, title, date_from, date_to, notes),
    )
    jid = cur.lastrowid
    if "excluded_ids" in data or "trip_ids" in data:
        _set_journey_trips(db, jid, excluded=data.get("excluded_ids"), included=data.get("trip_ids"))
    else:
        sync_journey_trips(db, journey_id=jid)
    db.commit()
    db.close()
    thumbs_im_hintergrund([jid])
    return jsonify({"ok": True, "id": jid})


@journeys_bp.route("/api/journeys/<int:journey_id>", methods=["POST"])
@login_required
def update_journey(journey_id):
    data = request.get_json(force=True)
    db = get_db()
    sets, vals = [], []
    for col in ("title", "date_from", "date_to", "notes", "share_title"):
        if col in data:
            val = data[col]
            if col == "share_title":
                val = str(val or "").strip()[:60] or None
            sets.append(f"{col} = ?")
            vals.append(val)
    if sets:
        vals.append(journey_id)
        db.execute(f"UPDATE journeys SET {', '.join(sets)} WHERE id = ?", vals)
    if "excluded_ids" in data or "trip_ids" in data:
        _set_journey_trips(db, journey_id, excluded=data.get("excluded_ids"), included=data.get("trip_ids"))
    db.commit()
    db.close()
    thumbs_im_hintergrund([journey_id])
    return jsonify({"ok": True})


@journeys_bp.route("/api/journeys/<int:journey_id>/trips/<int:trip_id>", methods=["DELETE"])
@login_required
def journey_remove_trip(journey_id, trip_id):
    """Take one trip out of the journey for good — the auto-fill skips it.
    Ticking it again in the edit dialog brings it back."""
    db = get_db()
    ok = db.execute(
        f"SELECT 1 FROM journeys j JOIN trips t ON {IN_RANGE} WHERE j.id = ? AND t.id = ?",
        (journey_id, trip_id)).fetchone()
    if ok:
        db.execute("INSERT OR IGNORE INTO journey_trip_excluded (journey_id, trip_id) VALUES (?, ?)",
                   (journey_id, trip_id))
    db.execute("DELETE FROM journey_trips WHERE journey_id = ? AND trip_id = ?", (journey_id, trip_id))
    db.commit()
    db.close()
    thumbs_im_hintergrund([journey_id])
    return jsonify({"ok": True})


@journeys_bp.route("/api/journeys/<int:journey_id>/trips/restore", methods=["POST"])
@login_required
def journey_restore_trips(journey_id):
    """Undo every by-hand removal at once: the journey goes back to "all trips
    of its period", the auto-fill takes over again. Saves ticking each trip in
    the edit dialog when the removals are stale — e.g. the conservative guess
    m054 made for journeys that predate the auto-fill."""
    db = get_db()
    n = db.execute("DELETE FROM journey_trip_excluded WHERE journey_id = ?",
                   (journey_id,)).rowcount
    db.commit()
    sync_journey_trips(db, journey_id=journey_id)
    db.close()
    return jsonify({"ok": True, "restored": n})


@journeys_bp.route("/api/journeys/<int:journey_id>", methods=["DELETE"])
@login_required
def delete_journey(journey_id):
    db = get_db()
    db.execute("DELETE FROM journeys WHERE id = ?", (journey_id,))
    db.commit()
    db.close()
    thumbs_im_hintergrund([journey_id])
    return jsonify({"ok": True})


@journeys_bp.route("/api/journeys/<int:journey_id>/trips")
@login_required
def journey_trips_list(journey_id):
    """All trips of a journey with GPS data."""
    db = get_db()
    sync_journey_trips(db, journey_id=journey_id)
    trips = db.execute(
        """SELECT t.* FROM trips t
           JOIN journey_trips jt ON jt.trip_id = t.id
           WHERE jt.journey_id = ?
           ORDER BY t.start_time""",
        (journey_id,),
    ).fetchall()
    db.close()
    return jsonify([dict(t) for t in trips])


@journeys_bp.route("/api/journeys/<int:journey_id>/route")
@login_required
def journey_route(journey_id):
    return jsonify(_journey_segments(journey_id))


def _utc(ts):
    """Stored trip time (local, naive) as an aware UTC datetime."""
    return datetime.fromisoformat(_to_rfc3339(ts).replace("Z", "+00:00"))


def _journey_trips(db, journey_id, bis=None):
    """The journey's trips (ordered), those started by ``bis`` only, plus
    ``stand`` (names that set) and the newest trip end within ``bis``
    (RFC3339) — cheap, SQLite only. A guest poll asks this first and loads
    the GPS points (InfluxDB, one query per trip) only when the stand moved."""
    sync_journey_trips(db, journey_id=journey_id)
    trips = db.execute(
        """SELECT t.id, t.device, t.start_time, t.end_time,
                  t.start_lat, t.start_lon, t.end_lat, t.end_lon, t.is_gpx
           FROM trips t
           JOIN journey_trips jt ON jt.trip_id = t.id
           WHERE jt.journey_id = ?
           ORDER BY t.start_time""",
        (journey_id,),
    ).fetchall()
    if bis is not None:
        trips = [t for t in trips if _utc(t["start_time"]) <= bis]
    stand = f"{len(trips)}-{max((t['id'] for t in trips), default=0)}"
    enden = [_to_rfc3339(t["end_time"]) for t in trips if bis is None or _utc(t["end_time"]) <= bis]
    return trips, stand, (max(enden) if enden else None)


def journey_stand(journey_id, bis=None):
    """(stand, letztes_ende) of a journey without touching InfluxDB."""
    db = get_db()
    if not db.execute("SELECT id FROM journeys WHERE id = ?", (journey_id,)).fetchone():
        db.close()
        return "0-0", None
    _, stand, letztes_ende = _journey_trips(db, journey_id, bis)
    db.close()
    return stand, letztes_ende


def _journey_segments(journey_id, bis=None):
    """GPS points of all trips in a journey.
    Priority: 1) InfluxDB time series  2) GPX waypoints  3) start/end coordinates of the trip

    ``bis`` (aware UTC datetime): only what had happened by then — trips that
    started later are left out, one still running then is cut there (the
    guest view with a delay, blueprints/share.py). ``stand`` names the set of
    trips shown, ``letztes_ende`` is the newest trip end (RFC3339) so a caller
    knows where a live trail has to start.
    """
    db = get_db()
    if not db.execute("SELECT id FROM journeys WHERE id = ?", (journey_id,)).fetchone():
        db.close()
        return {"segments": [], "stand": "0-0", "letztes_ende": None}
    trips, stand, letztes_ende = _journey_trips(db, journey_id, bis)
    bis_iso = bis.strftime("%Y-%m-%dT%H:%M:%SZ") if bis is not None else None

    def ende_drin(trip):
        return bis is None or _utc(trip["end_time"]) <= bis

    # Pre-load GPX waypoints (trip_id → [[lat,lon], ...])
    gpx_tracks = {}
    gpx_trip_ids = [t["id"] for t in trips if t["is_gpx"]]
    if gpx_trip_ids:
        placeholders = ",".join("?" * len(gpx_trip_ids))
        wpts = db.execute(
            f"SELECT trip_id, lat, lon FROM gpx_waypoints WHERE trip_id IN ({placeholders}) ORDER BY trip_id, timestamp",
            gpx_trip_ids,
        ).fetchall()
        for w in wpts:
            gpx_tracks.setdefault(w["trip_id"], []).append([round(w["lat"], 6), round(w["lon"], 6)])

    db.close()

    segments = []

    # 1) InfluxDB: query grouped by trip device
    client = detector.get_influx()
    if client:
        try:
            from collections import defaultdict
            by_device = defaultdict(list)
            for trip in trips:
                if trip["device"]:
                    by_device[trip["device"]].append(trip)

            for device, dev_trips in by_device.items():
                for trip in dev_trips:
                    stop = _to_rfc3339_padded(trip["end_time"])
                    if bis_iso and bis_iso < stop:
                        stop = bis_iso
                    query = f'''
                    from(bucket: "{config.INFLUX_BUCKET}")
                      |> range(start: {_to_rfc3339(trip["start_time"])}, stop: {stop})
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
                    raw = detector._filter_gps_jumps(raw)
                    pts = []
                    for rec in raw:
                        lat = rec.get("la")
                        lon = rec.get("lo")
                        if lat and lon and lat != 0 and lon != 0:
                            pts.append([round(lat, 6), round(lon, 6)])
                    if pts:
                        segments.append(pts)
        except Exception:
            log.exception("Error loading journey route %d", journey_id)
        finally:
            client.close()

    # 2) Fallback: GPX waypoints for trips without InfluxDB data
    if not segments:
        for trip in trips:
            if trip["id"] in gpx_tracks and ende_drin(trip):
                pts = gpx_tracks[trip["id"]]
                if pts:
                    segments.append(pts)

    # 3) Fallback: start/end coordinates of trips
    if not segments:
        for trip in trips:
            pts = []
            if trip["start_lat"] and trip["start_lon"]:
                pts.append([round(trip["start_lat"], 6), round(trip["start_lon"], 6)])
            if trip["end_lat"] and trip["end_lon"] and ende_drin(trip):
                pts.append([round(trip["end_lat"], 6), round(trip["end_lon"], 6)])
            if pts:
                segments.append(pts)

    return {"segments": segments, "source": "influx" if segments and client else ("gpx" if gpx_tracks else "coords"),
            "stand": stand, "letztes_ende": letztes_ende}


# --- Guest access ("Live teilen") -------------------------------------------
# One row per guest: link key + four-digit code, a delay, and what the guest
# did with it (last_seen_at, views, bound device). The guest side lives in
# blueprints/share.py; these routes are the owner's list/create/reset/remove.

VERSATZ_MIN = (0, 60, 240, 1440)


def _share_row(db, r):
    d = dict(r)
    # The page composes the link from its own origin (the address the owner
    # really uses); the server-side guess behind a proxy can be http:// or an
    # internal host name and stays only as a fallback.
    d["path"] = url_for("share.gast_seite", key=r["key"])
    d["url"] = url_for("share.gast_seite", key=r["key"], _external=True)
    d.pop("device_token", None)
    d["devices"] = [dict(x) for x in db.execute(
        """SELECT id, name, os, browser, model, first_seen, last_seen, blocked
           FROM journey_share_devices WHERE share_id = ? ORDER BY first_seen, id""", (r["id"],))]
    d["devices_active"] = sum(1 for x in d["devices"] if not x["blocked"])
    d["device_bound"] = d["devices_active"] > 0
    return d


def _share_laden(db, journey_id, share_id):
    return db.execute("SELECT * FROM journey_shares WHERE id = ? AND journey_id = ?",
                      (share_id, journey_id)).fetchone()


@journeys_bp.route("/api/journeys/<int:journey_id>/shares")
@login_required
def journey_shares(journey_id):
    db = get_db()
    rows = db.execute("SELECT * FROM journey_shares WHERE journey_id = ? ORDER BY id", (journey_id,)).fetchall()
    out = [_share_row(db, r) for r in rows]
    db.close()
    return jsonify(out)


@journeys_bp.route("/api/journeys/<int:journey_id>/shares", methods=["POST"])
@login_required
def journey_share_create(journey_id):
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()[:40]
    try:
        delay_min = int(data.get("delay_min") or 0)
    except (TypeError, ValueError):
        delay_min = -1
    from blueprints.share import GERAETE_WAHL, neuer_code, neuer_key
    try:
        max_devices = int(data.get("max_devices") or 1)
    except (TypeError, ValueError):
        max_devices = -1
    # the name is the owner's label for the link ("Familie", "Bowling"), optional —
    # every guest types their own name when entering the code
    if delay_min not in VERSATZ_MIN or max_devices not in GERAETE_WAHL:
        return jsonify({"error": "delay_min/max_devices"}), 400
    db = get_db()
    if not db.execute("SELECT id FROM journeys WHERE id = ?", (journey_id,)).fetchone():
        db.close()
        return jsonify({"error": "journey"}), 404
    cur = db.execute(
        "INSERT INTO journey_shares (journey_id, name, key, code, delay_min, max_devices) VALUES (?, ?, ?, ?, ?, ?)",
        (journey_id, name, neuer_key(), neuer_code(), delay_min, max_devices))
    db.commit()
    row = db.execute("SELECT * FROM journey_shares WHERE id = ?", (cur.lastrowid,)).fetchone()
    out = _share_row(db, row)
    db.close()
    return jsonify(out)


@journeys_bp.route("/api/journeys/<int:journey_id>/shares/<int:share_id>/reset", methods=["POST"])
@login_required
def journey_share_reset(journey_id, share_id):
    """New code; every device signed in with the old one is out."""
    from blueprints.share import neuer_code
    db = get_db()
    if not _share_laden(db, journey_id, share_id):
        db.close()
        return jsonify({"error": "share"}), 404
    db.execute("DELETE FROM journey_share_devices WHERE share_id = ?", (share_id,))
    db.execute("""UPDATE journey_shares SET code = ?, device_token = NULL, device_since = NULL,
                  failed_attempts = 0, locked_until = NULL WHERE id = ?""", (neuer_code(), share_id))
    db.commit()
    out = _share_row(db, _share_laden(db, journey_id, share_id))
    db.close()
    return jsonify(out)


@journeys_bp.route("/api/journeys/<int:journey_id>/shares/<int:share_id>/devices/<int:device_id>/block", methods=["POST"])
@login_required
def journey_share_block_device(journey_id, share_id, device_id):
    """Throw one device out. The share gets a new code at the same time, so
    the blocked device cannot simply sign in again; the other devices keep
    their cookies and notice nothing."""
    from blueprints.share import neuer_code
    db = get_db()
    if not _share_laden(db, journey_id, share_id):
        db.close()
        return jsonify({"error": "share"}), 404
    cur = db.execute("UPDATE journey_share_devices SET blocked = 1 WHERE id = ? AND share_id = ?", (device_id, share_id))
    if cur.rowcount == 0:
        db.close()
        return jsonify({"error": "device"}), 404
    db.execute("UPDATE journey_shares SET code = ?, failed_attempts = 0, locked_until = NULL WHERE id = ?",
               (neuer_code(), share_id))
    db.commit()
    out = _share_row(db, _share_laden(db, journey_id, share_id))
    db.close()
    return jsonify(out)


@journeys_bp.route("/api/journeys/<int:journey_id>/shares/<int:share_id>", methods=["DELETE"])
@login_required
def journey_share_delete(journey_id, share_id):
    db = get_db()
    db.execute("DELETE FROM journey_shares WHERE id = ? AND journey_id = ?", (share_id, journey_id))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@journeys_bp.route("/api/journeys/<int:journey_id>/chart-data")
@login_required
def journey_chart_data(journey_id):
    """Daily kilometers of the journey trips. Rest days within the journey's
    date_from..date_to span are filled with 0 so the bar chart doesn't squash
    two non-adjacent travel days next to each other and fake a daily routine."""
    db = get_db()
    journey = db.execute(
        "SELECT date_from, date_to FROM journeys WHERE id = ?",
        (journey_id,),
    ).fetchone()
    if not journey:
        db.close()
        return jsonify({"labels": [], "values": []})
    sync_journey_trips(db, journey_id=journey_id)
    rows = db.execute(
        """SELECT date(t.start_time) AS day, SUM(t.distance_km) AS km
           FROM trips t
           JOIN journey_trips jt ON jt.trip_id = t.id
           WHERE jt.journey_id = ?
           GROUP BY day ORDER BY day""",
        (journey_id,),
    ).fetchall()
    db.close()
    km_by_day = {
        r["day"]: round(r["km"], 1) if r["km"] is not None else 0
        for r in rows
    }
    try:
        start = datetime.fromisoformat(journey["date_from"]).date()
        end = datetime.fromisoformat(journey["date_to"]).date()
    except (TypeError, ValueError):
        # Malformed journey dates — fall back to just the days the SQL returned.
        return jsonify({"labels": list(km_by_day), "values": list(km_by_day.values())})
    labels, values = [], []
    day = start
    while day <= end:
        key = day.isoformat()
        labels.append(key)
        values.append(km_by_day.get(key, 0))
        day += timedelta(days=1)
    return jsonify({"labels": labels, "values": values})


@journeys_bp.route("/api/journeys/<int:journey_id>/gpx")
@login_required
def journey_gpx(journey_id):
    """GPX export of all trips in a journey: one track per trip, points from
    InfluxDB with time and speed; a trip without them falls back to its GPX
    waypoints or its start and end coordinates, so the file never comes out
    empty while the map shows a route."""
    db = get_db()
    journey = db.execute("SELECT * FROM journeys WHERE id = ?", (journey_id,)).fetchone()
    if not journey:
        db.close()
        lang = get_language()
        _t = _translations.get(lang, _translations["DE"])
        return jsonify({"error": _t["journey_not_found"]}), 404
    sync_journey_trips(db, journey_id=journey_id)
    trips = db.execute(
        """SELECT t.id, t.start_time, t.end_time, t.start_lat, t.start_lon, t.end_lat, t.end_lon, t.is_gpx
           FROM trips t JOIN journey_trips jt ON jt.trip_id = t.id
           WHERE jt.journey_id = ? ORDER BY t.start_time""",
        (journey_id,),
    ).fetchall()
    wegpunkte = {}
    gpx_ids = [t["id"] for t in trips if t["is_gpx"]]
    if gpx_ids:
        for w in db.execute(
                f"SELECT trip_id, lat, lon, timestamp FROM gpx_waypoints WHERE trip_id IN ({','.join('?' * len(gpx_ids))}) "
                "ORDER BY trip_id, timestamp", gpx_ids):
            wegpunkte.setdefault(w["trip_id"], []).append((w["lat"], w["lon"], w["timestamp"], None))
    db.close()

    lang = get_language()
    _t = _translations.get(lang, _translations["DE"])
    device = journey["device"]
    client = detector.get_influx()
    tracks = []
    try:
        for i, trip in enumerate(trips):
            punkte = []
            if client:
                try:
                    query = f'''
                    from(bucket: "{config.INFLUX_BUCKET}")
                      |> range(start: {_to_rfc3339(trip["start_time"])}, stop: {_to_rfc3339_padded(trip["end_time"])})
                      |> filter(fn: (r) => r._measurement == "v" and r.d == "{device}")
                      |> filter(fn: (r) => r._field == "la" or r._field == "lo" or r._field == "v")
                      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
                      |> filter(fn: (r) => exists r.la and exists r.lo)
                      |> sort(columns: ["_time"])
                    '''
                    for table in client.query_api().query(query, org=config.INFLUX_ORG):
                        for rec in table.records:
                            la, lo, t = rec.values.get("la"), rec.values.get("lo"), rec.values.get("_time")
                            if la and lo:
                                punkte.append((la, lo, t.isoformat() if t else None, rec.values.get("v")))
                except Exception:
                    log.exception("GPX export of journey %d: trip %d", journey_id, trip["id"])
            if not punkte:
                punkte = wegpunkte.get(trip["id"]) or []
            if not punkte:
                if trip["start_lat"] and trip["start_lon"]:
                    punkte.append((trip["start_lat"], trip["start_lon"], None, None))
                if trip["end_lat"] and trip["end_lon"]:
                    punkte.append((trip["end_lat"], trip["end_lon"], None, None))
            if punkte:
                tracks.append((f'{_t["journey_gpx_track_name"].format(i + 1)} ({trip["start_time"][:10]})', punkte))
    finally:
        if client:
            client.close()

    filename = gpxexport.dateiname("journey", journey["title"], journey["date_from"][:10], journey["date_to"][:10])
    return Response(
        gpxexport.gpx_dokument(journey["title"], tracks),
        mimetype="application/gpx+xml",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@journeys_bp.route("/api/journeys/available-trips")
@login_required
def journey_available_trips():
    """Trips in date range for journey assignment — same whole-day range as
    the auto-fill (IN_RANGE).

    With ``journey`` (the edit dialog) the journey's own vehicle decides, not
    the currently active one: the auto-fill goes by ``j.device``, so a list
    built for another vehicle would offer trips that ticking could never
    link."""
    date_from = request.args.get("from", "")[:10]
    date_to = request.args.get("to", "")[:10]
    db = get_db()
    device = None
    try:
        jid = int(request.args.get("journey", ""))
    except ValueError:
        jid = None
    if jid is not None:
        row = db.execute("SELECT device FROM journeys WHERE id = ?", (jid,)).fetchone()
        if row:
            device = row["device"]
    if device is None:
        device = active_device()
    trips = db.execute(
        """SELECT id, start_time, end_time, start_address, end_address, distance_km
           FROM trips
           WHERE device = ? AND start_time >= ? AND start_time < date(?, '+1 day')
           ORDER BY start_time""",
        (device, date_from, date_to),
    ).fetchall()
    db.close()
    return jsonify([dict(t) for t in trips])

