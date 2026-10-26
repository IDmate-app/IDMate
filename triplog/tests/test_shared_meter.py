"""Shared wallbox meter: a charge of the other car spanning this one opens
no phantom gap; a span already stored can't be filled in again; double
counted readings are found and removed (real case 10.10.2026)."""

import fahrzeugmodell as fm
import pytest


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin) VALUES (1, 'admin', 'x', 1)")
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'NOX', 'd1'), (2, 'TES', 'd2')")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def _r(db, ts, plate, a, b):
    db.execute("INSERT INTO charge_readings (timestamp, vehicle_plate, meter_start, meter_end, kwh) "
               "VALUES (?, ?, ?, ?, ?)", (ts, plate, a, b, round(b - a, 3)))


def _oct10(db):
    _r(db, "2026-10-10T01:15:00", "NOX", 7556.10, 7566.70)
    _r(db, "2026-10-10T02:45:00", "NOX", 7566.70, 7577.07)
    _r(db, "2026-10-10T11:15:00", "TES", 7577.07, 7588.08)
    _r(db, "2026-10-10T12:15:00", "NOX", 7588.08, 7615.00)
    _r(db, "2026-10-10T17:15:00", "NOX", 7615.00, 7642.01)
    _r(db, "2026-10-11T12:15:00", "TES", 7642.01, 7663.40)
    db.commit()


def test_meter_gaps_use_highest_meter_so_far():
    s = [{"first_meter": 7556.1, "last_meter": 7642.01, "n": "nox"},
         {"first_meter": 7577.07, "last_meter": 7588.08, "n": "tes"},
         {"first_meter": 7642.01, "last_meter": 7663.4, "n": "tes2"},
         {"first_meter": 7670.0, "last_meter": 7680.0, "n": "x"}]
    gaps = fm.meter_gaps(s)
    assert [(a["n"], b["n"], d) for a, b, d in gaps] == [("tes2", "x", 6.6)]


def test_covered_readings():
    rs = [{"id": 1, "meter_start": 0, "meter_end": 10}, {"id": 2, "meter_start": 10, "meter_end": 20},
          {"id": 3, "meter_start": 5, "meter_end": 18}, {"id": 4, "meter_start": 18, "meter_end": 25},
          {"id": 5, "meter_start": 20, "meter_end": 25}]
    assert fm.covered_readings(rs) == [3, 5]


def test_no_phantom_gap_and_fill_in_refused(app_module, db, client):
    _oct10(db)
    app_module.rebuild_charge_sessions(db)
    gaps = client.get("/api/charge/gaps").get_json()["gaps"]
    assert not any(abs(g["kwh_missing"] - 53.93) < 0.01 for g in gaps)
    r = client.put("/api/charge/readings?rebuild=0", json={
        "timestamp": "2026-10-10T12:13:00", "vehicle_plate": "TES",
        "meter_start": 7588.08, "meter_end": 7642.01, "kwh": 53.93})
    assert r.status_code == 409 and "NOX" in r.get_json()["error"]


def test_dedup_removes_double_counted_span(app_module, db, client):
    _oct10(db)
    _r(db, "2026-10-10T12:13:00", "TES", 7588.08, 7642.01)     # the filled-in phantom gap
    db.commit()
    checks = client.get("/api/admin/diagnostics/checks").get_json()["checks"]
    ov = next(c for c in checks if c["key"] == "readings_meter_overlap")
    assert ov["level"] == "error" and ov["value"] == 1
    assert client.post("/api/charge/readings/dedup").get_json()["removed"] == 1
    tes = db.execute("SELECT total_kwh FROM charge_sessions WHERE vehicle_plate = 'TES' "
                     "AND start_time LIKE '2026-10-10%'").fetchone()[0]
    assert tes == pytest.approx(11.01, abs=0.01)
