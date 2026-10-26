"""Tests for the reading-edit endpoints' session propagation.

Deleting/editing/inserting a charge reading must propagate into the parent
session's aggregates (total_kwh, cost_pauschale) via a scoped rebuild —
without it the charge print keeps showing the stale, e.g. duplicate-inflated,
session values. Batch flows opt out per call (?rebuild=0) and rebuild once at
the end from the earliest touched timestamp (response field `since`). Readings
before the billed freeze are reported as frozen instead of silently staying
stale behind an "ok".

All timestamps are in 2024 (> 730 days ago) and the vehicle has no device, so
no InfluxDB/trip backend is touched during rebuilds.
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


def _seed(db, app_module, with_duplicate=False):
    """One home charge of 3×2 kWh; optionally + a duplicated HASS reading."""
    db.execute("INSERT INTO vehicles (plate, device) VALUES ('EV1', '')")
    rows = [
        ("2024-06-01T10:00:00", 2.0, 1000.0, 1002.0),
        ("2024-06-01T10:15:00", 2.0, 1002.0, 1004.0),
        ("2024-06-01T10:30:00", 2.0, 1004.0, 1006.0),
    ]
    if with_duplicate:
        rows.append(("2024-06-01T10:16:00", 2.0, 1002.0, 1004.0))
    for ts, kwh, ms, me in rows:
        db.execute(
            "INSERT INTO charge_readings (timestamp, vehicle_plate, "
            "meter_start, meter_end, kwh) VALUES (?, 'EV1', ?, ?, ?)",
            (ts, ms, me, kwh))
    db.commit()
    app_module.rebuild_charge_sessions(db)


def _session(db):
    rows = db.execute("SELECT * FROM charge_sessions").fetchall()
    assert len(rows) == 1
    return rows[0]


def _rid(db, ts):
    return db.execute(
        "SELECT id FROM charge_readings WHERE timestamp = ?", (ts,)
    ).fetchone()["id"]


class TestReadingEditsPropagate:
    def test_delete_reading_rebuilds_session_totals(self, client, db, app_module):
        _seed(db, app_module, with_duplicate=True)
        assert _session(db)["total_kwh"] == 8.0  # inflated by the duplicate

        r = client.delete("/api/charge/readings/%d" % _rid(db, "2024-06-01T10:16:00"))
        body = r.get_json()
        assert r.status_code == 200 and body["ok"]
        assert body.get("rebuilt") is True
        assert body["since"] == "2024-06-01T10:16:00"
        s = _session(db)
        assert s["total_kwh"] == 6.0
        assert s["cost_pauschale"] == pytest.approx(6.0 * 0.34)

    def test_delete_with_optout_skips_rebuild_then_scoped_rebuild_fixes(
            self, client, db, app_module):
        _seed(db, app_module, with_duplicate=True)

        r = client.delete("/api/charge/readings/%d?rebuild=0"
                          % _rid(db, "2024-06-01T10:16:00"))
        body = r.get_json()
        assert body["ok"] and "rebuilt" not in body
        assert body["since"] == "2024-06-01T10:16:00"
        assert _session(db)["total_kwh"] == 8.0  # still stale, as requested

        rb = client.post("/api/charge/rebuild", json={"since": body["since"]})
        rb_body = rb.get_json()
        assert rb.status_code == 200 and rb_body["ok"]
        assert rb_body["blocked"] is False
        assert _session(db)["total_kwh"] == 6.0

    def test_delete_before_billed_freeze_reports_frozen(self, client, db, app_module):
        _seed(db, app_module, with_duplicate=True)
        db.execute("INSERT OR REPLACE INTO settings (key, value) "
                   "VALUES ('charge_billed_until', '2024-12-31')")
        db.commit()

        r = client.delete("/api/charge/readings/%d" % _rid(db, "2024-06-01T10:16:00"))
        body = r.get_json()
        assert body["ok"]
        assert body.get("frozen") is True
        assert body["billed_floor"] == "2025-01-01T00:00:00"
        assert "rebuilt" not in body
        assert _session(db)["total_kwh"] == 8.0  # frozen — deliberately untouched

    def test_rebuild_endpoint_flags_since_before_freeze(self, client, db, app_module):
        _seed(db, app_module)
        db.execute("INSERT OR REPLACE INTO settings (key, value) "
                   "VALUES ('charge_billed_until', '2024-12-31')")
        db.commit()

        rb = client.post("/api/charge/rebuild", json={"since": "2024-06-01T10:00:00"})
        body = rb.get_json()
        assert rb.status_code == 200 and body["ok"]
        assert body["blocked"] is True
        assert body["billed_floor"] == "2025-01-01T00:00:00"

    def test_update_reading_kwh_rebuilds_session(self, client, db, app_module):
        _seed(db, app_module)
        assert _session(db)["total_kwh"] == 6.0

        r = client.post("/api/charge/readings/%d" % _rid(db, "2024-06-01T10:15:00"),
                        json={"kwh": 5.0})
        body = r.get_json()
        assert body["ok"] and body.get("rebuilt") is True
        assert body["since"] == "2024-06-01T10:15:00"
        s = _session(db)
        assert s["total_kwh"] == 9.0
        assert s["cost_pauschale"] == pytest.approx(9.0 * 0.34)

    def test_insert_reading_extends_session(self, client, db, app_module):
        _seed(db, app_module)

        r = client.put("/api/charge/readings", json={
            "timestamp": "2024-06-01T10:45:00", "vehicle_plate": "EV1",
            "meter_start": 1006.0, "meter_end": 1008.0, "kwh": 2.0})
        body = r.get_json()
        assert body["ok"] and body.get("rebuilt") is True
        s = _session(db)
        assert s["total_kwh"] == 8.0
        linked = db.execute(
            "SELECT COUNT(*) AS n FROM charge_readings WHERE session_id = ?",
            (s["id"],)).fetchone()["n"]
        assert linked == 4
