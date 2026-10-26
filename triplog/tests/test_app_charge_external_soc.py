"""External charges know their SoC (1.2.142-dev).

Asked for: "When IDMate detects start and end SoC of an external charge, it
would be nice if it prefilled the fields." Before, the manual form had no
SoC fields at all, the POST stored none, and the auto-detection wrote the
pair only into the note. Now the trips before and after the charge window
give SoC start/end (and the odometer) — as a prefill in the form, as the
fallback when the form leaves them empty, and as columns on auto-detected
sessions."""

from datetime import datetime, timedelta

import pytest


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'dev1')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) "
               "VALUES (1, 'admin', 'x', 1, 1)")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def _trip(db, start, end, soc_start, soc_end, odo_start, odo_end):
    db.execute(
        """INSERT INTO trips (device, start_time, end_time, soc_start, soc_end, odo_start, odo_end,
                              distance_km, energy_kwh)
           VALUES ('dev1', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (start, end, soc_start, soc_end, odo_start, odo_end, odo_end - odo_start, 3.0))


def _rahmen(db):
    # Parked at 35 %, drove off with 80 % two hours later: a charge in between.
    _trip(db, "2024-06-01T08:00:00", "2024-06-01T10:00:00", 60, 35, 900, 1000)
    _trip(db, "2024-06-01T12:00:00", "2024-06-01T13:00:00", 80, 70, 1000, 1050)
    db.commit()


def test_hint_takes_soc_and_odometer_from_the_neighbouring_trips(client, db, app_module):
    _rahmen(db)
    d = client.get("/api/charge/external/hint?start=2024-06-01T10:30&end=2024-06-01T11:30").get_json()
    assert d["soc_start"] == 35 and d["soc_end"] == 80 and d["odometer"] == 1000
    cap = app_module.get_bat_kwh(db, "dev1")
    assert d["kwh_hint"] == round(0.45 * cap, 1)


def test_hint_stays_empty_without_trips(client):
    d = client.get("/api/charge/external/hint?start=2024-06-01T10:30").get_json()
    assert d == {"soc_start": None, "soc_end": None, "odometer": None, "kwh_hint": None}


def test_saving_without_soc_falls_back_to_the_trips(client, db):
    _rahmen(db)
    r = client.post("/api/charge/external", json={
        "vehicle_plate": "EV1", "kwh": 38.0,
        "timestamp": "2024-06-01 10:30", "end_time": "2024-06-01 11:30"})
    assert r.get_json()["ok"]
    row = db.execute("SELECT soc_start, soc_end, odometer FROM charge_sessions WHERE is_external = 1").fetchone()
    assert (row["soc_start"], row["soc_end"], row["odometer"]) == (35, 80, 1000)


def test_typed_soc_wins_over_the_trips(client, db):
    _rahmen(db)
    client.post("/api/charge/external", json={
        "vehicle_plate": "EV1", "kwh": 38.0, "soc_start": "40", "soc_end": 85,
        "timestamp": "2024-06-01 10:30", "end_time": "2024-06-01 11:30"})
    row = db.execute("SELECT soc_start, soc_end FROM charge_sessions WHERE is_external = 1").fetchone()
    assert (row["soc_start"], row["soc_end"]) == (40, 85)


def test_nonsense_soc_is_dropped_not_stored(client, db):
    r = client.post("/api/charge/external", json={
        "vehicle_plate": "EV1", "kwh": 10.0, "soc_start": 140, "soc_end": "abc",
        "timestamp": "2024-06-01 10:30", "end_time": "2024-06-01 11:30"})
    assert r.get_json()["ok"]
    row = db.execute("SELECT soc_start, soc_end FROM charge_sessions WHERE is_external = 1").fetchone()
    assert row["soc_start"] is None and row["soc_end"] is None


def test_auto_detected_external_charge_stores_its_soc_pair(client, db, app_module):
    import blueprints.charges as charges
    jetzt = datetime.now().replace(microsecond=0)
    a0, a1 = jetzt - timedelta(days=2, hours=4), jetzt - timedelta(days=2, hours=2)
    b0, b1 = jetzt - timedelta(days=2), jetzt - timedelta(days=1, hours=23)
    _trip(db, a0.isoformat(), a1.isoformat(), 60, 35, 900, 1000)
    _trip(db, b0.isoformat(), b1.isoformat(), 80, 70, 1000, 1050)
    db.commit()
    with app_module.app.test_request_context():
        assert charges.detect_external_from_trips(db) == 1
    row = db.execute("SELECT soc_start, soc_end, total_kwh, note FROM charge_sessions WHERE is_external = 1").fetchone()
    assert (row["soc_start"], row["soc_end"]) == (35, 80)
    assert row["total_kwh"] > 0 and "35" in row["note"] and "80" in row["note"]


def test_form_has_soc_fields_wired_to_the_hint(client):
    html = client.get("/charges").get_data(as_text=True)
    assert 'id="extSocStart"' in html and 'id="extSocEnd"' in html
    assert html.count('data-onchange="extZeitenGeaendert"') == 2
    assert "/api/charge/external/hint?start=" in html
    assert "soc_start: document.getElementById('extSocStart').value || null" in html
    # A typed value is never overwritten by a later hint.
    fn = html.split("async function extZeitenGeaendert", 1)[1].split("_addExternalPending", 1)[0]
    assert "if (cur && cur !== (el.dataset.auto || '')) return false;" in fn
