"""Vehicle texts from Home Assistant or the external-charge form may carry the
vehicle's name instead of its plate. They must land on the existing vehicle,
never spawn a second, device-less one (which would fall back to the default
device and show another car's trips)."""

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def client(app_module, db, monkeypatch):
    import config
    monkeypatch.setattr(config, "CHARGE_WEBHOOK_TOKEN", "test-token")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def _veh(db, plate, name, device):
    db.execute("INSERT INTO vehicles (plate, name, device) VALUES (?, ?, ?)",
               (plate, name, device))
    db.commit()


def _count(db):
    return db.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]


def test_resolve_plate_name_and_unknown(app_module, db):
    _veh(db, "AB-C 1", "Star", "dev_star")
    assert app_module.resolve_vehicle_plate(db, "AB-C 1") == "AB-C 1"
    assert app_module.resolve_vehicle_plate(db, "star") == "AB-C 1"
    assert app_module.resolve_vehicle_plate(db, " Star ") == "AB-C 1"
    assert app_module.resolve_vehicle_plate(db, "Other") is None
    assert app_module.resolve_vehicle_plate(db, "") is None


def test_name_beats_stray_plate_row(app_module, db):
    _veh(db, "AB-C 1", "Star", "dev_star")
    db.execute("INSERT INTO vehicles (plate) VALUES ('Star')")
    db.commit()
    assert app_module.resolve_vehicle_plate(db, "Star") == "AB-C 1"


def test_webhook_maps_name_to_plate(client, db):
    _veh(db, "AB-C 1", "Star", "dev_star")
    ts = (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S")
    r = client.post("/api/charge/reading",
                    json={"vehicle": "Star", "timestamp": ts,
                          "meter_start": 1000.0, "meter_end": 1002.0},
                    headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200 and r.get_json()["ok"]
    row = db.execute("SELECT vehicle_plate FROM charge_readings").fetchone()
    assert row["vehicle_plate"] == "AB-C 1"
    assert _count(db) == 1


def test_rebuild_skips_names_creates_unknown(app_module, db):
    _veh(db, "AB-C 1", "Star", "dev_star")
    for i, plate in enumerate(("Star", "NEW-1")):
        db.execute(
            """INSERT INTO charge_readings (timestamp, vehicle_plate, meter_start,
               meter_end, kwh, tibber_price, tibber_grundgebuehr)
               VALUES (?, ?, ?, ?, 2.0, 0.3, 0)""",
            (f"2024-06-0{i + 1}T10:00:00", plate, 1000.0 + i * 10, 1002.0 + i * 10))
    db.commit()
    app_module.rebuild_charge_sessions(db)
    plates = {r[0] for r in db.execute("SELECT plate FROM vehicles")}
    assert plates == {"AB-C 1", "NEW-1"}
