"""Additional geofences per location ("alias" rows with alias_of = parent):
the alias only widens WHERE the parent is recognised — name, trip defaults,
route rules, visits and the trip address all resolve to the parent."""

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


P_LAT, P_LON = 52.5200, 13.4050      # parent geofence
A_LAT, A_LON = 52.5300, 13.4200      # alias geofence, ~1.5 km away


def _loc(db, name, lat, lon, **kw):
    cols = {"lat": lat, "lon": lon, "radius_m": 200, **kw}
    db.execute(f"INSERT INTO locations (name, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
               (name, *cols.values()))
    db.commit()
    return db.execute("SELECT id FROM locations WHERE name = ?", (name,)).fetchone()["id"]


def _trip(start_iso, end_iso, end_lat, end_lon):
    return {"device": "dev1", "start_time": start_iso, "end_time": end_iso,
            "start_lat": 52.40, "start_lon": 13.30, "end_lat": end_lat, "end_lon": end_lon,
            "odo_start": 1000.0, "odo_end": 1020.0, "distance_km": 20.0,
            "soc_start": 80.0, "soc_end": 75.0, "energy_kwh": 4.0, "consumption": 20.0,
            "kw_start": None, "kw_end": None}


def test_match_in_alias_geofence_resolves_to_parent(app_module, db):
    pid = _loc(db, "Müller", P_LAT, P_LON, default_purpose="Kunde")
    _loc(db, "Müller Parkplatz", A_LAT, A_LON, alias_of=pid)
    hit = detector.match_location(db, A_LAT + 0.0002, A_LON)
    assert hit is not None and hit["id"] == pid and hit["name"] == "Müller"


def test_trip_ending_in_alias_gets_parent_defaults(app_module, db):
    pid = _loc(db, "Müller", P_LAT, P_LON, default_purpose="Kunde", default_reason="Wartung")
    _loc(db, "Müller Parkplatz", A_LAT, A_LON, alias_of=pid)
    detector.save_trips(db, [_trip("2024-06-01T08:00:00", "2024-06-01T08:30:00", A_LAT, A_LON)])
    t = db.execute("SELECT purpose, destination, visit_reason FROM trips").fetchone()
    assert (t["purpose"], t["destination"], t["visit_reason"]) == ("Kunde", "Müller", "Wartung")


def test_create_alias_inherits_look_and_renames_trips_to_parent(client, db):
    pid = _loc(db, "Müller", P_LAT, P_LON, icon="factory", color="#123456", icon_color="black")
    db.execute("INSERT INTO trips (device, start_time, end_time, end_lat, end_lon, end_address) "
               "VALUES ('dev1', '2024-06-01T08:00:00', '2024-06-01T08:30:00', ?, ?, 'Parkstraße 1')",
               (A_LAT, A_LON))
    db.commit()

    r = client.post("/api/locations", json={"name": "Müller Parkplatz", "lat": A_LAT, "lon": A_LON,
                                             "radius_m": 200, "alias_of": pid})
    assert r.status_code == 200
    body = r.get_json()
    assert body["reassigned"]["trips_end"] == 1

    alias = db.execute("SELECT * FROM locations WHERE id = ?", (body["id"],)).fetchone()
    assert alias["alias_of"] == pid
    assert (alias["icon"], alias["color"], alias["icon_color"]) == ("factory", "#123456", "black")
    assert db.execute("SELECT end_address FROM trips").fetchone()["end_address"] == "Müller"


def test_alias_of_an_alias_is_rejected(client, db):
    pid = _loc(db, "Müller", P_LAT, P_LON)
    aid = _loc(db, "Müller Parkplatz", A_LAT, A_LON, alias_of=pid)
    r = client.post("/api/locations", json={"name": "x", "lat": A_LAT, "lon": A_LON, "alias_of": aid})
    assert r.status_code == 404


def test_parent_look_change_propagates_to_aliases(client, db):
    pid = _loc(db, "Müller", P_LAT, P_LON)
    aid = _loc(db, "Müller Parkplatz", A_LAT, A_LON, alias_of=pid)
    r = client.post(f"/api/locations/{pid}", json={"color": "#abcdef", "icon": "shop"})
    assert r.status_code == 200
    a = db.execute("SELECT color, icon FROM locations WHERE id = ?", (aid,)).fetchone()
    assert (a["color"], a["icon"]) == ("#abcdef", "shop")


def test_delete_parent_cascades_aliases_and_clears_trips(client, db):
    pid = _loc(db, "Müller", P_LAT, P_LON)
    _loc(db, "Müller Parkplatz", A_LAT, A_LON, alias_of=pid)
    db.execute("INSERT INTO trips (device, start_time, end_time, end_lat, end_lon, end_address) "
               "VALUES ('dev1', '2024-06-01T08:00:00', '2024-06-01T08:30:00', ?, ?, 'Müller')",
               (A_LAT, A_LON))
    db.commit()
    r = client.delete(f"/api/locations/{pid}")
    assert r.status_code == 200
    assert r.get_json()["cleared"]["trips_end"] == 1
    assert db.execute("SELECT COUNT(*) AS n FROM locations").fetchone()["n"] == 0
    assert db.execute("SELECT end_address FROM trips").fetchone()["end_address"] is None


def test_delete_alias_keeps_parent_and_parent_trips(client, db):
    pid = _loc(db, "Müller", P_LAT, P_LON)
    aid = _loc(db, "Müller Parkplatz", A_LAT, A_LON, alias_of=pid)
    for lat, lon in ((P_LAT, P_LON), (A_LAT, A_LON)):
        db.execute("INSERT INTO trips (device, start_time, end_time, end_lat, end_lon, end_address) "
                   "VALUES ('dev1', '2024-06-01T08:00:00', '2024-06-01T08:30:00', ?, ?, 'Müller')", (lat, lon))
    db.commit()
    r = client.delete(f"/api/locations/{aid}")
    assert r.status_code == 200 and r.get_json()["cleared"]["trips_end"] == 1
    rows = db.execute("SELECT end_lat, end_address FROM trips ORDER BY end_lat").fetchall()
    assert [x["end_address"] for x in rows] == ["Müller", None]
    assert db.execute("SELECT COUNT(*) AS n FROM locations").fetchone()["n"] == 1
