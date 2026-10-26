"""keep_address geofences tag trips (destination / purpose / reason) but never
replace the street address — the visited company is tracked, the address in
the logbook stays what the geocoder found."""

import pytest

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


def _trip(db, day, end_address=None, destination=None, purpose=None):
    db.execute("INSERT INTO trips (device, start_time, end_time, start_lat, start_lon, end_lat, end_lon, "
               "end_address, destination, purpose) VALUES ('dev1', ?, ?, 52.40, 13.30, ?, ?, ?, ?, ?)",
               (f"2024-06-{day:02d}T08:00:00", f"2024-06-{day:02d}T08:30:00", LAT, LON,
                end_address, destination, purpose))
    db.commit()


def test_geocoder_keeps_street_address_for_keep_address_geofence(app_module, db, monkeypatch):
    _loc(db, "DB Stw 1", default_destination="DB", keep_address=1)
    _trip(db, 1)
    monkeypatch.setattr(geocoder, "_safe_geocode", lambda lat, lon: ("Bahnhofstraße 1, Musterstadt", "DE"))
    geocoder.geocode_trips(db)
    assert db.execute("SELECT end_address FROM trips").fetchone()["end_address"] == "Bahnhofstraße 1, Musterstadt"


def test_geocoder_still_names_classic_geofence(app_module, db, monkeypatch):
    _loc(db, "Zuhause")
    _trip(db, 1)
    monkeypatch.setattr(geocoder, "_safe_geocode", lambda lat, lon: ("should not be used", "DE"))
    geocoder.geocode_trips(db)
    assert db.execute("SELECT end_address FROM trips").fetchone()["end_address"] == "Zuhause"


def test_create_keep_address_geofence_tags_destination_without_renaming(client, db):
    _trip(db, 1, end_address="Bahnhofstraße 1")
    _trip(db, 2, end_address="Bahnhofstraße 1", destination="Eigenes Ziel", purpose="Privat")
    r = client.post("/api/locations", json={"name": "DB Stw 1", "lat": LAT, "lon": LON, "radius_m": 200,
                                             "default_destination": "DB", "default_purpose": "Kunde",
                                             "keep_address": 1})
    assert r.status_code == 200
    body = r.get_json()
    assert body["reassigned"] == {"trips_start": 0, "trips_end": 0}
    assert body["assigned"] == 1
    rows = db.execute("SELECT end_address, destination, purpose FROM trips ORDER BY start_time").fetchall()
    assert [tuple(x) for x in rows] == [
        ("Bahnhofstraße 1", "DB", "Kunde"),
        ("Bahnhofstraße 1", "Eigenes Ziel", "Privat"),   # user tags untouched
    ]


def test_switching_to_keep_address_restores_street_address(client, db):
    lid = _loc(db, "DB Stw 1", default_destination="DB")
    _trip(db, 1, end_address="DB Stw 1")
    r = client.post(f"/api/locations/{lid}", json={"keep_address": 1})
    assert r.status_code == 200
    t = db.execute("SELECT end_address, destination FROM trips").fetchone()
    assert t["end_address"] is None        # geocoder will fetch the street address again
    assert t["destination"] == "DB"        # the visited company stays tagged


def test_alias_inherits_keep_address(client, db):
    pid = _loc(db, "DB Stw 1", default_destination="DB", keep_address=1)
    r = client.post("/api/locations", json={"name": "DB Werk", "lat": 52.53, "lon": 13.42,
                                             "radius_m": 200, "alias_of": pid})
    assert r.status_code == 200
    assert db.execute("SELECT keep_address FROM locations WHERE id = ?",
                      (r.get_json()["id"],)).fetchone()["keep_address"] == 1


def test_logbook_options_hide_simple_destination_geofences(client, db):
    db.execute("INSERT INTO vehicles (plate, device) VALUES ('EV1', 'dev1')")
    _loc(db, "GTU Büro")                                   # main place
    _loc(db, "DB Stw 1", default_destination="DB", keep_address=1)  # simple geofence
    r = client.get("/api/fahrtenbuch/options")
    assert r.status_code == 200
    names = [l["name"] for l in r.get_json()["locations"]]
    assert "GTU Büro" in names and "DB Stw 1" not in names


def test_location_list_hides_tagging_geofences_unless_all(client, db):
    _loc(db, "GTU Büro")
    _loc(db, "Eidelstedt", default_purpose="Büro GTU", keep_address=1)
    names = [l["name"] for l in client.get("/api/locations").get_json()]
    assert names == ["GTU Büro"]
    names_all = [l["name"] for l in client.get("/api/locations?all=1").get_json()]
    assert sorted(names_all) == ["Eidelstedt", "GTU Büro"]
