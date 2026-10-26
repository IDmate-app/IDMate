"""Overlapping geofences each tag one thing: the detector merges destination,
purpose and reason over EVERY geofence containing the trip end, a simple
tagging geofence never lends its name as destination, and address naming /
route-rule anchoring prefer the nearest real place over a nearer tagging
geofence."""

import pytest

import detector
import geocoder


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


def _loc(db, name, **kw):
    cols = {"lat": LAT, "lon": LON, "radius_m": 200, **kw}
    db.execute(f"INSERT INTO locations (name, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
               (name, *cols.values()))
    db.commit()
    return db.execute("SELECT id FROM locations WHERE name = ?", (name,)).fetchone()["id"]


def _trip(end_lat=LAT, end_lon=LON):
    return {"device": "dev1", "start_time": "2024-06-01T08:00:00", "end_time": "2024-06-01T08:30:00",
            "start_lat": 52.40, "start_lon": 13.30, "end_lat": end_lat, "end_lon": end_lon,
            "odo_start": 1000.0, "odo_end": 1020.0, "distance_km": 20.0,
            "soc_start": 80.0, "soc_end": 75.0, "energy_kwh": 4.0, "consumption": 20.0,
            "kw_start": None, "kw_end": None}


def test_overlapping_tag_geofences_are_merged(app_module, db):
    _loc(db, "DB Stw 1", default_destination="DB", keep_address=1)
    _loc(db, "Kundenbesuch", default_purpose="Kunde", keep_address=1, lat=LAT + 0.0003)
    _loc(db, "Wartungsgrund", default_reason="Wartung", keep_address=1, lat=LAT - 0.0003)
    detector.save_trips(db, [_trip()])
    t = db.execute("SELECT purpose, destination, visit_reason FROM trips").fetchone()
    assert (t["purpose"], t["destination"], t["visit_reason"]) == ("Kunde", "DB", "Wartung")


def test_purpose_only_geofence_does_not_lend_its_name_as_destination(app_module, db):
    _loc(db, "Dienstreise", default_purpose="Dienstreise", keep_address=1)
    detector.save_trips(db, [_trip()])
    t = db.execute("SELECT purpose, destination FROM trips").fetchone()
    assert (t["purpose"], t["destination"]) == ("Dienstreise", "")


def test_place_name_still_becomes_destination_next_to_a_tag_geofence(app_module, db):
    _loc(db, "GTU Büro")                                             # real place, further away
    _loc(db, "Dienst", default_purpose="Dienst", keep_address=1, lat=LAT + 0.0001)  # nearer tag
    detector.save_trips(db, [_trip()])
    t = db.execute("SELECT purpose, destination FROM trips").fetchone()
    assert (t["purpose"], t["destination"]) == ("Dienst", "GTU Büro")


def test_geocoder_prefers_place_over_nearer_tag_geofence(app_module, db, monkeypatch):
    _loc(db, "GTU Büro", lat=LAT + 0.0008)
    _loc(db, "Dienst", default_purpose="Dienst", keep_address=1)
    db.execute("INSERT INTO trips (device, start_time, end_time, start_lat, start_lon, end_lat, end_lon) "
               "VALUES ('dev1', '2024-06-01T08:00:00', '2024-06-01T08:30:00', 52.40, 13.30, ?, ?)", (LAT, LON))
    db.commit()
    monkeypatch.setattr(geocoder, "_safe_geocode", lambda lat, lon: ("street", "DE"))
    geocoder.geocode_trips(db)
    assert db.execute("SELECT end_address FROM trips").fetchone()["end_address"] == "GTU Büro"


def test_route_rule_anchors_on_place_not_on_tag_geofence(app_module, db):
    pid = _loc(db, "GTU Büro", lat=LAT + 0.0008)
    _loc(db, "Dienst", default_purpose="Dienst", keep_address=1)
    db.execute("INSERT INTO route_rules (from_location_id, to_location_id, purpose) VALUES (NULL, ?, 'Regelzweck')", (pid,))
    db.commit()
    detector.save_trips(db, [_trip()])
    assert db.execute("SELECT purpose FROM trips").fetchone()["purpose"] == "Regelzweck"


def test_backfill_of_purpose_geofence_keeps_destination(client, db):
    db.execute("INSERT INTO trips (device, start_time, end_time, end_lat, end_lon, end_address, destination) "
               "VALUES ('dev1', '2024-06-01T08:00:00', '2024-06-01T08:30:00', ?, ?, 'Bahnhofstraße 1', 'DB')", (LAT, LON))
    db.commit()
    r = client.post("/api/locations", json={"name": "Kundenbesuch", "lat": LAT, "lon": LON, "radius_m": 200,
                                             "default_purpose": "Kunde", "keep_address": 1})
    assert r.status_code == 200 and r.get_json()["assigned"] == 1
    t = db.execute("SELECT purpose, destination, end_address FROM trips").fetchone()
    assert (t["purpose"], t["destination"], t["end_address"]) == ("Kunde", "DB", "Bahnhofstraße 1")
