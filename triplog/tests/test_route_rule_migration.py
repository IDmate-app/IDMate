"""Route rules → per-geofence defaults: the m052 migration absorbs every rule
the new model expresses exactly, and the detector applies a place's purpose
to departures too (which is what the mirrored 'X → any' rules did)."""

import logging

import detector
import migrations


LAT, LON = 52.5200, 13.4050
HOME = (52.80, 13.37)


def _loc(db, name, lat=LAT, lon=LON, **kw):
    cols = {"lat": lat, "lon": lon, "radius_m": 200, **kw}
    db.execute(f"INSERT INTO locations (name, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
               (name, *cols.values()))
    db.commit()
    return db.execute("SELECT id FROM locations WHERE name = ?", (name,)).fetchone()["id"]


def _rule(db, from_id, to_id, purpose, destination=None, reason=None):
    db.execute("INSERT INTO route_rules (from_location_id, to_location_id, purpose, destination, visit_reason) "
               "VALUES (?, ?, ?, ?, ?)", (from_id, to_id, purpose, destination, reason))
    db.commit()


def _trip(start, end):
    return {"device": "dev1", "start_time": "2024-06-01T08:00:00", "end_time": "2024-06-01T08:30:00",
            "start_lat": start[0], "start_lon": start[1], "end_lat": end[0], "end_lon": end[1],
            "odo_start": 1000.0, "odo_end": 1020.0, "distance_km": 20.0,
            "soc_start": 80.0, "soc_end": 75.0, "energy_kwh": 4.0, "consumption": 20.0,
            "kw_start": None, "kw_end": None}


def test_migration_absorbs_mirrored_wildcard_rules(app_module, db):
    office = _loc(db, "Büro Nord")
    priv = _loc(db, "Gartenhaus", lat=LAT + 0.02)
    _rule(db, None, office, "Büro")
    _rule(db, office, None, "Büro")
    _rule(db, None, priv, "Privatfahrt")
    _rule(db, priv, None, "Privatfahrt")
    migrations.m052_route_rules_to_location_defaults(db, logging.getLogger("t"))
    db.commit()
    assert db.execute("SELECT COUNT(*) AS n FROM route_rules").fetchone()["n"] == 0
    assert db.execute("SELECT default_purpose FROM locations WHERE id = ?", (office,)).fetchone()["default_purpose"] == "Büro"
    assert db.execute("SELECT default_purpose FROM locations WHERE id = ?", (priv,)).fetchone()["default_purpose"] == "Privatfahrt"


def test_migration_keeps_rules_it_cannot_express(app_module, db):
    office = _loc(db, "Büro", default_purpose="Bestand")       # conflicting existing default
    other = _loc(db, "Kunde A", lat=LAT + 0.02)
    _rule(db, None, office, "Büro")                          # conflicts → stays
    _rule(db, other, office, "Dienst")                          # exact from+to → stays
    _rule(db, office, None, "Sonstiges")                        # no matching default → stays
    migrations.m052_route_rules_to_location_defaults(db, logging.getLogger("t"))
    db.commit()
    assert db.execute("SELECT COUNT(*) AS n FROM route_rules").fetchone()["n"] == 3
    assert db.execute("SELECT default_purpose FROM locations WHERE id = ?", (office,)).fetchone()["default_purpose"] == "Bestand"


def test_migration_is_idempotent(app_module, db):
    office = _loc(db, "Büro")
    _rule(db, None, office, "Büro", destination="Firma", reason="Arbeit")
    log = logging.getLogger("t")
    migrations.m052_route_rules_to_location_defaults(db, log); db.commit()
    migrations.m052_route_rules_to_location_defaults(db, log); db.commit()
    loc = db.execute("SELECT * FROM locations WHERE id = ?", (office,)).fetchone()
    assert (loc["default_purpose"], loc["default_destination"], loc["default_reason"]) == ("Büro", "Firma", "Arbeit")
    assert db.execute("SELECT COUNT(*) AS n FROM route_rules").fetchone()["n"] == 0


def test_migration_m053_makes_existing_purposes_apply_both_ways(app_module, db):
    import logging
    _loc(db, "Büro Nord", default_purpose="Büro")
    _loc(db, "Kunde", lat=LAT + 0.02)
    db.execute("UPDATE locations SET purpose_dirs = 1")
    db.commit()
    # simulate a pre-m053 database: drop the marker the migration checks for
    cols = {r[1] for r in db.execute("PRAGMA table_info(locations)")}
    assert "purpose_dirs" in cols
    # re-run on an already migrated DB: must not touch anything
    migrations.m053_locations_tag_dirs(db, logging.getLogger("t")); db.commit()
    assert db.execute("SELECT purpose_dirs FROM locations WHERE name = 'Büro Nord'").fetchone()[0] == 1


def test_departure_from_a_tagged_place_inherits_its_purpose(app_module, db):
    _loc(db, "Büro Nord", default_purpose="Büro", purpose_dirs=3)
    detector.save_trips(db, [_trip((LAT, LON), HOME)])       # office → untagged home
    assert db.execute("SELECT purpose FROM trips").fetchone()["purpose"] == "Büro"


def test_destination_purpose_beats_departure_purpose(app_module, db):
    _loc(db, "Gartenhaus", default_purpose="Privatfahrt", purpose_dirs=3)
    _loc(db, "Büro Nord", lat=HOME[0], lon=HOME[1], default_purpose="Büro", purpose_dirs=3)
    detector.save_trips(db, [_trip((LAT, LON), HOME)])       # private place → office
    t = db.execute("SELECT purpose, destination FROM trips").fetchone()
    assert (t["purpose"], t["destination"]) == ("Büro", "Büro Nord")
