"""Diagnostics: the trip / charge behind a finding can be fixed in place,
deleted, or marked as checked (then it no longer counts)."""

import pytest


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) VALUES (1, 'a', 'x', 1, 1)")
    db.execute("INSERT INTO vehicles (id, plate, device, battery_capacity_kwh) VALUES (1, 'EV1', 'dev1', 80), (2, 'EV2', 'dev2', 50)")
    # a stick dropout: end before start, odometer running backwards after it
    rows = [("2026-02-27T06:20:00", "2026-02-27T07:05:00", 3713, 3716, 3, 60, 58),
            ("2026-02-27T19:09:00", "2026-02-27T07:55:00", 3716, 3760, 44, 58, 45),
            ("2026-02-28T08:00:00", "2026-02-28T09:00:00", 3760, 3800, 40, 45, 35)]
    for st, et, o1, o2, km, s1, s2 in rows:
        db.execute("INSERT INTO trips (device, start_time, end_time, odo_start, odo_end, distance_km, soc_start, soc_end) "
                   "VALUES ('dev2', ?, ?, ?, ?, ?, ?, ?)", (st, et, o1, o2, km, s1, s2))
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def _check(c, key, plate="EV2"):
    return next(x for x in c.get("/api/admin/diagnostics/checks").get_json()["checks"]
                if x["key"] == key and x["vehicle"] == plate)


def test_fix_a_trip_with_its_neighbours(client, db):
    assert _check(client, "trips_negative")["value"] == 1
    d = client.get("/api/admin/diagnostics/trip/2").get_json()
    assert d["prev"]["id"] == 1 and d["trip"]["end_time"] == "2026-02-27T07:55:00"
    r = client.post("/api/admin/diagnostics/trip/2", json={"start_time": "2026-02-27T07:15"}).get_json()
    assert r["ok"] and r["trip"]["start_time"] == "2026-02-27T07:15:00" and r["trip"]["manual_fields"] == "start"
    assert r["trip"]["energy_kwh"] == pytest.approx(13 / 100 * 50, abs=0.01)     # SoC 58 → 45 × 50 kWh
    assert r["trip"]["consumption"] == pytest.approx(6.5 / 44 * 100, abs=0.1)
    assert _check(client, "trips_negative")["value"] == 0
    bad = client.post("/api/admin/diagnostics/trip/2", json={"end_time": "2026-02-27T07:00"})
    assert bad.status_code == 400


def test_mark_as_checked_and_reset(client):
    assert _check(client, "trips_negative")["value"] == 1
    r = client.post("/api/admin/diagnostics/ack", json={"key": "trips_negative", "id": 2}).get_json()
    assert r["ok"] and r["total"] == 1
    d = client.get("/api/admin/diagnostics/checks").get_json()
    assert d["acked"] == 1 and _check(client, "trips_negative")["level"] == "ok"
    client.post("/api/admin/diagnostics/ack", json={"reset": True})
    assert _check(client, "trips_negative")["value"] == 1


def test_delete_a_trip_of_another_vehicle(client, db):
    assert client.delete("/api/admin/diagnostics/trip/2").get_json()["ok"]
    assert db.execute("SELECT COUNT(*) FROM trips WHERE device = 'dev2'").fetchone()[0] == 2


def test_admin_page_has_the_editor(client):
    html = client.get("/admin").get_data(as_text=True)
    assert 'id="dgEditModal"' in html and "dgOpenEdit" in html
