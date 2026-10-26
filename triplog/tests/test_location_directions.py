"""Per-tag direction bits: a purpose / destination / reason on a geofence
applies on ARRIVAL (bit 1) and/or DEPARTURE (bit 2). Arrival tags at the trip
end win; departure tags at the trip start fill what is still empty."""

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


OFFICE = (52.5200, 13.4050)
HOME = (52.80, 13.37)


def _loc(db, name, pos, **kw):
    cols = {"lat": pos[0], "lon": pos[1], "radius_m": 200, **kw}
    db.execute(f"INSERT INTO locations (name, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
               (name, *cols.values()))
    db.commit()
    return db.execute("SELECT id FROM locations WHERE name = ?", (name,)).fetchone()["id"]


def _trip(start, end, day=1):
    return {"device": "dev1", "start_time": f"2024-06-{day:02d}T08:00:00", "end_time": f"2024-06-{day:02d}T08:30:00",
            "start_lat": start[0], "start_lon": start[1], "end_lat": end[0], "end_lon": end[1],
            "odo_start": 1000.0, "odo_end": 1020.0, "distance_km": 20.0,
            "soc_start": 80.0, "soc_end": 75.0, "energy_kwh": 4.0, "consumption": 20.0,
            "kw_start": None, "kw_end": None}


def _purposes(db):
    return [r["purpose"] for r in db.execute("SELECT purpose FROM trips ORDER BY start_time").fetchall()]


def test_departure_only_tag_ignores_arrivals(app_module, db):
    _loc(db, "GTU Tiefgarage", OFFICE, default_purpose="Büro GTU", purpose_dirs=2, keep_address=1)
    detector.save_trips(db, [_trip(HOME, OFFICE, 1), _trip(OFFICE, HOME, 2)])
    assert _purposes(db) == ["", "Büro GTU"]


def test_arrival_only_tag_ignores_departures(app_module, db):
    _loc(db, "GTU Büro", OFFICE, default_purpose="Büro GTU", purpose_dirs=1, keep_address=1)
    detector.save_trips(db, [_trip(HOME, OFFICE, 1), _trip(OFFICE, HOME, 2)])
    assert _purposes(db) == ["Büro GTU", ""]


def test_both_directions(app_module, db):
    _loc(db, "GTU Büro", OFFICE, default_purpose="Büro GTU", purpose_dirs=3, keep_address=1)
    detector.save_trips(db, [_trip(HOME, OFFICE, 1), _trip(OFFICE, HOME, 2)])
    assert _purposes(db) == ["Büro GTU", "Büro GTU"]


def test_fields_fall_back_independently(app_module, db):
    # Start: office tags the purpose on departure. End: customer geofence tags
    # the destination on arrival only — the trip gets both.
    _loc(db, "GTU Büro", OFFICE, default_purpose="Büro GTU", purpose_dirs=2, keep_address=1)
    _loc(db, "DB Stw 1", HOME, default_destination="DB", destination_dirs=1, keep_address=1)
    detector.save_trips(db, [_trip(OFFICE, HOME)])
    t = db.execute("SELECT purpose, destination FROM trips").fetchone()
    assert (t["purpose"], t["destination"]) == ("Büro GTU", "DB")


def test_departure_never_lends_a_place_name_as_destination(app_module, db):
    _loc(db, "GTU Büro", OFFICE, destination_dirs=3)      # real place, both directions
    detector.save_trips(db, [_trip(OFFICE, HOME)])
    assert db.execute("SELECT destination FROM trips").fetchone()["destination"] == ""


def test_api_stores_direction_bits_and_backfills_departures(client, db):
    db.execute("INSERT INTO trips (device, start_time, end_time, start_lat, start_lon, end_lat, end_lon) "
               "VALUES ('dev1', '2024-06-01T08:00:00', '2024-06-01T08:30:00', ?, ?, ?, ?)",
               (*OFFICE, *HOME))
    db.commit()
    r = client.post("/api/locations", json={"name": "GTU Tiefgarage", "lat": OFFICE[0], "lon": OFFICE[1],
                                             "radius_m": 200, "default_purpose": "Büro GTU",
                                             "purpose_dirs": 2, "keep_address": 1})
    assert r.status_code == 200 and r.get_json()["assigned"] == 1
    loc = db.execute("SELECT purpose_dirs, destination_dirs FROM locations").fetchone()
    assert (loc["purpose_dirs"], loc["destination_dirs"]) == (2, 1)
    assert db.execute("SELECT purpose FROM trips").fetchone()["purpose"] == "Büro GTU"

    r = client.post(f"/api/locations/{r.get_json()['id']}", json={"purpose_dirs": 3})
    assert r.status_code == 200
    assert db.execute("SELECT purpose_dirs FROM locations").fetchone()["purpose_dirs"] == 3


def test_apply_all_covers_the_whole_history(client, db):
    # Two untagged trips years apart, one ending at the office, one leaving it.
    db.execute("INSERT INTO trips (device, start_time, end_time, start_lat, start_lon, end_lat, end_lon) "
               "VALUES ('dev1', '2021-03-01T08:00:00', '2021-03-01T08:30:00', ?, ?, ?, ?)", (*HOME, *OFFICE))
    db.execute("INSERT INTO trips (device, start_time, end_time, start_lat, start_lon, end_lat, end_lon, purpose) "
               "VALUES ('dev1', '2026-08-01T08:00:00', '2026-08-01T08:30:00', ?, ?, ?, ?, 'Handeingabe')", (*OFFICE, *HOME))
    db.execute("INSERT INTO trips (device, start_time, end_time, start_lat, start_lon, end_lat, end_lon) "
               "VALUES ('dev1', '2026-08-02T08:00:00', '2026-08-02T08:30:00', ?, ?, ?, ?)", (*OFFICE, *HOME))
    db.commit()
    _loc(db, "GTU Büro", OFFICE, default_purpose="Büro GTU", purpose_dirs=3)
    r = client.post("/api/locations/apply-defaults")
    assert r.status_code == 200
    assert r.get_json()["assigned"] == 2
    assert _purposes(db) == ["Büro GTU", "Handeingabe", "Büro GTU"]


def test_direction_zero_disables_the_tag(client, db, app_module):
    lid = _loc(db, "GTU Büro", OFFICE, default_purpose="Büro GTU", purpose_dirs=3)
    r = client.post(f"/api/locations/{lid}", json={"purpose_dirs": 0})
    assert r.status_code == 200
    assert db.execute("SELECT purpose_dirs FROM locations").fetchone()["purpose_dirs"] == 0
    detector.save_trips(db, [_trip(HOME, OFFICE, 1), _trip(OFFICE, HOME, 2)])
    assert _purposes(db) == ["", ""]


def test_clearing_the_tag_value_stores_null(client, db):
    lid = _loc(db, "GTU Büro", OFFICE, default_purpose="Büro GTU", purpose_dirs=3)
    r = client.post(f"/api/locations/{lid}", json={"default_purpose": "", "purpose_dirs": 0})
    assert r.status_code == 200
    assert db.execute("SELECT default_purpose FROM locations").fetchone()["default_purpose"] is None
