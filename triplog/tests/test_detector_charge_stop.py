"""A charging stop inside a running trip splits it, so the external charge
between the two trips can be found — from the SoC alone, since the
telemetry has no reliable "parked" signal (speed missing or jittering while
the car sleeps at the charger)."""

from datetime import datetime, timedelta

import detector
from factory import at, reading

BAT = 80.0


def _drive(t0, minutes, soc0, soc1, od0, la0):
    rows = []
    n = minutes * 2
    for i in range(n + 1):
        f = i / n
        rows.append(reading(at(t0, 30 * i), v=80, s=round(soc0 + (soc1 - soc0) * f, 1),
                            od=round(od0 + 26 * minutes / 20 * f, 1), la=la0 + 0.2 * f, lo=13.4))
    return rows


def test_fast_charge_in_one_percent_steps_with_speed_jitter_splits():
    rows = _drive(0, 20, 60, 40, 1000, 52.0)
    # 5 min at a fast charger: speed jitters at 1 km/h, SoC climbs 1 % per 30 s
    for i in range(1, 11):
        rows.append(reading(at(20, 30 * i), v=1, s=40 + i, od=1026, la=52.2, lo=13.4))
    rows += _drive(26, 20, 50, 30, 1026, 52.2)
    trips = detector.detect_trips(rows, bat_kwh=BAT)
    assert len(trips) == 2
    assert trips[0]["soc_end"] == 40 and trips[1]["soc_start"] >= 50


def test_sleeping_car_gps_jitter_and_soc_jump_splits():
    rows = _drive(0, 20, 30, 10, 1000, 52.0)
    # 12 min asleep at the charger: only GPS jitter (60 m per minute), no speed, no SoC
    for i in range(1, 13):
        rows.append(reading(at(20, 60 * i), la=52.2 + (0.00054 if i % 2 else 0), lo=13.4))
    rows += _drive(33, 20, 32, 12, 1026, 52.2)
    trips = detector.detect_trips(rows, bat_kwh=BAT)
    assert len(trips) == 2 and trips[1]["soc_start"] == 32


def test_recuperation_downhill_does_not_split():
    rows = _drive(0, 10, 50, 45, 1000, 46.0)
    for i in range(1, 21):           # long descent at speed: +6 % SoC
        rows.append(reading(at(10, 30 * i), v=60, s=45 + i * 0.3, od=1013 + i * 0.5, la=46.0 + i * 0.001, lo=11.0))
    rows += _drive(21, 10, 51, 46, 1023, 46.1)
    assert len(detector.detect_trips(rows, bat_kwh=BAT)) == 1


def test_dead_zone_merge_never_across_a_charge(db):
    db.execute("INSERT INTO vehicles (plate, device) VALUES ('EV1', 'dev1')")
    db.commit()
    now = datetime.now(detector.LOCAL_TZ).replace(microsecond=0)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S")
    base = {"device": "dev1", "start_lat": 53.0, "start_lon": 10.0, "end_lat": 53.1, "end_lon": 10.1,
            "kw_start": None, "kw_end": None, "consumption": None}
    detector.save_trips(db, [{**base, "start_time": iso(now - timedelta(minutes=70)),
                              "end_time": iso(now - timedelta(minutes=30)), "odo_start": 1000.0,
                              "odo_end": 1060.0, "distance_km": 60.0, "soc_start": 40.0, "soc_end": 10.0,
                              "energy_kwh": 24.0}])
    # 9 min later (inside MERGE_GAP_MIN) the car drives on — with 22 % more SoC
    detector.save_trips(db, [{**base, "start_time": iso(now - timedelta(minutes=21)),
                              "end_time": iso(now - timedelta(minutes=5)), "odo_start": 1060.0,
                              "odo_end": 1080.0, "distance_km": 20.0, "soc_start": 32.0, "soc_end": 26.0,
                              "energy_kwh": 4.8}])
    assert db.execute("SELECT COUNT(*) FROM trips").fetchone()[0] == 2


def _split_client(app_module, db):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin) VALUES (1, 'admin', 'x', 1)")
    db.execute("INSERT INTO vehicles (id, plate, device, battery_capacity_kwh) VALUES (1, 'EV1', 'dev1', 80)")
    db.execute("INSERT INTO trips (id, device, start_time, end_time, odo_start, odo_end, distance_km, soc_start, "
               "soc_end, is_manual) VALUES (1, 'dev1', '2026-10-25T16:44:00', '2026-10-25T18:45:00', 1000, 1200, 200, 42, 8, 0)")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    c = app_module.app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
    return c


def _pts():
    pts = []
    for i in range(0, 58):           # driving 16:44-17:41, odo 1000 → 1100, SoC 42 → 10
        pts.append({"t": f"2026-10-25T{16 + (44 + i) // 60}:{(44 + i) % 60:02d}:00", "lat": 53.0, "lon": 12.0,
                    "odo": 1000 + round(i * 100 / 57), "soc": round(42 - i * 32 / 57, 1)})
    pts.append({"t": "2026-10-25T17:52:30", "lat": 53.3, "lon": 12.2, "odo": 1100, "soc": 32.0})   # after the charge
    for i in range(1, 50):           # driving on 17:53-18:42
        pts.append({"t": f"2026-10-25T{17 + (52 + i) // 60}:{(52 + i) % 60:02d}:00", "lat": 53.3, "lon": 12.2,
                    "odo": 1100 + 2 * i, "soc": round(32 - i * 0.48, 1)})
    return pts


def test_manual_split_inside_a_stop_keeps_the_charge_visible(app_module, db, monkeypatch):
    c = _split_client(app_module, db)
    import blueprints.trips as bt
    monkeypatch.setattr(bt, "_trip_telemetry_points", lambda trip: _pts())
    r = c.post("/api/trips/1/split", json={"at": "2026-10-25T17:46:00"})
    assert r.status_code == 200, r.get_json()
    a, b = db.execute("SELECT start_time, end_time, soc_start, soc_end, odo_end, odo_start FROM trips ORDER BY start_time").fetchall()
    assert a["end_time"] == "2026-10-25T17:41:00" and a["soc_end"] == 10.0
    assert b["start_time"] == "2026-10-25T17:52:30" and b["soc_start"] == 32.0
    from blueprints.charges import detect_external_from_trips
    db.execute("UPDATE trips SET start_time = datetime('now', '-3 hours'), end_time = datetime('now', '-2 hours') "
               "WHERE end_time = '2026-10-25T17:41:00'")
    db.execute("UPDATE trips SET start_time = datetime('now', '-100 minutes'), end_time = datetime('now', '-60 minutes') "
               "WHERE start_time = '2026-10-25T17:52:30'")
    db.commit()
    assert detect_external_from_trips(db) == 1


def test_manual_split_while_driving_splits_at_the_point(app_module, db, monkeypatch):
    c = _split_client(app_module, db)
    import blueprints.trips as bt
    monkeypatch.setattr(bt, "_trip_telemetry_points", lambda trip: _pts())
    assert c.post("/api/trips/1/split", json={"at": "2026-10-25T17:10:00"}).status_code == 200
    a, b = db.execute("SELECT end_time, soc_end, start_time, soc_start FROM trips ORDER BY id").fetchall()
    assert a["end_time"] == "2026-10-25T17:10:00" and b["start_time"] == "2026-10-25T17:10:00"
