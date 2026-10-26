"""Capacity trend uses only measured energy, and the TeslaMate marker
survives the charge recalc (its kWh are battery side)."""

import logging

import pytest

import migrations


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin) VALUES (1, 'admin', 'x', 1)")
    db.execute("INSERT INTO vehicles (id, plate, device, battery_capacity_kwh) VALUES (1, 'EV1', 'dev1', 77)")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def _cs(db, start, kwh, ext=0, number=None, cost=None, note=None):
    db.execute("INSERT INTO charge_sessions (vehicle_plate, start_time, end_time, total_kwh, "
               "soc_start, soc_end, is_external, session_number, cost_total, note) "
               "VALUES ('EV1', ?, ?, ?, 30, 80, ?, ?, ?, ?)",
               (start, start[:11] + "23:00:00", kwh, ext, number, cost, note))
    db.commit()


def test_capacity_points_skip_estimated_externals(app_module, db, client):
    _cs(db, "2026-01-05T10:00:00", 42.0)                         # wallbox: 42 × 0.9 / 0.5
    _cs(db, "2026-02-05T10:00:00", 36.0, ext=1)                  # auto-detected estimate
    _cs(db, "2026-03-05T10:00:00", 40.0, ext=1, cost=20.0)       # priced receipt
    _cs(db, "2026-04-05T10:00:00", 37.5, ext=1, number="TM-9")   # TeslaMate, battery side
    pts, _ = app_module._capacity_points(db, "EV1")
    assert [p["date"][:7] for p in pts] == ["2026-01", "2026-03", "2026-04"]
    assert pts[-1]["cap"] == pytest.approx(75.0, abs=0.1)        # TM not × efficiency


def test_recalc_keeps_teslamate_marker(db, client):
    _cs(db, "2026-04-05T10:00:00", 37.5, ext=1, number="TM-9")
    _cs(db, "2026-04-06T10:00:00", 20.0, ext=1, number="X1")
    assert client.post("/api/charge/recalc").status_code == 200
    nums = [r[0] for r in db.execute("SELECT session_number FROM charge_sessions ORDER BY start_time")]
    assert nums == ["TM-9", None]


def test_migration_restores_lost_markers(db):
    _cs(db, "2026-04-05T10:00:00", 37.5, ext=1, note="TeslaMate import (kwh_used=41, kw_max=11)")
    _cs(db, "2026-04-06T10:00:00", 20.0, ext=1, note="Ionity")
    migrations.m063_restore_tm_markers(db, logging.getLogger("t"))
    nums = [r[0] for r in db.execute("SELECT session_number FROM charge_sessions ORDER BY start_time")]
    assert nums[0].startswith("TM-") and nums[1] is None
