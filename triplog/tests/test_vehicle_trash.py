"""Vehicle delete goes to a 14-day trash; the confirm preview names exactly
which vehicle and how many trips/charges belong to it."""

import json
from datetime import datetime, timedelta, timezone

import pytest

import vehicle_trash


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin) VALUES (1, 'admin', 'x', 1)")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def _setup(db):
    db.execute("INSERT INTO vehicles (id, plate, name, device) VALUES (1, 'AB-C 1', 'Star', 'dev_star')")
    db.execute("INSERT INTO vehicles (id, plate) VALUES (9, 'Star')")
    db.execute("INSERT INTO trips (device, start_time, end_time, distance_km) "
               "VALUES ('dev_star', '2024-01-01T10:00', '2024-01-01T11:00', 10)")
    db.execute("INSERT INTO charge_sessions (is_external, vehicle_plate, start_time, end_time, total_kwh) "
               "VALUES (1, 'Star', '2024-01-02T10:00', '2024-01-02T11:00', 5)")
    db.commit()


def test_preview_counts_and_fields(db):
    _setup(db)
    p = vehicle_trash.preview(db, 9)
    assert (p["name"], p["plate"], p["device"], p["trips"], p["charges"]) == ("", "Star", "", 0, 1)
    real = vehicle_trash.preview(db, 1)
    assert real["trips"] == 1 and real["charges"] == 0
    assert vehicle_trash.preview(db, 404) is None


def test_trash_keeps_data_and_restores(db):
    _setup(db)
    db.execute("INSERT INTO vehicle_batteries (vehicle_id, installed_at, nominal_kwh) VALUES (1, '2024-01-01', 77)")
    db.commit()
    with db:
        assert vehicle_trash.trash(db, 1, "admin")
    assert db.execute("SELECT COUNT(*) FROM vehicles WHERE id = 1").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM trips").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM vehicle_batteries").fetchone()[0] == 0
    rows = vehicle_trash.listing(db)
    assert len(rows) == 1 and rows[0]["plate"] == "AB-C 1" and rows[0]["days_left"] == 14
    with db:
        assert vehicle_trash.restore(db, rows[0]["id"]) is None
    v = db.execute("SELECT * FROM vehicles WHERE id = 1").fetchone()
    assert (v["plate"], v["name"], v["device"]) == ("AB-C 1", "Star", "dev_star")
    assert db.execute("SELECT nominal_kwh FROM vehicle_batteries WHERE vehicle_id = 1").fetchone()[0] == 77
    assert vehicle_trash.listing(db) == []


def test_restore_refuses_taken_plate(db):
    _setup(db)
    with db:
        vehicle_trash.trash(db, 9)
        db.execute("INSERT INTO vehicles (plate) VALUES ('Star')")
    tid = vehicle_trash.listing(db)[0]["id"]
    with db:
        assert vehicle_trash.restore(db, tid) == "plate_taken"


def test_old_entries_purged(db):
    _setup(db)
    with db:
        vehicle_trash.trash(db, 9)
    old = (datetime.now(timezone.utc) - timedelta(days=15)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("UPDATE vehicle_trash SET deleted_at = ?", (old,))
    db.commit()
    assert vehicle_trash.listing(db) == []


def test_api_preview_message_and_delete(client, db):
    _setup(db)
    r = client.get("/api/vehicles/9/delete-preview")
    msg = r.get_json()["message"]
    assert "Star" in msg and "0" in msg and "1" in msg
    assert client.delete("/api/vehicles/9").get_json()["ok"]
    rows = client.get("/api/vehicles/trash").get_json()
    assert len(rows) == 1
    assert client.post(f"/api/vehicles/trash/{rows[0]['id']}/restore").get_json()["ok"]
    assert db.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 2
