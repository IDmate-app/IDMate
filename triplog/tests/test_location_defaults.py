"""Location trip defaults: a location can carry the purpose / visited party /
reason that trips ENDING there get automatically. Route rules keep precedence;
the defaults are the detector's fallback. Saving a location backfills trips
at that location that still have no purpose (never overwriting user tags)."""

import pytest

import detector


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin) "
               "VALUES (1, 'admin', 'x', 1)")
    db.commit()
    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


LAT, LON = 52.5200, 13.4050


def _location(db, name, **kw):
    cols = {"lat": LAT, "lon": LON, "radius_m": 200, **kw}
    keys = ", ".join(cols)
    db.execute(f"INSERT INTO locations (name, {keys}) VALUES (?, {', '.join('?' * len(cols))})",
               (name, *cols.values()))
    db.commit()
    return db.execute("SELECT id FROM locations WHERE name = ?", (name,)).fetchone()["id"]


def _trip(start_iso, end_iso, end_lat=LAT, end_lon=LON):
    return {"device": "dev1", "start_time": start_iso, "end_time": end_iso,
            "start_lat": 52.40, "start_lon": 13.30, "end_lat": end_lat, "end_lon": end_lon,
            "odo_start": 1000.0, "odo_end": 1020.0, "distance_km": 20.0,
            "soc_start": 80.0, "soc_end": 75.0, "energy_kwh": 4.0, "consumption": 20.0,
            "kw_start": None, "kw_end": None}


def test_migration_adds_default_columns(db):
    cols = {r[1] for r in db.execute("PRAGMA table_info(locations)").fetchall()}
    assert {"default_purpose", "default_destination", "default_reason"} <= cols


def test_detector_applies_location_defaults(app_module, db):
    _location(db, "Müller Werk 2", default_purpose="Kunde",
              default_destination="Firma Müller GmbH", default_reason="Wartung")
    detector.save_trips(db, [_trip("2024-06-01T08:00:00", "2024-06-01T08:30:00")])
    t = db.execute("SELECT purpose, destination, visit_reason FROM trips").fetchone()
    assert (t["purpose"], t["destination"], t["visit_reason"]) == ("Kunde", "Firma Müller GmbH", "Wartung")


def test_visited_falls_back_to_location_name(app_module, db):
    _location(db, "Zuhause", default_purpose="Privat")
    detector.save_trips(db, [_trip("2024-06-01T08:00:00", "2024-06-01T08:30:00")])
    t = db.execute("SELECT purpose, destination FROM trips").fetchone()
    assert (t["purpose"], t["destination"]) == ("Privat", "Zuhause")


def test_route_rule_beats_location_defaults(app_module, db):
    loc_id = _location(db, "Müller Werk 2", default_purpose="Kunde")
    db.execute("INSERT INTO route_rules (from_location_id, to_location_id, purpose, destination) "
               "VALUES (NULL, ?, 'Dienstreise', 'Messe')", (loc_id,))
    db.commit()
    detector.save_trips(db, [_trip("2024-06-01T08:00:00", "2024-06-01T08:30:00")])
    t = db.execute("SELECT purpose, destination FROM trips").fetchone()
    assert (t["purpose"], t["destination"]) == ("Dienstreise", "Messe")


def test_save_backfills_only_trips_without_purpose(client, db):
    loc_id = _location(db, "Müller Werk 2")
    for i, purpose in enumerate(["", None, "Privat"]):
        db.execute(
            "INSERT INTO trips (device, start_time, end_time, end_lat, end_lon, end_address, purpose, destination) "
            "VALUES ('dev1', ?, ?, ?, ?, 'Müller Werk 2', ?, 'Müller Werk 2')",
            (f"2024-06-0{i+1}T08:00:00", f"2024-06-0{i+1}T08:30:00", LAT, LON, purpose))
    db.commit()

    r = client.post(f"/api/locations/{loc_id}",
                    json={"default_purpose": "Kunde", "default_destination": "Firma Müller GmbH"})
    assert r.status_code == 200
    assert r.get_json()["assigned"] == 3

    # Purpose only where empty (the user's "Privat" survives); the visited
    # party is replaced wherever it still is the auto value (location name).
    rows = db.execute("SELECT purpose, destination FROM trips ORDER BY start_time").fetchall()
    assert [(x["purpose"], x["destination"]) for x in rows] == [
        ("Kunde", "Firma Müller GmbH"), ("Kunde", "Firma Müller GmbH"), ("Privat", "Firma Müller GmbH")]
