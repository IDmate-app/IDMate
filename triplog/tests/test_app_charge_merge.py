"""A car plugged in for days: the >12 h gap rule splits one plug-in into
several charges. Merging joins them, survives every rebuild (stored as a
bridge between readings) and keeps the old numbers visible as a range on
the merged charge, so the invoice shows no hole. Undo gives the parts their
numbers back."""

import pytest


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin) VALUES (1, 'admin', 'x', 1)")
    db.execute("INSERT INTO vehicles (plate, device) VALUES ('EV1', '')")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def _readings(db, tag, meter):
    for h in ("10:00", "10:15", "10:30"):
        db.execute("INSERT INTO charge_readings (timestamp, vehicle_plate, meter_start, meter_end, kwh) "
                   "VALUES (?, 'EV1', ?, ?, 2.0)", (f"{tag}T{h}:00", meter, meter + 2.0))
        meter += 2.0
    db.commit()
    return meter


def _sessions(db):
    return [dict(r) for r in db.execute(
        "SELECT id, session_number, start_time, end_time, total_kwh FROM charge_sessions ORDER BY datetime(start_time)")]


def _seed(db, app_module):
    m = _readings(db, "2024-06-01", 1000.0)      # day 1
    m = _readings(db, "2024-06-02", m)           # day 2: gap > 12 h -> own charge
    m = _readings(db, "2024-06-03", m)           # day 3
    _readings(db, "2024-06-10", m)               # a week later: stays apart
    app_module.rebuild_charge_sessions(db)
    s = _sessions(db)
    assert [x["session_number"] for x in s] == ["1", "2", "3", "4"]
    return s


def test_merge_keeps_the_numbers_as_a_range_and_survives_rebuilds(client, db, app_module):
    s = _seed(db, app_module)
    res = client.post("/api/charge/sessions/merge", json={"ids": [s[0]["id"], s[1]["id"], s[2]["id"]]})
    assert res.status_code == 200, res.get_data(as_text=True)
    assert res.get_json()["session_number"] == "1-3"
    nach = _sessions(db)
    assert [x["session_number"] for x in nach] == ["1-3", "4"]
    assert nach[0]["start_time"].startswith("2024-06-01T10:") and nach[0]["end_time"].startswith("2024-06-03T10:")
    assert nach[0]["total_kwh"] == 18.0
    # a full rebuild (and every webhook rebuild) keeps the merge
    app_module.rebuild_charge_sessions(db)
    assert [x["session_number"] for x in _sessions(db)] == ["1-3", "4"]
    app_module.rebuild_charge_sessions(db, since="2024-06-02T00:00:00")
    assert [x["session_number"] for x in _sessions(db)] == ["1-3", "4"]
    # the list marks it so the bar offers the undo
    html = client.get("/charges?from=2024-05-01&to=2024-07-01").get_data(as_text=True)
    assert 'data-merged="1"' in html and "1-3" in html
    # undo: three charges again with their old numbers
    merged_id = _sessions(db)[0]["id"]
    res = client.post(f"/api/charge/sessions/{merged_id}/unmerge", json={})
    assert res.status_code == 200 and len(res.get_json()["ids"]) == 3
    assert [x["session_number"] for x in _sessions(db)] == ["1", "2", "3", "4"]
    assert client.post(f"/api/charge/sessions/{_sessions(db)[0]['id']}/unmerge", json={}).status_code == 409


def test_merge_refuses_gaps_in_the_selection_and_billed_charges(client, db, app_module):
    s = _seed(db, app_module)
    assert client.post("/api/charge/sessions/merge", json={"ids": [s[0]["id"]]}).status_code == 400
    res = client.post("/api/charge/sessions/merge", json={"ids": [s[0]["id"], s[2]["id"]]})   # 2 lies between
    assert res.status_code == 409 and "aufeinanderfolgende" in res.get_json()["error"]
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('charge_billed_until', '2024-06-05')")
    db.commit()
    assert client.post("/api/charge/sessions/merge", json={"ids": [s[0]["id"], s[1]["id"]]}).status_code == 409
    assert [x["session_number"] for x in _sessions(db)] == ["1", "2", "3", "4"]


