"""Schema migrations for the IDMate SQLite database.

Replaces the inline ~280-line migration block that used to live in
``app.get_db()``. Migrations are ordered and tracked via SQLite's native
``PRAGMA user_version`` — no SQLAlchemy/Alembic dependency.

Design notes
------------
* ``schema.sql`` (run by ``get_db`` before this module) creates the *base*
  tables with ``CREATE TABLE IF NOT EXISTS``. Everything here evolves that
  baseline via ``ALTER TABLE`` / table rebuilds / one-off data fixes.
* Each migration keeps its original *idempotent guard* (``if col not in
  cols``). That makes the runner safe for the large installed base of
  databases that pre-date version tracking: such a DB reports
  ``user_version == 0`` even though every column already exists, so the runner
  re-walks all migrations — but each one is a no-op on already-applied schema
  and the version is then stamped to ``SCHEMA_VERSION`` for instant skips on
  every later startup.
* To add a migration: append a ``(version, name, fn)`` tuple with the next
  integer and bump nothing else — ``SCHEMA_VERSION`` is derived from the list.

Helpers from ``app``/``detector`` that data-migrations need are imported lazily
inside the function (the established pattern in ``detector.get_db``) to avoid a
circular import at module load, since ``app`` imports this module.
"""

from detector import sanitize_soc as _sanitize_soc


# ── small introspection helpers ──────────────────────────────────

def _cols(db, table):
    """Column names of *table* (empty list if the table does not exist)."""
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]


