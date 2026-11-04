"""Mixed charging parks: an Allego and a Tesla park side by side. A point
belongs to the smallest charge geofence containing it, labels follow the
fences when they are drawn tighter, renamed or deleted — saving one fence no
longer takes every trip of the overlap."""

import json

import pytest

ALLEGO = (53.0000, 10.0000)                  # circle centre
TESLA = (53.0010, 10.0000)                   # 111 m north, inside the Allego circle
TESLA_POLY = [[53.0008, 9.9997], [53.0008, 10.0003], [53.0012, 10.0003], [53.0012, 9.9997]]
ALLEGO_STALL = (53.0000, 10.0002)
OUTSIDE = (53.0020, 10.0000)                 # 222 m north


@pytest.fixture
def park(app_module, db, monkeypatch):
    monkeypatch.setattr(app_module, "_kick_geocoder", lambda: None)
    db.execute("INSERT INTO charge_locations (name, lat, lon, radius_m, shape) VALUES ('Allego', ?, ?, 150, 'circle')",
               ALLEGO)
    db.execute("INSERT INTO charge_locations (name, lat, lon, radius_m, shape, polygon_coords) "
               "VALUES ('Tesla', ?, ?, 30, 'polygon', ?)", (*TESLA, json.dumps(TESLA_POLY)))
    for i in range(11):                      # eleven Tesla visits ...
        db.execute("INSERT INTO trips (device, start_time, end_time, end_lat, end_lon) VALUES "
                   "('id7', ?, ?, ?, ?)", (f"2026-10-{i + 1:02d}T10:00", f"2026-10-{i + 1:02d}T11:00", *TESLA))
    db.execute("INSERT INTO trips (device, start_time, end_time, end_lat, end_lon) VALUES "
               "('id7', '2026-10-20T10:00', '2026-10-20T11:00', ?, ?)", ALLEGO_STALL)   # ... one Allego
    db.execute("INSERT INTO charge_sessions (vehicle_plate, lat, lon, location_name) VALUES ('X', ?, ?, 'Allego')",
               TESLA)
    db.commit()
    return db


def ends(db):
    return [r[0] for r in db.execute("SELECT end_address FROM trips ORDER BY start_time")]


def test_smallest_fence_wins_in_the_overlap(app_module, park):
    r = app_module._relabel_charge_fences(park, {"Allego"})     # saving Allego ...
    assert ends(park) == ["Tesla"] * 11 + ["Allego"]           # ... no longer takes the Tesla trips
    assert r["trips_end"] == 12 and r["charges"] == 1
    assert park.execute("SELECT location_name FROM charge_sessions").fetchone()[0] == "Tesla"
    assert app_module._relabel_charge_fences(park, {"Allego", "Tesla"}) == \
        {"charges": 0, "trips_start": 0, "trips_end": 0}       # stable on the next save


def test_drawing_a_fence_tighter_drops_the_name_outside(app_module, park):
    park.execute("INSERT INTO trips (device, start_time, end_time, end_lat, end_lon, end_address) VALUES "
                 "('id7', '2026-10-25T10:00', '2026-10-25T11:00', ?, ?, 'Allego')", OUTSIDE)
    park.commit()
    app_module._relabel_charge_fences(park, {"Allego"})
    assert ends(park)[-1] is None                              # back to the geocoder


def test_rename_and_delete_move_the_labels(app_module, park):
    app_module._relabel_charge_fences(park, {"Allego", "Tesla"})
    park.execute("UPDATE charge_locations SET name = 'Tesla Supercharger' WHERE name = 'Tesla'")
    app_module._relabel_charge_fences(park, {"Tesla", "Tesla Supercharger"})
    assert ends(park)[:11] == ["Tesla Supercharger"] * 11
    park.execute("DELETE FROM charge_locations WHERE name = 'Tesla Supercharger'")
    app_module._relabel_charge_fences(park, {"Tesla Supercharger"})
    assert ends(park) == ["Allego"] * 12                       # the overlapping fence takes over


def test_match_charge_location_uses_the_same_rule(app_module, park):
    assert app_module.match_charge_location(park, *TESLA)["name"] == "Tesla"
    assert app_module.match_charge_location(park, *ALLEGO_STALL)["name"] == "Allego"
