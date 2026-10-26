"""A trip extended by a later scan gets the energy of its whole SoC span,
not only of the newly scanned piece (82 km with 0.33 kWh in real data)."""

import logging

import detector
import migrations

BASE = {"device": "dev1", "start_lat": 53.0, "start_lon": 10.0, "end_lat": 53.1, "end_lon": 10.1,
        "kw_start": None, "kw_end": None, "cap_kwh": 83.3}


def _row(db):
    return db.execute("SELECT start_time, end_time, distance_km, soc_start, soc_end, energy_kwh, "
                      "consumption FROM trips").fetchone()


def test_extend_end_recomputes_energy_over_whole_trip(app_module, db):
    db.execute("INSERT INTO vehicles (plate, device) VALUES ('EV1', 'dev1')")
    db.commit()
    detector.save_trips(db, [{**BASE, "start_time": "2026-09-26T10:19:00", "end_time": "2026-09-26T11:17:00",
                              "odo_start": 31217.0, "odo_end": 31297.0, "distance_km": 80.0,
                              "soc_start": 80.5, "soc_end": 63.7, "energy_kwh": 13.99, "consumption": 17.5}])
    # next scan sees only the tail (overlapping the stored trip), reaching further
    detector.save_trips(db, [{**BASE, "start_time": "2026-09-26T11:10:00", "end_time": "2026-09-26T11:35:00",
                              "odo_start": 31296.0, "odo_end": 31299.0, "distance_km": 3.0,
                              "soc_start": 63.2, "soc_end": 62.8, "energy_kwh": 0.33, "consumption": None}])
    r = _row(db)
    assert r["end_time"] == "2026-09-26T11:35:00" and r["distance_km"] == 82.0
    assert r["energy_kwh"] == round((80.5 - 62.8) / 100 * 83.3, 2)
    assert r["consumption"] == round(r["energy_kwh"] / 82.0 * 100, 1)


def test_extend_start_recomputes_energy_and_distance(app_module, db):
    db.execute("INSERT INTO vehicles (plate, device) VALUES ('EV1', 'dev1')")
    db.commit()
    detector.save_trips(db, [{**BASE, "start_time": "2026-09-26T10:30:00", "end_time": "2026-09-26T11:30:00",
                              "odo_start": 1020.0, "odo_end": 1080.0, "distance_km": 60.0,
                              "soc_start": 70.0, "soc_end": 58.0, "energy_kwh": 10.0, "consumption": 16.7}])
    detector.save_trips(db, [{**BASE, "start_time": "2026-09-26T10:00:00", "end_time": "2026-09-26T10:40:00",
                              "odo_start": 1000.0, "odo_end": 1025.0, "distance_km": 25.0,
                              "soc_start": 74.0, "soc_end": 69.0, "energy_kwh": 4.17, "consumption": 16.7}])
    r = _row(db)
    assert r["start_time"] == "2026-09-26T10:00:00" and r["distance_km"] == 80.0
    assert r["energy_kwh"] == round(16 / 100 * 83.3, 2)


def test_migration_repairs_wrong_energy(app_module, db):
    db.execute("INSERT INTO vehicles (plate, device, battery_capacity_kwh) VALUES ('EV1', 'dev1', 80)")
    db.execute("INSERT INTO trips (device, start_time, end_time, distance_km, soc_start, soc_end, "
               "energy_kwh, consumption, is_manual) VALUES "
               "('dev1', '2026-09-26T10:19:00', '2026-09-26T11:35:00', 82, 80.5, 62.8, 0.33, 0.4, 0), "
               "('dev1', '2026-09-27T10:00:00', '2026-09-27T11:00:00', 50, 60, 50, 8.1, 16.2, 0), "
               "('dev1', '2026-09-28T10:00:00', '2026-09-28T11:00:00', 50, 60, 50, 2.0, 4.0, 1)")
    db.commit()
    migrations.m064_repair_extended_trip_energy(db, logging.getLogger("t"))
    e = [r[0] for r in db.execute("SELECT energy_kwh FROM trips ORDER BY start_time")]
    assert e == [round(17.7 / 100 * 80, 2), 8.1, 2.0]


def test_migration_stores_utc_trip_times_as_local(db):
    db.execute("INSERT INTO trips (device, start_time, end_time) VALUES "
               "('dev1', '2026-03-30T05:22:50+00:00', '2026-03-30T06:31:45+00:00'), "
               "('dev1', '2026-07-01T08:00:00', '2026-07-01T09:00:00')")
    db.commit()
    migrations.m065_trip_times_local(db, logging.getLogger("t"))
    rows = db.execute("SELECT start_time, end_time FROM trips ORDER BY id").fetchall()
    assert tuple(rows[0]) == ("2026-03-30T07:22:50", "2026-03-30T08:31:45")   # CEST
    assert tuple(rows[1]) == ("2026-07-01T08:00:00", "2026-07-01T09:00:00")


def test_migration_moves_filled_in_bounds_along(db):
    db.execute("INSERT INTO trips (device, start_time, end_time, is_manual) VALUES "
               "('dev1', '2026-03-30T05:22:50+00:00', '2026-03-30T06:31:45+00:00', 0), "
               "('dev1', '2026-03-28T10:40', '2026-03-30T05:22', 1), "
               "('dev1', '2026-03-30T06:31', '2026-03-30T15:13', 1)")
    db.commit()
    migrations.m065_trip_times_local(db, logging.getLogger("t"))
    rows = [tuple(r) for r in db.execute("SELECT start_time, end_time FROM trips ORDER BY id")]
    assert rows[1] == ("2026-03-28T10:40", "2026-03-30T07:22")
    assert rows[2] == ("2026-03-30T08:31", "2026-03-30T15:13")