def _tables(db):
    return {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


# ── individual migrations (each is its own user_version step) ─────

def m001_trips_is_gpx(db, log):
    if "is_gpx" not in _cols(db, "trips"):
        db.execute("ALTER TABLE trips ADD COLUMN is_gpx INTEGER DEFAULT 0")


def m002_trips_odo(db, log):
    cols = _cols(db, "trips")
    if "odo_start" not in cols:
        db.execute("ALTER TABLE trips ADD COLUMN odo_start REAL")
        db.execute("ALTER TABLE trips ADD COLUMN odo_end REAL")


def m003_trips_country_code(db, log):
    if "country_code" not in _cols(db, "trips"):
        db.execute("ALTER TABLE trips ADD COLUMN country_code TEXT")


def m004_trips_is_manual(db, log):
    if "is_manual" not in _cols(db, "trips"):
        db.execute("ALTER TABLE trips ADD COLUMN is_manual INTEGER NOT NULL DEFAULT 0")
        log.info("Migration: trips.is_manual added")
        # Backfill country_code from start_address (format: "DE - City - Street")
        db.execute("""
            UPDATE trips SET country_code = UPPER(SUBSTR(start_address, 1, 2))
            WHERE country_code IS NULL AND start_address IS NOT NULL
              AND LENGTH(start_address) >= 2
              AND SUBSTR(start_address, 3, 3) IN (' - ', ' –', ' — ')
        """)
        log.info("Migration: trips.country_code backfilled")


def m005_trips_geocode_anchors(db, log):
    # Store the coordinates an address was geocoded for, so the background
    # geocoder can re-fetch when start/end shifts (e.g. a trip prematurely
    # closed before arrival and later extended).
    if "start_geo_lat" not in _cols(db, "trips"):
        db.execute("ALTER TABLE trips ADD COLUMN start_geo_lat REAL")
        db.execute("ALTER TABLE trips ADD COLUMN start_geo_lon REAL")
        db.execute("ALTER TABLE trips ADD COLUMN end_geo_lat REAL")
        db.execute("ALTER TABLE trips ADD COLUMN end_geo_lon REAL")
        # Anchor existing addresses to their current coordinates (no API calls):
        # history stays as-is, but any future coordinate shift now triggers a
        # re-geocode — including trips that are still open right now.
        db.execute("""UPDATE trips SET start_geo_lat = start_lat, start_geo_lon = start_lon
                      WHERE start_address IS NOT NULL AND start_lat IS NOT NULL""")
        db.execute("""UPDATE trips SET end_geo_lat = end_lat, end_geo_lon = end_lon
                      WHERE end_address IS NOT NULL AND end_lat IS NOT NULL""")
        log.info("Migration: trips geocode-anchor columns added")


def m006_recalc_consumption(db, log):
    # Recalculate consumption where SoC exists but energy/consumption is missing.
    missing = db.execute(
        """SELECT id, device, soc_start, soc_end, distance_km FROM trips
           WHERE energy_kwh IS NULL AND soc_start IS NOT NULL AND soc_end IS NOT NULL
             AND soc_start > soc_end"""
    ).fetchall()
    if not missing:
        return
    from app import get_bat_kwh  # lazy: app imports this module; only needed with data
    for t in missing:
        bat_kwh = get_bat_kwh(db, t["device"])
        energy = round((t["soc_start"] - t["soc_end"]) / 100 * bat_kwh, 2)
        cons = round(energy / t["distance_km"] * 100, 1) if t["distance_km"] and t["distance_km"] >= 10 else None
        db.execute("UPDATE trips SET energy_kwh = ?, consumption = ? WHERE id = ?",
                   (energy, cons, t["id"]))
    log.info("Consumption recalculated: %d trips", len(missing))


def m007_users_default_device(db, log):
    if "default_device" not in _cols(db, "users"):
        db.execute("ALTER TABLE users ADD COLUMN default_device TEXT")


def m008_users_active_vehicle(db, log):
    # Temporary selection, persisted in DB.
    if "active_vehicle_id" not in _cols(db, "users"):
        db.execute("ALTER TABLE users ADD COLUMN active_vehicle_id INTEGER")


def m009_users_totp(db, log):
    if "totp_secret" not in _cols(db, "users"):
        db.execute("ALTER TABLE users ADD COLUMN totp_secret TEXT")
        db.execute("ALTER TABLE users ADD COLUMN totp_enabled INTEGER DEFAULT 0")
        db.execute("ALTER TABLE users ADD COLUMN setup_required INTEGER DEFAULT 0")


def m010_users_recovery_codes(db, log):
    if "recovery_codes" not in _cols(db, "users"):
        db.execute("ALTER TABLE users ADD COLUMN recovery_codes TEXT")


def m011_users_default_trip_purpose(db, log):
    if "default_trip_purpose" not in _cols(db, "users"):
        db.execute("ALTER TABLE users ADD COLUMN default_trip_purpose TEXT")


def m012_users_avatar(db, log):
    if "avatar_filename" not in _cols(db, "users"):
        db.execute("ALTER TABLE users ADD COLUMN avatar_filename TEXT")


def m013_cs_session_number(db, log):
    if "session_number" not in _cols(db, "charge_sessions"):
        db.execute("ALTER TABLE charge_sessions ADD COLUMN session_number TEXT")
        db.execute("ALTER TABLE charge_sessions ADD COLUMN is_external INTEGER DEFAULT 0")


def m014_cs_note_cost(db, log):
    if "note" not in _cols(db, "charge_sessions"):
        db.execute("ALTER TABLE charge_sessions ADD COLUMN note TEXT")
        db.execute("ALTER TABLE charge_sessions ADD COLUMN cost_total REAL")


def m015_cs_location(db, log):
    if "lat" not in _cols(db, "charge_sessions"):
        db.execute("ALTER TABLE charge_sessions ADD COLUMN lat REAL")
        db.execute("ALTER TABLE charge_sessions ADD COLUMN lon REAL")
        db.execute("ALTER TABLE charge_sessions ADD COLUMN location_name TEXT")


def m016_purpose_meta_is_main(db, log):
    if "is_main" not in _cols(db, "purpose_meta"):
        db.execute("ALTER TABLE purpose_meta ADD COLUMN is_main INTEGER NOT NULL DEFAULT 0")


def m017_route_rules_nullable(db, log):
    # Make from/to nullable (wildcard rules).
    rr_info = db.execute("PRAGMA table_info(route_rules)").fetchall()
    rr_cols = {r[1]: r[3] for r in rr_info}  # name -> notnull
    if rr_cols.get("from_location_id") == 1:  # still NOT NULL
        db.executescript("""
            CREATE TABLE route_rules_new (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                from_location_id INTEGER REFERENCES locations(id) ON DELETE CASCADE,
                to_location_id   INTEGER REFERENCES locations(id) ON DELETE CASCADE,
                purpose          TEXT NOT NULL,
                destination      TEXT,
                visit_reason     TEXT,
                created_at       TEXT DEFAULT (datetime('now')),
                UNIQUE(from_location_id, to_location_id)
            );
            INSERT INTO route_rules_new SELECT * FROM route_rules;
            DROP TABLE route_rules;
            ALTER TABLE route_rules_new RENAME TO route_rules;
        """)
        log.info("Migration: route_rules – from/to nullable")


def m018_vehicles_device(db, log):
    if "device" not in _cols(db, "vehicles"):
        db.execute("ALTER TABLE vehicles ADD COLUMN device TEXT")
        log.info("Migration: vehicles.device added")


def m019_vehicles_model(db, log):
    if "model" not in _cols(db, "vehicles"):
        db.execute("ALTER TABLE vehicles ADD COLUMN model TEXT")


def m020_vehicles_battery(db, log):
    if "battery_capacity_kwh" not in _cols(db, "vehicles"):
        db.execute("ALTER TABLE vehicles ADD COLUMN battery_capacity_kwh REAL")
        log.info("Migration: vehicles.battery_capacity_kwh added")


def m021_users_default_vehicle(db, log):
    if "default_vehicle_id" not in _cols(db, "users"):
        db.execute("ALTER TABLE users ADD COLUMN default_vehicle_id INTEGER")
        log.info("Migration: users.default_vehicle_id added")


def m022_locations_color(db, log):
    loc_cols = _cols(db, "locations")
    if loc_cols and "color" not in loc_cols:
        db.execute("ALTER TABLE locations ADD COLUMN color TEXT DEFAULT '#21262d'")
        log.info("Migration: locations.color added")


def m023_cl_icon_filename(db, log):
    cl_cols = _cols(db, "charge_locations")
    if cl_cols and "icon_filename" not in cl_cols:
        db.execute("ALTER TABLE charge_locations ADD COLUMN icon_filename TEXT")


def m024_cl_country_code(db, log):
    cl_cols = _cols(db, "charge_locations")
    if cl_cols and "country_code" not in cl_cols:
        db.execute("ALTER TABLE charge_locations ADD COLUMN country_code TEXT DEFAULT 'DE'")


def m025_cl_color(db, log):
    cl_cols = _cols(db, "charge_locations")
    if cl_cols and "color" not in cl_cols:
        db.execute("ALTER TABLE charge_locations ADD COLUMN color TEXT DEFAULT '#8b949e'")


def m026_cl_shape(db, log):
    cl_cols = _cols(db, "charge_locations")
    if cl_cols and "shape" not in cl_cols:
        db.execute("ALTER TABLE charge_locations ADD COLUMN shape TEXT DEFAULT 'circle'")
        db.execute("ALTER TABLE charge_locations ADD COLUMN lat2 REAL")
        db.execute("ALTER TABLE charge_locations ADD COLUMN lon2 REAL")


def m027_cs_operator(db, log):
    if "operator" not in _cols(db, "charge_sessions"):
        db.execute("ALTER TABLE charge_sessions ADD COLUMN operator TEXT")


def m028_cr_soc(db, log):
    if "soc" not in _cols(db, "charge_readings"):
        db.execute("ALTER TABLE charge_readings ADD COLUMN soc REAL")


def m029_cs_soc(db, log):
    if "soc_start" not in _cols(db, "charge_sessions"):
        db.execute("ALTER TABLE charge_sessions ADD COLUMN soc_start REAL")
        db.execute("ALTER TABLE charge_sessions ADD COLUMN soc_end REAL")


def m046_cs_manual_fields(db, log):
    # CSV of fields an admin manually backfilled (odometer/soc_start/soc_end) on
    # an automatic session — preserved across rebuild_charge_sessions so the
    # correction sticks instead of reverting to NULL on the next rebuild.
    if "manual_fields" not in _cols(db, "charge_sessions"):
        db.execute("ALTER TABLE charge_sessions ADD COLUMN manual_fields TEXT")


def m047_locations_polygon(db, log):
    # Polygon geofences for normal locations too (like charge_locations):
    # 'shape' = circle|polygon, 'polygon_coords' = JSON [[lat,lon],...].
    cols = _cols(db, "locations")
    if cols and "shape" not in cols:
        db.execute("ALTER TABLE locations ADD COLUMN shape TEXT DEFAULT 'circle'")
        db.execute("UPDATE locations SET shape = 'circle' WHERE shape IS NULL")
        log.info("Migration: locations.shape added")
    if cols and "polygon_coords" not in cols:
        db.execute("ALTER TABLE locations ADD COLUMN polygon_coords TEXT")
        log.info("Migration: locations.polygon_coords added")


def m048_cl_is_home(db, log):
    # Mark a charge location as "home". Until now "home charge" was implied by
    # is_external=0, i.e. "metered by the wallbox". Charges reconstructed from
    # trips/SoC before the wallbox existed are stored as is_external=1 even
    # though they happened at home — they polluted the external totals (real
    # case: 538 kWh of unpriced home charging dragged the external average from
    # 72.9 ct/kWh down to 7.0) and were missing from every home total.
    cols = _cols(db, "charge_locations")
    if cols and "is_home" not in cols:
        db.execute("ALTER TABLE charge_locations ADD COLUMN is_home INTEGER NOT NULL DEFAULT 0")
        # Seed: every location that hosts at least one metered wallbox session
        # is a home location.
        db.execute(
            "UPDATE charge_locations SET is_home = 1 WHERE name IN ("
            "  SELECT DISTINCT location_name FROM charge_sessions"
            "   WHERE is_external = 0 AND location_name IS NOT NULL)"
        )
        n = db.execute("SELECT COUNT(*) FROM charge_locations WHERE is_home = 1").fetchone()[0]
        log.info("Migration: charge_locations.is_home added (%d home location(s) seeded)", n)


def m030_operators_table(db, log):
    # Operators with custom icon/color.
    if "operators" not in _tables(db):
        db.execute("""CREATE TABLE operators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            color TEXT DEFAULT '#8b949e',
            icon_filename TEXT
        )""")
        log.info("Migration: operators table created")


def m031_cl_operator_id(db, log):
    cl_cols = _cols(db, "charge_locations")
    if cl_cols and "operator_id" not in cl_cols:
        db.execute("ALTER TABLE charge_locations ADD COLUMN operator_id INTEGER REFERENCES operators(id) ON DELETE SET NULL")
        log.info("Migration: charge_locations.operator_id added")


def m032_user_notes(db, log):
    # Per-user notes (dashboard scratchpad).
    if "user_notes" not in _tables(db):
        db.execute("""CREATE TABLE user_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            vehicle_plate TEXT,
            content TEXT NOT NULL DEFAULT '',
            pinned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT
        )""")
        db.execute("CREATE INDEX idx_user_notes_user ON user_notes(user_id, vehicle_plate, pinned DESC, id DESC)")
        log.info("Migration: user_notes table created")
    else:
        # vehicle_plate column added later — keep legacy NULL rows visible for
        # every vehicle (backward compat).
        if "vehicle_plate" not in _cols(db, "user_notes"):
            db.execute("ALTER TABLE user_notes ADD COLUMN vehicle_plate TEXT")
            log.info("Migration: user_notes.vehicle_plate added")


def m033_soc_underflow_repair(db, log):
    # Retroactively fix the SoC-Underflow bug (firmware cast of a negative float
    # to uint16 → ~6552 % instead of ~-1 %). Idempotent: already-repaired values
    # are back in range and no longer match the >110/<0 filter.
    repaired = 0
    for table, cols in (
        ("charge_readings", ("soc",)),
        ("charge_sessions", ("soc_start", "soc_end")),
        ("trips",           ("soc_start", "soc_end")),
    ):
        existing = set(_cols(db, table))
        for col in cols:
            if col not in existing:
                continue
            rows = db.execute(
                f"SELECT rowid, {col} FROM {table} WHERE {col} IS NOT NULL "
                f"AND ({col} > 110 OR {col} < 0)"
            ).fetchall()
            for rowid, raw in rows:
                fixed = _sanitize_soc(raw)
                if fixed is None:
                    # impossible to repair → NULL rather than keep nonsense
                    db.execute(f"UPDATE {table} SET {col} = NULL WHERE rowid = ?", (rowid,))
                else:
                    db.execute(f"UPDATE {table} SET {col} = ? WHERE rowid = ?", (fixed, rowid))
                repaired += 1
    if repaired:
        log.info("Migration: SoC underflow repair (%d values corrected)", repaired)


def m034_trips_kw(db, log):
    if "kw_start" not in _cols(db, "trips"):
        db.execute("ALTER TABLE trips ADD COLUMN kw_start REAL")
        db.execute("ALTER TABLE trips ADD COLUMN kw_end REAL")
        log.info("Migration: trips.kw_start/kw_end added")


def m035_locations_icon(db, log):
    if "icon" not in _cols(db, "locations"):
        db.execute("ALTER TABLE locations ADD COLUMN icon TEXT DEFAULT 'pin'")
        log.info("Migration: locations.icon added")


def m036_locations_icon_color(db, log):
    if "icon_color" not in _cols(db, "locations"):
        db.execute("ALTER TABLE locations ADD COLUMN icon_color TEXT DEFAULT 'white'")
        db.execute("UPDATE locations SET icon_color = 'white' WHERE icon_color IS NULL")
        log.info("Migration: locations.icon_color added")


def m037_cl_polygon(db, log):
    cl_cols = _cols(db, "charge_locations")
    if cl_cols and "polygon_coords" not in cl_cols:
        db.execute("ALTER TABLE charge_locations ADD COLUMN polygon_coords TEXT")
        log.info("Migration: charge_locations.polygon_coords added")


def m038_users_map_style(db, log):
    if "map_style" not in _cols(db, "users"):
        db.execute("ALTER TABLE users ADD COLUMN map_style TEXT")
        log.info("Migration: users.map_style added")


def m039_users_theme(db, log):
    # UI theme per user: NULL/'' = dark (default), 'light', 'gt'.
    if "theme" not in _cols(db, "users"):
        db.execute("ALTER TABLE users ADD COLUMN theme TEXT")
        log.info("Migration: users.theme added")


# ── FIXES 15.1 — Trip tags: global catalog + per-user visibility ──
# Model: ONE catalog per tag type (no duplicates), a visibility junction
# decides which user sees which entry in the tagging dropdown. Semantics:
# a tag is visible to user X if (a) a row (tag, X) exists OR
# (b) the tag has NO visibility rows at all (= legacy/global, no backfill).
# Everything additive (ADD COLUMN / CREATE TABLE) — no rebuild, no data risk.

def m040_purpose_meta_created_by(db, log):
    if "created_by" not in _cols(db, "purpose_meta"):
        db.execute("ALTER TABLE purpose_meta ADD COLUMN created_by INTEGER REFERENCES users(id) ON DELETE SET NULL")
        log.info("Migration: purpose_meta.created_by added")


def m041_purpose_visibility(db, log):
    if "purpose_visibility" not in _tables(db):
        db.execute("""CREATE TABLE purpose_visibility (
            name    TEXT    NOT NULL REFERENCES purpose_meta(name) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            PRIMARY KEY (name, user_id)
        )""")
        log.info("Migration: purpose_visibility table created")


def m042_preset_values_visibility(db, log):
    if "created_by" not in _cols(db, "preset_values"):
        db.execute("ALTER TABLE preset_values ADD COLUMN created_by INTEGER REFERENCES users(id) ON DELETE SET NULL")
    if "preset_value_visibility" not in _tables(db):
        db.execute("""CREATE TABLE preset_value_visibility (
            value_id INTEGER NOT NULL REFERENCES preset_values(id) ON DELETE CASCADE,
            user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            PRIMARY KEY (value_id, user_id)
        )""")
        log.info("Migration: preset_value_visibility table created")


def m043_route_rules_visibility(db, log):
    if "created_by" not in _cols(db, "route_rules"):
        db.execute("ALTER TABLE route_rules ADD COLUMN created_by INTEGER REFERENCES users(id) ON DELETE SET NULL")
    if "route_rule_visibility" not in _tables(db):
        db.execute("""CREATE TABLE route_rule_visibility (
            rule_id INTEGER NOT NULL REFERENCES route_rules(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            PRIMARY KEY (rule_id, user_id)
        )""")
        log.info("Migration: route_rule_visibility table created")


def m044_users_language(db, log):
    # Display language per user (FIXES 15.5): NULL = global settings fallback.
    if "language" not in _cols(db, "users"):
        db.execute("ALTER TABLE users ADD COLUMN language TEXT")
        log.info("Migration: users.language added")


def m045_user_vehicle_access(db, log):
    # Per-user vehicle visibility: which vehicles a user is allowed to see.
    # Semantics like the tags: NO row for a user = all vehicles
    # (default/backward compat); rows present = only those. Admins always all.
    if "user_vehicle_access" not in _tables(db):
        db.execute("""CREATE TABLE user_vehicle_access (
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, vehicle_id)
        )""")
        log.info("Migration: user_vehicle_access table created")


# ── ordered migration registry ───────────────────────────────────
# (version, name, fn). Append new entries with the next integer.

def m049_locations_defaults(db, log):
    # A location carries its own trip defaults for trips ENDING there: the
    # purpose and the visited party ("Besucht"), next to the existing default
    # visit reason. Route rules (from→to) keep precedence; these are the
    # fallback the detector applies when no rule matches.
    cols = _cols(db, "locations")
    if cols and "default_purpose" not in cols:
        db.execute("ALTER TABLE locations ADD COLUMN default_purpose TEXT")
        log.info("Migration: locations.default_purpose added")
    if cols and "default_destination" not in cols:
        db.execute("ALTER TABLE locations ADD COLUMN default_destination TEXT")
        log.info("Migration: locations.default_destination added")


def m050_locations_alias_of(db, log):
    # Several geofences per location: an "alias" row carries its own circle/
    # polygon but points at a parent via alias_of. Matching, route rules, trip
    # defaults and the trip address all resolve to the parent; the alias only
    # widens WHERE the parent is recognised (second entrance, parking lot,
    # another site of the same customer).
    cols = _cols(db, "locations")
    if cols and "alias_of" not in cols:
        db.execute("ALTER TABLE locations ADD COLUMN alias_of INTEGER")
        log.info("Migration: locations.alias_of added")


def m051_locations_keep_address(db, log):
    # A geofence that only TAGS trips (destination / purpose / reason) and
    # leaves the street address in the logbook untouched — the "visited
    # company" is tracked, the address stays what the geocoder found.
    # 0 (default) keeps the classic behaviour: the location name becomes
    # the trip address.
    cols = _cols(db, "locations")
    if cols and "keep_address" not in cols:
        db.execute("ALTER TABLE locations ADD COLUMN keep_address INTEGER NOT NULL DEFAULT 0")
        log.info("Migration: locations.keep_address added")


def m052_route_rules_to_location_defaults(db, log):
    # Absorb the route rules that the per-geofence defaults express exactly:
    #   (any → X : P/D/R)  →  X.default_purpose/destination/reason (fill empty)
    #   (X → any : P)      →  covered by X.default_purpose applying to
    #                          departures too (detector start fallback)
    # A rule is deleted only when the location carries every value the rule
    # set — otherwise (conflicting defaults, from+to rules) it stays and keeps
    # its precedence. Idempotent; a DB without rules is a no-op.
    cols = _cols(db, "route_rules")
    if not cols or "default_purpose" not in _cols(db, "locations"):
        return
    absorbed = 0
    for r in db.execute("SELECT * FROM route_rules WHERE to_location_id IS NOT NULL "
                        "AND from_location_id IS NULL").fetchall():
        loc = db.execute("SELECT * FROM locations WHERE id = ?", (r["to_location_id"],)).fetchone()
        if not loc:
            continue
        wanted = {"default_purpose": (r["purpose"] or "").strip(),
                  "default_destination": (r["destination"] or "").strip(),
                  "default_reason": (r["visit_reason"] or "").strip()}
        for col, val in wanted.items():
            if val and not (loc[col] or "").strip():
                db.execute(f"UPDATE locations SET {col} = ? WHERE id = ?", (val, loc["id"]))
        loc = db.execute("SELECT * FROM locations WHERE id = ?", (loc["id"],)).fetchone()
        if all(not val or (loc[col] or "").strip() == val for col, val in wanted.items()):
            db.execute("DELETE FROM route_rules WHERE id = ?", (r["id"],))
            absorbed += 1
    for r in db.execute("SELECT * FROM route_rules WHERE from_location_id IS NOT NULL "
                        "AND to_location_id IS NULL").fetchall():
        loc = db.execute("SELECT default_purpose FROM locations WHERE id = ?",
                         (r["from_location_id"],)).fetchone()
        if (loc and not (r["destination"] or "").strip() and not (r["visit_reason"] or "").strip()
                and (loc["default_purpose"] or "").strip() == (r["purpose"] or "").strip()):
            db.execute("DELETE FROM route_rules WHERE id = ?", (r["id"],))
            absorbed += 1
    if absorbed:
        log.info("Migration: %d route rules absorbed into location defaults", absorbed)


def m053_locations_tag_dirs(db, log):
    # Every tag on a geofence (purpose / destination / reason) applies on
    # ARRIVAL (bit 1) and/or DEPARTURE (bit 2): leaving the office for an
    # untagged place is still office business, arriving home means nothing.
    # Existing purposes came from mirrored route rules (any->X and X->any),
    # i.e. both directions; destination and reason stay arrival-only.
    cols = _cols(db, "locations")
    if not cols:
        return
    added = False
    for col in ("purpose_dirs", "destination_dirs", "reason_dirs"):
        if col not in cols:
            db.execute(f"ALTER TABLE locations ADD COLUMN {col} INTEGER NOT NULL DEFAULT 1")
            added = True
    if added:
        db.execute("UPDATE locations SET purpose_dirs = 3 "
                   "WHERE default_purpose IS NOT NULL AND TRIM(default_purpose) <> ''")
        log.info("Migration: locations.*_dirs added (existing purposes: arrival + departure)")


def m054_journey_trip_excluded(db, log):
    # Journeys now take in every trip of their date range automatically — also
    # trips detected after the journey was created. A trip removed by hand is
    # remembered here so the auto-fill never brings it back.
    if "journey_trip_excluded" in _tables(db):
        return
    db.execute("""CREATE TABLE journey_trip_excluded (
        journey_id INTEGER NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
        trip_id    INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
        PRIMARY KEY (journey_id, trip_id)
    )""")
    # Existing journeys: a trip in range that is not linked although it already
    # existed when the journey was created was offered in the checklist and
    # unticked -> keep it out. Trips detected later were never offered; they
    # join through the auto-fill. (Both created_at are SQLite UTC defaults.)
    n = db.execute("""
        INSERT INTO journey_trip_excluded (journey_id, trip_id)
        SELECT j.id, t.id FROM journeys j
          JOIN trips t ON t.device = j.device
                      AND t.start_time >= substr(j.date_from, 1, 10)
                      AND t.start_time < date(substr(j.date_to, 1, 10), '+1 day')
         WHERE NOT EXISTS (SELECT 1 FROM journey_trips jt
                            WHERE jt.journey_id = j.id AND jt.trip_id = t.id)
           AND (t.created_at IS NULL OR j.created_at IS NULL OR t.created_at <= j.created_at)
    """).rowcount
    log.info("Migration: journey_trip_excluded created (%d unticked trips kept out)", n)


def m055_journey_orphan_device(db, log):
    # Journeys carry the InfluxDB tag of the vehicle that was active when they
    # were created — the guess from config.INFLUX_DEVICE when that vehicle had
    # no tag yet. Such a journey matches no vehicle and no trip afterwards: it
    # vanishes from the journeys list and never picks a trip up automatically.
    # Repair the unambiguous, lossless cases (see journey_sync).
    from journey_sync import repair_orphan_journey_devices
    n = repair_orphan_journey_devices(db)
    log.info("Migration: %d orphaned journey(s) re-pointed to their vehicle", n)


def m056_vehicles_kartenauto(db, log):
    # Das Auto auf der Karte: Karosserie (limousine/kombi/suv, NULL = blauer
    # Punkt wie bisher) und Wagenfarbe (#rrggbb) je Fahrzeug.
    cols = _cols(db, "vehicles")
    if "body_style" not in cols:
        db.execute("ALTER TABLE vehicles ADD COLUMN body_style TEXT")
    if "color" not in cols:
        db.execute("ALTER TABLE vehicles ADD COLUMN color TEXT")
    log.info("Migration: vehicles.body_style/color added")


def m057_journey_shares(db, log):
    # Live-Reise fuer Gaeste: je Gast ein Link (key) und ein vierstelliger
    # Code. Pro Code ist ein Geraet angemeldet (device_token, Cookie); wer den
    # Code auf einem zweiten Geraet eingibt, loest das erste ab. delay_min
    # verschiebt alles, was der Gast sieht, um so viele Minuten in die
    # Vergangenheit. last_seen_at zeigt dem Besitzer, wann der Gast zuletzt
    # zugeschaut hat.
    if "journey_shares" in _tables(db):
        return
    db.execute("""CREATE TABLE journey_shares (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        journey_id      INTEGER NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
        name            TEXT NOT NULL,
        key             TEXT NOT NULL UNIQUE,
        code            TEXT NOT NULL,
        delay_min       INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        device_token    TEXT,
        device_since    TEXT,
        last_seen_at    TEXT,
        views           INTEGER NOT NULL DEFAULT 0,
        failed_attempts INTEGER NOT NULL DEFAULT 0,
        locked_until    TEXT
    )""")
    log.info("Migration: journey_shares created")


def m058_journeys_share_title(db, log):
    # Gastname der Reise: was Gaeste der Live-Reise als Titel sehen (Gastseite
    # und Einladungstext). NULL = der Reisetitel. Der Reisetitel selbst darf
    # so ein Sortierpraefix wie "2026 09" tragen, ohne dass Gaeste es sehen.
    if "share_title" not in _cols(db, "journeys"):
        db.execute("ALTER TABLE journeys ADD COLUMN share_title TEXT")
    log.info("Migration: journeys.share_title added")


def m059_journey_share_devices(db, log):
    # Familienlink: ein Zugang fuer mehrere Geraete (max_devices), jedes Geraet
    # mit dem Namen, den der Gast eintippt, System/Browser aus dem User-Agent
    # und eigenem Cookie-Token. Der Besitzer sieht die Liste und kann einzelne
    # Geraete blockieren. Das eine Geraet bisheriger Zugaenge zieht mit um.
    if "max_devices" not in _cols(db, "journey_shares"):
        db.execute("ALTER TABLE journey_shares ADD COLUMN max_devices INTEGER NOT NULL DEFAULT 1")
    if "journey_share_devices" in _tables(db):
        return
    db.execute("""CREATE TABLE journey_share_devices (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        share_id   INTEGER NOT NULL REFERENCES journey_shares(id) ON DELETE CASCADE,
        token      TEXT NOT NULL UNIQUE,
        name       TEXT NOT NULL DEFAULT '',
        os         TEXT,
        browser    TEXT,
        model      TEXT,
        first_seen TEXT NOT NULL DEFAULT (datetime('now')),
        last_seen  TEXT,
        blocked    INTEGER NOT NULL DEFAULT 0
    )""")
    n = 0
    for r in db.execute("SELECT id, device_token, device_since, last_seen_at FROM journey_shares "
                        "WHERE device_token IS NOT NULL AND device_token != ''").fetchall():
        db.execute("INSERT INTO journey_share_devices (share_id, token, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                   (r[0], r[1], r[2] or r[3] or "1970-01-01T00:00:00Z", r[3]))
        n += 1
    log.info("Migration: journey_share_devices created (%d device(s) carried over)", n)


def m060_charge_session_bridges(db, log):
    # Zusammengefuehrte Ladungen: Eine Bruecke sagt dem Neuaufbau, dass er die
    # Ablesungen eines Fahrzeugs zwischen from_ts und to_ts nicht trennt (auch
    # nicht bei einer Luecke ueber 12 h). label ist die Nummer der vereinten
    # Ladung ("246-249"), numbers die alten Nummern (JSON) fuers Aufheben.
    if "charge_session_bridges" in _tables(db):
        return
    db.execute("""CREATE TABLE charge_session_bridges (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        vehicle_plate TEXT NOT NULL,
        from_ts       TEXT NOT NULL,
        to_ts         TEXT NOT NULL,
        label         TEXT NOT NULL,
        numbers       TEXT NOT NULL DEFAULT '[]',
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    log.info("Migration: charge_session_bridges created")


def m061_trip_manual_fields(db, log):
    # Fahrten, deren Start oder Ende im Luecken-Editor korrigiert wurde
    # (Telemetrie-Ausfall): CSV aus "start"/"end". Die Fahrterkennung
    # ueberschreibt diese Seite dann nicht mehr, die Fahrtenliste markiert
    # sie mit einem roten Sternchen.
    if "manual_fields" not in _cols(db, "trips"):
        db.execute("ALTER TABLE trips ADD COLUMN manual_fields TEXT")
        log.info("Migration: trips.manual_fields added")


def m062_passkeys(db, log):
    # Passkeys (WebAuthn): Anmeldung ohne Passwort, Benutzerpruefung (Face ID,
    # PIN) Pflicht – zaehlt damit als beide Faktoren. webauthn_handle ist die
    # zufaellige, stabile Benutzerkennung, die der Passkey mit sich traegt.
    if "webauthn_handle" not in _cols(db, "users"):
        db.execute("ALTER TABLE users ADD COLUMN webauthn_handle TEXT")
    if "passkeys" not in _tables(db):
        db.execute("""CREATE TABLE passkeys (
            id          TEXT PRIMARY KEY,          -- credential id (base64url)
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            rp_id       TEXT NOT NULL,             -- host the passkey belongs to
            public_key  BLOB NOT NULL,
            sign_count  INTEGER NOT NULL DEFAULT 0,
            transports  TEXT,
            label       TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            last_used   TEXT
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_passkeys_user ON passkeys(user_id)")
    log.info("Migration: passkeys ready")


def m063_restore_tm_markers(db, log):
    # The charge recalc nulled session_number on every external charge, also
    # the "TM-<id>" marker of TeslaMate imports. Without it their energy
    # (already battery side) was multiplied by the charging efficiency again.
    # The import note still names them.
    n = db.execute(
        "UPDATE charge_sessions SET session_number = 'TM-r' || id "
        "WHERE is_external = 1 AND session_number IS NULL AND note LIKE 'TeslaMate import%'"
    ).rowcount
    if n:
        log.info("Migration: TeslaMate marker restored on %d charges", n)


def m064_repair_extended_trip_energy(db, log):
    # A trip extended by a later scan kept only the energy of the newly
    # scanned piece (82 km with 0.33 kWh). Recompute the energy of detected
    # trips whose stored value is off by more than 25 % from SoC span ×
    # capacity at the time; hand-filled trips stay as they are.
    rows = db.execute(
        """SELECT id, device, start_time, soc_start, soc_end, distance_km, energy_kwh
           FROM trips WHERE COALESCE(is_manual, 0) = 0 AND COALESCE(is_gpx, 0) = 0
             AND soc_start IS NOT NULL AND soc_end IS NOT NULL
             AND soc_start - soc_end >= 1 AND energy_kwh IS NOT NULL"""
    ).fetchall()
    if not rows:
        return
    from app import get_bat_kwh  # lazy: app imports this module
    fixed = 0
    for t in rows:
        cap = get_bat_kwh(db, t["device"], t["start_time"])
        if not cap:
            continue
        energy = round((t["soc_start"] - t["soc_end"]) / 100 * cap, 2)
        if abs(t["energy_kwh"] - energy) <= 0.25 * energy:
            continue
        km = t["distance_km"]
        cons = round(energy / km * 100, 1) if km and km >= 10 else None
        db.execute("UPDATE trips SET energy_kwh = ?, consumption = ? WHERE id = ?",
                   (energy, cons, t["id"]))
        fixed += 1
    if fixed:
        log.info("Migration: energy of %d extended trips recomputed", fixed)


def m065_trip_times_local(db, log):
    # A few trips (March 2026) were stored as UTC with "+00:00" while every
    # other time is local without offset: they sorted wrong and showed 1-2 h
    # off. Store them as local time like the rest.
    from datetime import datetime
    from detector import LOCAL_TZ
    rows = db.execute(
        "SELECT id, device, start_time, end_time FROM trips WHERE start_time LIKE '%+%' "
        "OR start_time LIKE '%Z' OR end_time LIKE '%+%' OR end_time LIKE '%Z'").fetchall()

    def local(v):
        if not v or not ("+" in v[10:] or v.endswith("Z")):
            return v
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return v
        return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%dT%H:%M:%S")
    shifted = {}   # old UTC minute → new local minute (same device)
    for r in rows:
        for v in (r["start_time"], r["end_time"]):
            if v and local(v) != v:
                shifted[(r["device"], v[:16])] = local(v)[:16]
        db.execute("UPDATE trips SET start_time = ?, end_time = ? WHERE id = ?",
                   (local(r["start_time"]), local(r["end_time"]), r["id"]))
    # Trips filled in by hand around them took their bounds from these UTC
    # values (to the minute): move those bounds along.
    moved = 0
    if shifted:
        for m in db.execute("SELECT id, device, start_time, end_time FROM trips WHERE is_manual = 1"):
            st = shifted.get((m["device"], (m["start_time"] or "")[:16]))
            en = shifted.get((m["device"], (m["end_time"] or "")[:16]))
            if st or en:
                db.execute("UPDATE trips SET start_time = ?, end_time = ? WHERE id = ?",
                           (st + m["start_time"][16:] if st else m["start_time"],
                            en + m["end_time"][16:] if en else m["end_time"], m["id"]))
                moved += 1
    if rows:
        log.info("Migration: %d trip times converted from UTC to local, %d filled-in trips moved along",
                 len(rows), moved)


def m066_vehicle_batteries(db, log):
    # Vehicle model: a vehicle can get a new battery (capacity is fitted per
    # battery, never across a swap) and has its own charging efficiency
    # (NULL = the global setting).
    if "vehicle_batteries" not in _tables(db):
        db.execute("""CREATE TABLE vehicle_batteries (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id   INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
            installed_at TEXT NOT NULL,              -- date the new battery went in
            nominal_kwh  REAL,                       -- net capacity of the new battery
            note         TEXT,
            created_at   TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_vehicle_batteries ON vehicle_batteries(vehicle_id, installed_at)")
    if "charge_efficiency" not in _cols(db, "vehicles"):
        db.execute("ALTER TABLE vehicles ADD COLUMN charge_efficiency REAL")
    log.info("Migration: vehicle batteries and per-vehicle efficiency ready")


def m067_vehicle_efficiency_dc(db, log):
    # DC charging has its own efficiency (receipt kWh → battery); NULL = the
    # AC value. Measured against the BMS: ID.7 AC ~0.95, DC ~0.97.
    if "charge_efficiency_dc" not in _cols(db, "vehicles"):
        db.execute("ALTER TABLE vehicles ADD COLUMN charge_efficiency_dc REAL")


def m068_efficiency_automatic(db, log):
    # The per-vehicle efficiency could only be set with "Übernehmen" of a
    # measurement (1.2.205-1.2.207). Since 1.2.208 it is measured with the
    # energy counter automatically; a stored value would freeze it and show
    # as "fixed". Back to automatic.
    n = db.execute("UPDATE vehicles SET charge_efficiency = NULL, charge_efficiency_dc = NULL "
                   "WHERE charge_efficiency IS NOT NULL OR charge_efficiency_dc IS NOT NULL").rowcount
    if n:
        log.info("Migration: efficiency of %d vehicle(s) back to automatic", n)



def m069_dst_session_meters(db, log):
    # Sessions through the double hour when the clocks go back took their
    # first meter from the second pass (readings sorted by local text time):
    # +x kWh overcount and -x kWh gap, print blocked. Meter bounds again in
    # true time order; billed sessions stay as they are.
    import fahrzeugmodell as fm
    row = db.execute("SELECT value FROM settings WHERE key = 'charge_billed_until'").fetchone()
    billed = (str(row["value"])[:10] + "T99") if row and row["value"] else ""
    rows = db.execute(
        "SELECT cr.id, cr.session_id, cr.timestamp, cr.created_at, cr.meter_start, cr.meter_end "
        "FROM charge_readings cr JOIN charge_sessions cs ON cs.id = cr.session_id "
        "WHERE cs.is_external = 0 AND cs.start_time > ? AND cr.session_id IN ("
        "  SELECT session_id FROM charge_readings WHERE substr(timestamp, 6, 2) = '10' "
        "  AND substr(timestamp, 12, 2) = '02' AND session_id IS NOT NULL)", (billed,)).fetchall()
    n = 0
    for sid, (a, b) in fm.session_meters(rows).items():
        n += db.execute("UPDATE charge_sessions SET meter_start = ?, meter_end = ? WHERE id = ? "
                        "AND (meter_start IS NOT ? OR meter_end IS NOT ?)", (a, b, sid, a, b)).rowcount
    if n:
        log.info("Migration: meter bounds of %d charge session(s) through the clock change fixed", n)



def m070_consumption_min_km(db, log):
    # One rule since 1.2.223: a trip's consumption from 10 km on (below the
    # 1 % SoC steps dominate). Merge, split and import stored it from 0-1 km.
    import fahrzeugmodell as fm
    n = db.execute("UPDATE trips SET consumption = NULL WHERE consumption IS NOT NULL "
                   "AND (distance_km IS NULL OR distance_km < ?)", (fm.MIN_CONS_KM,)).rowcount
    if n:
        log.info("Migration: consumption of %d trip(s) under %d km cleared", n, fm.MIN_CONS_KM)



def m071_operator_names(db, log):
    # Receipt OCR stored the operator line as read: "Tr: SLA", "oogle",
    # "Google & L", "eon". Now: the operator code of the EVSE ID in the note
    # first, else the read name brought to one brand name, garbage → NULL.
    import beleg
    rows = db.execute("SELECT id, operator, note FROM charge_sessions WHERE is_external = 1 "
                      "AND (operator IS NOT NULL AND operator != '' OR note LIKE '%*%*%')").fetchall()
    n = 0
    for r in rows:
        name = beleg.betreiber_aus_evse(r["note"]) or beleg.betreiber_name(r["operator"])
        if name != (r["operator"] or None):
            db.execute("UPDATE charge_sessions SET operator = ? WHERE id = ?", (name, r["id"]))
            n += 1
    if n:
        log.info("Migration: operator of %d external charge(s) cleaned up", n)



def m072_diag_ack(db, log):
    # Findings of the diagnostics the user looked at and keeps as they are
    # (e.g. telemetry dropouts in the first weeks): left out of the checks
    # and of the data continuity.
    if "diag_ack" not in _tables(db):
        db.execute("""CREATE TABLE diag_ack (
            check_key  TEXT NOT NULL,
            ref_id     INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (check_key, ref_id)
        )""")
        log.info("Migration: diag_ack ready")


MIGRATIONS = [
    (1,  "trips_is_gpx",            m001_trips_is_gpx),
    (2,  "trips_odo",              m002_trips_odo),
    (3,  "trips_country_code",     m003_trips_country_code),
    (4,  "trips_is_manual",        m004_trips_is_manual),
    (5,  "trips_geocode_anchors",  m005_trips_geocode_anchors),
    (6,  "recalc_consumption",     m006_recalc_consumption),
    (7,  "users_default_device",   m007_users_default_device),
    (8,  "users_active_vehicle",   m008_users_active_vehicle),
    (9,  "users_totp",             m009_users_totp),
    (10, "users_recovery_codes",   m010_users_recovery_codes),
    (11, "users_default_purpose",  m011_users_default_trip_purpose),
    (12, "users_avatar",           m012_users_avatar),
    (13, "cs_session_number",      m013_cs_session_number),
    (14, "cs_note_cost",           m014_cs_note_cost),
    (15, "cs_location",            m015_cs_location),
    (16, "purpose_meta_is_main",   m016_purpose_meta_is_main),
    (17, "route_rules_nullable",   m017_route_rules_nullable),
    (18, "vehicles_device",        m018_vehicles_device),
    (19, "vehicles_model",         m019_vehicles_model),
    (20, "vehicles_battery",       m020_vehicles_battery),
    (21, "users_default_vehicle",  m021_users_default_vehicle),
    (22, "locations_color",        m022_locations_color),
    (23, "cl_icon_filename",       m023_cl_icon_filename),
    (24, "cl_country_code",        m024_cl_country_code),
    (25, "cl_color",               m025_cl_color),
    (26, "cl_shape",               m026_cl_shape),
    (27, "cs_operator",            m027_cs_operator),
    (28, "cr_soc",                 m028_cr_soc),
    (29, "cs_soc",                 m029_cs_soc),
    (30, "operators_table",        m030_operators_table),
    (31, "cl_operator_id",         m031_cl_operator_id),
    (32, "user_notes",             m032_user_notes),
    (33, "soc_underflow_repair",   m033_soc_underflow_repair),
    (34, "trips_kw",               m034_trips_kw),
    (35, "locations_icon",         m035_locations_icon),
    (36, "locations_icon_color",   m036_locations_icon_color),
    (37, "cl_polygon",             m037_cl_polygon),
    (38, "users_map_style",        m038_users_map_style),
    (39, "users_theme",            m039_users_theme),
    (40, "purpose_meta_created_by", m040_purpose_meta_created_by),
    (41, "purpose_visibility",     m041_purpose_visibility),
    (42, "preset_values_vis",      m042_preset_values_visibility),
    (43, "route_rules_vis",        m043_route_rules_visibility),
    (44, "users_language",         m044_users_language),
    (45, "user_vehicle_access",    m045_user_vehicle_access),
    (46, "cs_manual_fields",       m046_cs_manual_fields),
    (47, "locations_polygon",      m047_locations_polygon),
    (48, "cl_is_home",             m048_cl_is_home),
    (49, "locations_defaults",     m049_locations_defaults),
    (50, "locations_alias_of",     m050_locations_alias_of),
    (51, "locations_keep_address", m051_locations_keep_address),
    (52, "rules_to_loc_defaults",  m052_route_rules_to_location_defaults),
    (53, "locations_tag_dirs",     m053_locations_tag_dirs),
    (54, "journey_trip_excluded",  m054_journey_trip_excluded),
    (55, "journey_orphan_device",  m055_journey_orphan_device),
    (56, "vehicles_kartenauto",    m056_vehicles_kartenauto),
    (57, "journey_shares",         m057_journey_shares),
    (58, "journeys_share_title",   m058_journeys_share_title),
    (59, "journey_share_devices",  m059_journey_share_devices),
    (60, "cs_bridges",             m060_charge_session_bridges),
    (61, "trip_manual_fields",     m061_trip_manual_fields),
    (62, "passkeys",               m062_passkeys),
    (63, "restore_tm_markers",     m063_restore_tm_markers),
    (64, "repair_trip_energy",     m064_repair_extended_trip_energy),
    (65, "trip_times_local",       m065_trip_times_local),
    (66, "vehicle_batteries",      m066_vehicle_batteries),
    (67, "vehicle_efficiency_dc",  m067_vehicle_efficiency_dc),
    (68, "efficiency_automatic",   m068_efficiency_automatic),
    (69, "dst_session_meters",     m069_dst_session_meters),
    (70, "consumption_min_km",     m070_consumption_min_km),
    (71, "operator_names",         m071_operator_names),
    (72, "diag_ack",               m072_diag_ack),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


def run_migrations(db, log):
    """Apply every migration whose version exceeds the DB's ``user_version``.

    Safe for fresh DBs (base tables from schema.sql, all migrations run) and for
    legacy unversioned DBs (``user_version`` 0 with columns already present —
    each guarded migration is a no-op, then the version is stamped to head).
    Each migration commits individually so a failure leaves the DB at the last
    fully-applied version rather than half-migrated.
    """
    current = db.execute("PRAGMA user_version").fetchone()[0]
    if current >= SCHEMA_VERSION:
        return
    for version, name, fn in MIGRATIONS:
        if version <= current:
            continue
        fn(db, log)
        # PRAGMA does not accept bound params; version is a trusted int literal.
        db.execute(f"PRAGMA user_version = {version}")
        db.commit()
