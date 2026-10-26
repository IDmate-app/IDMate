"""Live detection follows the trip geometry: polygons are matched as drawn,
a departure is anchored on the previous arrival when the first GPS fix has
drifted out of the fence, and an extended or merged trip re-derives its tags
(manual edits stay, stale automatic values follow the new end)."""

import json

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


OFFICE = (52.6038, 12.8889)     # office
STOP = (52.7100, 13.0500)      # intermediate stop, no fence
HOME = (52.8000, 13.3700)


def _loc(db, name, pos, **kw):
    cols = {"lat": pos[0], "lon": pos[1], "radius_m": 200, **kw}
    db.execute(f"INSERT INTO locations (name, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
               (name, *cols.values()))
    db.commit()
    return db.execute("SELECT id FROM locations WHERE name = ?", (name,)).fetchone()["id"]


def _trip(start, end, st="2024-06-01T08:00:00", et="2024-06-01T08:30:00", odo=(1000.0, 1020.0)):
    return {"device": "dev1", "start_time": st, "end_time": et,
            "start_lat": start[0], "start_lon": start[1], "end_lat": end[0], "end_lon": end[1],
            "odo_start": odo[0], "odo_end": odo[1], "distance_km": round(odo[1] - odo[0], 1),
            "soc_start": 80.0, "soc_end": 75.0, "energy_kwh": 4.0, "consumption": 20.0,
            "kw_start": None, "kw_end": None}


def _rows(db):
    return [dict(r) for r in db.execute(
        "SELECT id, end_time, purpose, destination FROM trips ORDER BY start_time").fetchall()]


def test_polygon_is_matched_as_drawn(app_module, db):
    # A 400 m x 400 m square whose stored centre/radius would only cover 200 m.
    d = 0.0018  # ~200 m
    poly = [[OFFICE[0] - d, OFFICE[1] - d], [OFFICE[0] - d, OFFICE[1] + d],
            [OFFICE[0] + d, OFFICE[1] + d], [OFFICE[0] + d, OFFICE[1] - d]]
    _loc(db, "Eidelstedt", OFFICE, shape="polygon", polygon_coords=json.dumps(poly),
         default_purpose="Dienstfahrt", purpose_dirs=3, keep_address=1)
    corner = (OFFICE[0] + 0.0016, OFFICE[1] + 0.0016)   # inside square, ~250 m from centre
    outside = (OFFICE[0] + 0.0016, OFFICE[1] - 0.0030)  # inside 250 m? no — outside the square
    assert [l["name"] for l in detector.matching_locations(*corner, db=db)] == ["Eidelstedt"]
    assert detector.matching_locations(*outside, db=db) == []


def test_extension_retags_arrival(app_module, db):
    _loc(db, "Eidelstedt", OFFICE, default_purpose="Dienstfahrt", purpose_dirs=1, keep_address=1)
    # First seen ending at an intermediate stop → no tag.
    detector.save_trips(db, [_trip(HOME, STOP)])
    assert _rows(db)[0]["purpose"] == ""
    # Next scan: same trip, end moved on to the office → arrival tag applied.
    detector.save_trips(db, [_trip(HOME, OFFICE, et="2024-06-01T09:15:00", odo=(1000.0, 1045.0))])
    rows = _rows(db)
    assert len(rows) == 1 and rows[0]["end_time"] == "2024-06-01T09:15:00"
    assert rows[0]["purpose"] == "Dienstfahrt"


def test_extension_keeps_manual_purpose(app_module, db):
    _loc(db, "Eidelstedt", OFFICE, default_purpose="Dienstfahrt", purpose_dirs=1, keep_address=1)
    detector.save_trips(db, [_trip(HOME, STOP)])
    db.execute("UPDATE trips SET purpose = 'Handeingabe'"); db.commit()
    detector.save_trips(db, [_trip(HOME, OFFICE, et="2024-06-01T09:15:00", odo=(1000.0, 1045.0))])
    assert _rows(db)[0]["purpose"] == "Handeingabe"


def test_extension_replaces_stale_automatic_value(app_module, db):
    _loc(db, "Aldi", STOP, default_purpose="Einkauf", purpose_dirs=1, keep_address=1)
    _loc(db, "Eidelstedt", OFFICE, default_purpose="Dienstfahrt", purpose_dirs=1, keep_address=1)
    detector.save_trips(db, [_trip(HOME, STOP)])
    assert _rows(db)[0]["purpose"] == "Einkauf"           # automatic, from the stop
    detector.save_trips(db, [_trip(HOME, OFFICE, et="2024-06-01T09:15:00", odo=(1000.0, 1045.0))])
    assert _rows(db)[0]["purpose"] == "Dienstfahrt"       # follows the real end


def test_departure_anchors_on_previous_arrival(app_module, db):
    _loc(db, "Eidelstedt", OFFICE, default_purpose="Dienstfahrt", purpose_dirs=2, keep_address=1)
    detector.save_trips(db, [_trip(HOME, OFFICE)])                                   # arrival (no tag: departure-only)
    drifted = (OFFICE[0] + 0.0022, OFFICE[1])                                         # ~245 m out, first fix down the road
    detector.save_trips(db, [_trip(drifted, HOME, st="2024-06-01T15:36:00", et="2024-06-01T16:56:00",
                                   odo=(1020.0, 1070.0))])
    rows = _rows(db)
    assert [r["purpose"] for r in rows] == ["", "Dienstfahrt"]


def test_departure_anchor_needs_nearby_previous_end(app_module, db):
    _loc(db, "Eidelstedt", OFFICE, default_purpose="Dienstfahrt", purpose_dirs=2, keep_address=1)
    detector.save_trips(db, [_trip(HOME, OFFICE)])
    far = (OFFICE[0] + 0.02, OFFICE[1])                                               # ~2.2 km away: not the same stop
    detector.save_trips(db, [_trip(far, HOME, st="2024-06-01T15:36:00", et="2024-06-01T16:56:00",
                                   odo=(1020.0, 1070.0))])
    assert [r["purpose"] for r in _rows(db)] == ["", ""]


def test_backfill_departure_via_previous_arrival(client, db):
    db.execute("INSERT INTO trips (device, start_time, end_time, start_lat, start_lon, end_lat, end_lon) "
               "VALUES ('dev1', '2024-06-01T08:00:00', '2024-06-01T09:15:00', ?, ?, ?, ?)", (*HOME, *OFFICE))
    db.execute("INSERT INTO trips (device, start_time, end_time, start_lat, start_lon, end_lat, end_lon) "
               "VALUES ('dev1', '2024-06-01T15:36:00', '2024-06-01T16:56:00', ?, ?, ?, ?)",
               (OFFICE[0] + 0.0022, OFFICE[1], *HOME))
    db.commit()
    r = client.post("/api/locations", json={"name": "Eidelstedt", "lat": OFFICE[0], "lon": OFFICE[1],
                                             "radius_m": 200, "default_purpose": "Dienstfahrt",
                                             "purpose_dirs": 2, "keep_address": 1})
    assert r.status_code == 200 and r.get_json()["assigned"] == 1
    assert [x["purpose"] or "" for x in _rows(db)] == ["", "Dienstfahrt"]