def test_numbers_stay_put_across_a_scoped_rebuild(client, db, app_module):
    """The webhook rebuilds from the newest reading on; the charges in that
    range used to come back renumbered (holes like 245 and 250 in the list)."""
    s = _seed(db, app_module)
    app_module.rebuild_charge_sessions(db, since="2024-06-02T00:00:00")
    assert [x["session_number"] for x in _sessions(db)] == ["1", "2", "3", "4"]
    app_module.rebuild_charge_sessions(db)
    assert [x["session_number"] for x in _sessions(db)] == ["1", "2", "3", "4"]
    # a new charge after the others gets the next number
    _readings(db, "2024-06-12", 2000.0)
    app_module.rebuild_charge_sessions(db, since="2024-06-12T00:00:00")
    assert [x["session_number"] for x in _sessions(db)] == ["1", "2", "3", "4", "5"]


def test_two_vehicles_share_one_numbering_and_keep_it(client, db, app_module):
    """Numbers run across all vehicles (a second car's charges are the holes in
    one car's list). Rebuilds keep them, and merging one car's charges around
    the other car's charge labels exactly the absorbed numbers."""
    db.execute("INSERT INTO vehicles (plate, device) VALUES ('EV2', '')")
    db.commit()
    m1 = _readings(db, "2024-06-01", 1000.0)                      # EV1
    for h in ("18:00", "18:15"):                                  # EV2 the same evening
        db.execute("INSERT INTO charge_readings (timestamp, vehicle_plate, meter_start, meter_end, kwh) "
                   "VALUES (?, 'EV2', 5000.0, 5002.0, 2.0)", (f"2024-06-01T{h}:00",))
    db.commit()
    m1 = _readings(db, "2024-06-02", m1)                          # EV1 again
    _readings(db, "2024-06-03", m1)                               # EV1
    app_module.rebuild_charge_sessions(db)
    alle = [dict(r) for r in db.execute(
        "SELECT id, vehicle_plate, session_number FROM charge_sessions ORDER BY datetime(start_time)")]
    assert [(x["vehicle_plate"], x["session_number"]) for x in alle] == [("EV1", "1"), ("EV2", "2"), ("EV1", "3"), ("EV1", "4")]
    app_module.rebuild_charge_sessions(db, since="2024-06-01T12:00:00")
    assert [x["session_number"] for x in db.execute("SELECT session_number FROM charge_sessions ORDER BY datetime(start_time)")] == ["1", "2", "3", "4"]
    # EV1's first two charges are consecutive for EV1 although EV2 charged in between
    # (ids are fresh after a rebuild, the page reloads them too)
    ev1 = [dict(r) for r in db.execute(
        "SELECT id FROM charge_sessions WHERE vehicle_plate = 'EV1' ORDER BY datetime(start_time)")]
    res = client.post("/api/charge/sessions/merge", json={"ids": [ev1[0]["id"], ev1[1]["id"]]})
    assert res.status_code == 200 and res.get_json()["session_number"] == "1,3"
    nach = [(x["vehicle_plate"], x["session_number"]) for x in db.execute(
        "SELECT vehicle_plate, session_number FROM charge_sessions ORDER BY datetime(start_time)")]
    assert nach == [("EV1", "1,3"), ("EV2", "2"), ("EV1", "4")]
    app_module.rebuild_charge_sessions(db)
    assert [x[1] for x in db.execute("SELECT vehicle_plate, session_number FROM charge_sessions ORDER BY datetime(start_time)")] == ["1,3", "2", "4"]
    # EV2's charge is untouched: still one charge of 4 kWh
    ev2 = db.execute("SELECT total_kwh, session_number FROM charge_sessions WHERE vehicle_plate = 'EV2'").fetchall()
    assert len(ev2) == 1 and ev2[0]["total_kwh"] == 4.0 and ev2[0]["session_number"] == "2"
    # undo restores 1 and 3
    mid = db.execute("SELECT id FROM charge_sessions WHERE session_number = '1,3'").fetchone()["id"]
    assert client.post(f"/api/charge/sessions/{mid}/unmerge", json={}).status_code == 200
    assert [x[0] for x in db.execute("SELECT session_number FROM charge_sessions ORDER BY datetime(start_time)")] == ["1", "2", "3", "4"]
