"""Tests for update_charge_session's derived-cost consistency.

Editing total_kwh on an automatic (wallbox) session must re-derive
cost_pauschale — the charge print's home EUR/100km reads that column, and a
stale one diverges from the corrected energy. External sessions keep their
user-entered cost_total, and sessions without a flat-rate cost (NULL) keep
the print's kwh × tariff fallback.
"""

import pytest


@pytest.fixture
def client(app_module, db):
    db.execute(
        "INSERT INTO users (id, username, password_hash, is_admin) "
        "VALUES (1, 'admin', 'x', 1)")
    db.commit()
    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def _seed_session(db, app_module):
    db.execute("INSERT INTO vehicles (plate, device) VALUES ('EV1', '')")
    for ts, kwh, ms, me in [
        ("2024-06-01T10:00:00", 2.0, 1000.0, 1002.0),
        ("2024-06-01T10:15:00", 2.0, 1002.0, 1004.0),
        ("2024-06-01T10:30:00", 2.0, 1004.0, 1006.0),
    ]:
        db.execute(
            "INSERT INTO charge_readings (timestamp, vehicle_plate, "
            "meter_start, meter_end, kwh) VALUES (?, 'EV1', ?, ?, ?)",
            (ts, ms, me, kwh))
    db.commit()
    app_module.rebuild_charge_sessions(db)
    return db.execute("SELECT * FROM charge_sessions").fetchone()


class TestSessionKwhEdit:
    def test_total_kwh_edit_rederives_cost_pauschale(self, client, db, app_module):
        s = _seed_session(db, app_module)
        assert s["cost_pauschale"] == pytest.approx(6.0 * 0.34)

        r = client.post("/api/charge/sessions/%d" % s["id"], json={"total_kwh": 12.0})
        assert r.status_code == 200 and r.get_json()["ok"]

        after = db.execute("SELECT * FROM charge_sessions WHERE id = ?",
                           (s["id"],)).fetchone()
        assert after["total_kwh"] == 12.0
        assert after["cost_pauschale"] == pytest.approx(12.0 * 0.34)
        assert after["cost_diff"] == pytest.approx(
            after["cost_pauschale"] - after["cost_tibber"])

    def test_external_session_cost_pauschale_untouched(self, client, db):
        db.execute(
            "INSERT INTO charge_sessions (is_external, vehicle_plate, start_time, "
            "end_time, total_kwh, cost_total) "
            "VALUES (1, 'EV1', '2024-06-01T09:00:00', '2024-06-01T09:30:00', 20.0, 12.0)")
        db.commit()
        sid = db.execute("SELECT id FROM charge_sessions").fetchone()["id"]

        r = client.post("/api/charge/sessions/%d" % sid, json={"total_kwh": 25.0})
        assert r.status_code == 200 and r.get_json()["ok"]

        after = db.execute("SELECT * FROM charge_sessions WHERE id = ?",
                           (sid,)).fetchone()
        assert after["total_kwh"] == 25.0
        assert after["cost_pauschale"] is None  # fallback kwh × tariff stays
        assert after["cost_total"] == 12.0      # user-entered receipt untouched
