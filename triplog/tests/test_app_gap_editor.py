"""Gap editor for a telemetry outage: the trip after the gap must not count
the gap km (the detector counted from the previous trip's end), the filled-in
trips get their own times, SoC and GPS, the neighbour trips can be corrected,
and a later scan does not move a corrected side again."""

import pytest

import detector


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) "
               "VALUES (1, 'admin', 'x', 1, 1)")
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'dev1')")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def _trip(db, st, et, odo_s, odo_e, dist=None, soc=(80.0, 70.0), pos=((53.0, 10.0), (53.1, 10.1))):
    cur = db.execute(
        """INSERT INTO trips (device, start_time, end_time, odo_start, odo_end, distance_km,
               soc_start, soc_end, energy_kwh, start_lat, start_lon, end_lat, end_lon, is_manual)
           VALUES ('dev1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (st, et, odo_s, odo_e, dist if dist is not None else odo_e - odo_s, soc[0], soc[1],
         (soc[0] - soc[1]) * 0.8, pos[0][0], pos[0][1], pos[1][0], pos[1][1]))
    db.commit()
    return cur.lastrowid


def _gap(client):
    gaps = client.get("/api/admin/gaps?vehicle=EV1").get_json()["gaps"]
    return next(g for g in gaps if g["prev_trip_id"])


def _setup_outage(db):
    """Trip before ends at 1000 km; after a 210 km outage the next trip runs
    1210 → 1230 but (old detector) counts 1000 → 1230 = 230 km."""
    prev_id = _trip(db, "2026-10-01T08:00:00", "2026-10-01T09:00:00", 950.0, 1000.0)
    next_id = _trip(db, "2026-10-03T17:00:00", "2026-10-03T17:30:00", 1210.0, 1230.0,
                    dist=230.0, soc=(60.0, 56.0))
    db.execute("INSERT INTO charge_sessions (vehicle_plate, start_time, end_time, is_external, "
               "total_kwh, odometer, soc_start, soc_end) VALUES "
               "('EV1', '2026-10-02T12:00:00', '2026-10-02T13:00:00', 1, 30.0, 1100, 40, 80)")
    db.commit()
    return prev_id, next_id


def test_gap_lists_double_counted_km_and_charges(client, db):
    _setup_outage(db)
    g = _gap(client)
    assert g["km"] == 210.0
    assert g["in_next_trip_km"] == 210.0
    assert [c["odometer"] for c in g["charges"]] == [1100]
    assert g["next_trip"]["soc_start"] == 60.0 and g["prev_trip"]["odo_end"] == 1000.0


def test_save_fills_trips_and_fixes_next_trip(client, db):
    prev_id, next_id = _setup_outage(db)
    r = client.post("/api/admin/gaps", json={
        "vehicle": "EV1", "from_km": 1000.0, "to_km": 1210.0,
        "prev": {"id": prev_id},
        "next": {"id": next_id, "start_time": "2026-10-03T17:05", "soc_start": "61"},
        "splits": [
            {"km": 100, "start_time": "2026-10-01T10:00", "end_time": "2026-10-01T11:30",
             "soc_start": 70, "soc_end": 50, "gps": "53.60308082178861, 9.888429999859472",
             "purpose": "Dienstfahrt"},
            {"km": 110, "start_time": "2026-10-02T14:00", "end_time": "2026-10-02T15:40",
             "soc_start": 80, "soc_end": 61},
        ]})
    assert r.status_code == 200, r.get_json()
    nxt = db.execute("SELECT * FROM trips WHERE id = ?", (next_id,)).fetchone()
    assert nxt["distance_km"] == 20.0                 # 1230 − 1210, gap km gone
    assert nxt["start_time"] == "2026-10-03T17:05:00"
    assert nxt["soc_start"] == 61.0 and nxt["manual_fields"] == "start"
    assert db.execute("SELECT manual_fields FROM trips WHERE id = ?", (prev_id,)).fetchone()[0] is None

    rows = db.execute("SELECT * FROM trips WHERE is_manual = 1 ORDER BY odo_start").fetchall()
    assert [(t["odo_start"], t["odo_end"]) for t in rows] == [(1000.0, 1100.0), (1100.0, 1210.0)]
    first, second = rows
    assert first["start_time"] == "2026-10-01T10:00:00" and first["end_time"] == "2026-10-01T11:30:00"
    assert (first["start_lat"], first["start_lon"]) == (53.1, 10.1)            # trip before ends here
    assert (first["end_lat"], first["end_lon"]) == (53.60308082178861, 9.888429999859472)
    assert (second["start_lat"], second["end_lat"]) == (53.60308082178861, 53.0)  # ends where next starts
    assert first["energy_kwh"] and first["consumption"]
    assert first["manual_fields"] == "end,start"

    # the gap stays listed, now fully assigned and no longer double counted
    g = _gap(client)
    assert g["assigned"] and g["in_next_trip_km"] == 0


def test_save_rejects_wrong_sum_and_bad_gps(client, db):
    prev_id, next_id = _setup_outage(db)
    base = {"vehicle": "EV1", "prev": {"id": prev_id}, "next": {"id": next_id}}
    r = client.post("/api/admin/gaps", json={**base, "splits": [
        {"km": 200, "start_time": "2026-10-01T10:00", "end_time": "2026-10-01T12:00"}]})
    assert r.status_code == 400 and "210" in r.get_json()["error"]
    r = client.post("/api/admin/gaps", json={**base, "splits": [
        {"km": 210, "start_time": "2026-10-01T10:00", "end_time": "2026-10-01T12:00", "gps": "Hamburg"}]})
    assert r.status_code == 400
    assert db.execute("SELECT COUNT(*) FROM trips WHERE is_manual = 1").fetchone()[0] == 0


def test_corrected_next_odo_start_moves_the_gap(client, db):
    prev_id, next_id = _setup_outage(db)
    r = client.post("/api/admin/gaps", json={
        "vehicle": "EV1", "prev": {"id": prev_id}, "next": {"id": next_id, "odo_start": "1205"},
        "splits": [{"km": 205, "start_time": "2026-10-01T10:00", "end_time": "2026-10-01T14:00"}]})
    assert r.status_code == 200, r.get_json()
    assert db.execute("SELECT distance_km FROM trips WHERE id = ?", (next_id,)).fetchone()[0] == 25.0


def test_scan_keeps_corrected_start(client, db):
    prev_id, next_id = _setup_outage(db)
    client.post("/api/admin/gaps", json={
        "vehicle": "EV1", "prev": {"id": prev_id},
        "next": {"id": next_id, "start_time": "2026-10-03T17:05"},
        "splits": [{"km": 210, "start_time": "2026-10-01T10:00", "end_time": "2026-10-01T14:00"}]})
    # a rescan sees the next trip again, starting earlier
    detector.save_trips(db, [{
        "device": "dev1", "start_time": "2026-10-03T16:50:00", "end_time": "2026-10-03T17:30:00",
        "start_lat": 53.0, "start_lon": 10.0, "end_lat": 53.1, "end_lon": 10.1,
        "odo_start": 1209.0, "odo_end": 1230.0, "distance_km": 21.0, "soc_start": 62.0,
        "soc_end": 56.0, "energy_kwh": 4.8, "consumption": None, "kw_start": None, "kw_end": None}])
    nxt = db.execute("SELECT start_time, odo_start FROM trips WHERE id = ?", (next_id,)).fetchone()
    assert nxt["start_time"] == "2026-10-03T17:05:00" and nxt["odo_start"] == 1210.0


def test_detector_keeps_outage_km_out_of_the_new_trip(app_module, db):
    db.execute("INSERT INTO vehicles (plate, device) VALUES ('EV1', 'dev1')")
    db.commit()
    _trip(db, "2026-10-01T08:00:00", "2026-10-01T09:00:00", 950.0, 1000.0)
    base = {"device": "dev1", "start_lat": 53.0, "start_lon": 10.0, "end_lat": 53.1, "end_lon": 10.1,
            "soc_start": 60.0, "soc_end": 56.0, "energy_kwh": 3.2, "consumption": None,
            "kw_start": None, "kw_end": None}
    # 210 km jump: outage → only the trip's own 20 km
    detector.save_trips(db, [{**base, "start_time": "2026-10-03T17:00:00", "end_time": "2026-10-03T17:30:00",
                              "odo_start": 1210.0, "odo_end": 1230.0, "distance_km": 20.0}])
    # 2 km late first odometer sample: still counted from the previous end
    detector.save_trips(db, [{**base, "start_time": "2026-10-04T08:00:00", "end_time": "2026-10-04T08:30:00",
                              "odo_start": 1232.0, "odo_end": 1250.0, "distance_km": 18.0}])
    km = [r[0] for r in db.execute(
        "SELECT distance_km FROM trips WHERE start_time >= '2026-10-03' ORDER BY start_time")]
    assert km == [20.0, 20.0]


def test_trips_list_marks_filled_trips_with_asterisk(client, db):
    prev_id, next_id = _setup_outage(db)
    client.post("/api/admin/gaps", json={
        "vehicle": "EV1", "prev": {"id": prev_id}, "next": {"id": next_id},
        "splits": [{"km": 210, "start_time": "2026-10-01T10:00", "end_time": "2026-10-01T11:00"}]})
    html = client.get("/trips?from=2026-09-30&to=2026-10-05").get_data(as_text=True)
    assert "Zwischengepflegt: nachgetragen und eingemittelt" in html
    assert "Zeiten nicht plausibel: Ø 210 km/h ist zu schnell" in html
    assert "trip-manual-mark" not in html


def test_options_count_usage_of_the_last_30_days(client, db):
    from datetime import datetime, timedelta
    recent = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%dT08:00:00")
    old = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%dT08:00:00")
    for st, dest in ((recent, "Kunde B"), (recent, "Kunde B"), (recent, "Kunde A"), (old, "Hafen")):
        db.execute("INSERT INTO trips (device, start_time, end_time, destination, purpose, is_manual) "
                   "VALUES ('dev1', ?, ?, ?, 'Dienstfahrt', 0)", (st, st, dest))
    db.commit()
    use = client.get("/api/fahrtenbuch/options").get_json()["usage_30d"]
    assert use["destination"] == {"Kunde B": 2, "Kunde A": 1}
    assert use["purpose"] == {"Dienstfahrt": 3}


def test_charge_saved_by_start_time_after_rebuild_changed_its_id(client, db):
    prev_id, next_id = _setup_outage(db)
    old_id = db.execute("SELECT id FROM charge_sessions").fetchone()[0]
    db.execute("UPDATE charge_sessions SET id = id + 100")   # rebuild: new ids
    db.commit()
    r = client.post("/api/admin/gaps", json={
        "vehicle": "EV1", "prev": {"id": prev_id}, "next": {"id": next_id},
        "charges": [{"id": old_id, "start_time": "2026-10-02T12:00:00", "odometer": "1105", "soc_start": "38"}],
        "splits": [{"km": 210, "start_time": "2026-10-01T10:00", "end_time": "2026-10-01T14:00"}]})
    assert r.status_code == 200 and r.get_json()["charges_saved"] == 1
    c = db.execute("SELECT odometer, soc_start, manual_fields FROM charge_sessions").fetchone()
    assert (c["odometer"], c["soc_start"], c["manual_fields"]) == (1105.0, 38.0, "odometer,soc_start")


def test_unknown_charge_aborts_without_writing(client, db):
    prev_id, next_id = _setup_outage(db)
    r = client.post("/api/admin/gaps", json={
        "vehicle": "EV1", "prev": {"id": prev_id}, "next": {"id": next_id},
        "charges": [{"id": 999, "start_time": "2026-09-01T12:00:00", "odometer": "1105"}],
        "splits": [{"km": 210, "start_time": "2026-10-01T10:00", "end_time": "2026-10-01T14:00"}]})
    assert r.status_code == 400 and "nicht gefunden" in r.get_json()["error"]
    assert db.execute("SELECT COUNT(*) FROM trips WHERE is_manual = 1").fetchone()[0] == 0
