"""Journeys take in every trip of their date range automatically — also trips
detected after the journey was created — while a trip removed by hand stays
out (journey_trip_excluded) until it is ticked again."""

import logging

import pytest

import migrations


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


def _trip(db, start, km=10.0, device="dev1"):
    cur = db.execute("INSERT INTO trips (device, start_time, end_time, distance_km) VALUES (?, ?, ?, ?)",
                     (device, start, start, km))
    db.commit()
    return cur.lastrowid


def _ids(client, jid):
    return [t["id"] for t in client.get(f"/api/journeys/{jid}/trips").get_json()]


def _create(client, **extra):
    body = {"title": "Finnland", "date_from": "2026-07-15T08:00", "date_to": "2026-07-20T18:00", **extra}
    return client.post("/api/journeys", json=body).get_json()["id"]


def test_trips_in_range_join_also_when_detected_later(client, db):
    before = _trip(db, "2026-07-14T09:00:00")          # day before: out
    first = _trip(db, "2026-07-15T06:30:00")           # before 08:00 but same day: in (whole days)
    other_car = _trip(db, "2026-07-16T10:00:00", device="dev2")
    jid = _create(client, excluded_ids=[])
    assert _ids(client, jid) == [first]
    later = _trip(db, "2026-07-18T12:00:00")           # detected after creation
    last_day = _trip(db, "2026-07-20T23:30:00")        # end day counts whole
    after = _trip(db, "2026-07-21T00:10:00")
    assert _ids(client, jid) == [first, later, last_day]
    assert before not in _ids(client, jid) and other_car not in _ids(client, jid) and after not in _ids(client, jid)
    # the list page counts them too
    assert "Finnland" in client.get("/journeys").get_data(as_text=True)


def test_unticked_and_removed_trips_stay_out(client, db):
    a = _trip(db, "2026-07-15T10:00:00")
    b = _trip(db, "2026-07-16T10:00:00")
    jid = _create(client, excluded_ids=[b])
    assert _ids(client, jid) == [a]
    c = _trip(db, "2026-07-17T10:00:00")
    assert _ids(client, jid) == [a, c]                  # new one joins, b stays out
    assert client.delete(f"/api/journeys/{jid}/trips/{c}").get_json()["ok"]
    assert _ids(client, jid) == [a]                     # removed for good
    html = client.get(f"/journeys/{jid}").get_data(as_text=True)
    assert "2 entfernte Fahrt(en)" in html
    # ticking everything again in the edit dialog brings both back
    client.post(f"/api/journeys/{jid}", json={"title": "Finnland", "excluded_ids": []})
    assert _ids(client, jid) == [a, b, c]


def test_legacy_trip_ids_count_unticked_as_removed(client, db):
    a = _trip(db, "2026-07-15T10:00:00")
    b = _trip(db, "2026-07-16T10:00:00")
    jid = _create(client, trip_ids=[a])
    c = _trip(db, "2026-07-17T10:00:00")
    assert _ids(client, jid) == [a, c]
    assert b not in _ids(client, jid)


def test_changing_the_range_moves_the_membership(client, db):
    a = _trip(db, "2026-07-15T10:00:00")
    b = _trip(db, "2026-07-22T10:00:00")
    jid = _create(client, excluded_ids=[])
    client.post(f"/api/journeys/{jid}", json={"date_from": "2026-07-21T00:00", "date_to": "2026-07-23T00:00",
                                              "excluded_ids": []})
    assert _ids(client, jid) == [b]
    assert a not in _ids(client, jid)


def test_migration_keeps_unticked_and_adds_later_trips(app_module, db):
    db.execute("DROP TABLE journey_trip_excluded")
    db.execute("INSERT INTO journeys (id, device, title, date_from, date_to, created_at) "
               "VALUES (7, 'dev1', 'Alt', '2026-07-15', '2026-07-20', '2026-07-15 12:00:00')")
    linked = _trip(db, "2026-07-15T09:00:00")
    unticked = _trip(db, "2026-07-15T10:00:00")
    later = _trip(db, "2026-07-16T10:00:00")
    db.execute("UPDATE trips SET created_at = '2026-07-15 11:00:00' WHERE id IN (?, ?)", (linked, unticked))
    db.execute("UPDATE trips SET created_at = '2026-07-16 11:00:00' WHERE id = ?", (later,))
    db.execute("INSERT INTO journey_trips (journey_id, trip_id) VALUES (7, ?)", (linked,))
    migrations.m054_journey_trip_excluded(db, logging.getLogger("test"))
    excluded = [r[0] for r in db.execute("SELECT trip_id FROM journey_trip_excluded WHERE journey_id = 7")]
    assert excluded == [unticked]
    from blueprints.journeys import sync_journey_trips
    sync_journey_trips(db, journey_id=7)
    members = [r[0] for r in db.execute("SELECT trip_id FROM journey_trips WHERE journey_id = 7 ORDER BY trip_id")]
    assert members == [linked, later]


def test_merge_of_two_removed_trips_stays_out(client, db):
    a = _trip(db, "2026-07-15T10:00:00", km=5.0)
    b = _trip(db, "2026-07-15T10:30:00", km=6.0)
    keep = _trip(db, "2026-07-16T10:00:00")
    jid = _create(client, excluded_ids=[a, b])
    new_id = client.post("/api/trips/merge", json={"ids": [a, b]}).get_json()["id"]
    assert _ids(client, jid) == [keep]
    assert new_id not in _ids(client, jid)


def _detected(start, end, device="dev1", **extra):
    """A trip dict shaped like detect_trips() hands it to save_trips()."""
    t = {"device": device, "start_time": start, "end_time": end,
         "start_lat": 52.0, "start_lon": 13.0, "end_lat": 52.1, "end_lon": 13.1,
         "odo_start": 1000.0, "odo_end": 1042.0, "distance_km": 42.0,
         "soc_start": 80.0, "soc_end": 70.0, "energy_kwh": 7.0, "consumption": 16.7,
         "kw_start": None, "kw_end": None}
    t.update(extra)
    return t


def test_detector_fills_the_journey_without_opening_it(client, db):
    """The trip belongs to the journey the moment detection saves it — no page
    view, no edit dialog. This is what makes a journey planned in advance
    collect its trips on its own."""
    import detector
    jid = _create(client, excluded_ids=[])
    detector.save_trips(db, [_detected("2026-07-17T09:00:00", "2026-07-17T10:00:00")])
    members = [r[0] for r in db.execute(
        "SELECT trip_id FROM journey_trips WHERE journey_id = ?", (jid,))]
    assert len(members) == 1
    # …and a trip outside the period still stays out
    detector.save_trips(db, [_detected("2026-07-25T09:00:00", "2026-07-25T10:00:00")])
    assert len(db.execute("SELECT trip_id FROM journey_trips WHERE journey_id = ?",
                          (jid,)).fetchall()) == 1


def test_detector_respects_a_trip_removed_by_hand(client, db):
    """Re-detection of a removed trip (same window, extended end) must not
    sneak it back into the journey."""
    import detector
    cur = db.execute("INSERT INTO trips (device, start_time, end_time, distance_km) "
                     "VALUES ('dev1', '2026-07-16T10:00:00', '2026-07-16T11:00:00', 10.0)")
    db.commit()
    a = cur.lastrowid
    jid = _create(client, excluded_ids=[a])
    # same window, a bit more data: save_trips extends the existing row
    detector.save_trips(db, [_detected("2026-07-16T10:00:00", "2026-07-16T11:30:00")])
    assert db.execute("SELECT COUNT(*) FROM trips").fetchone()[0] == 1
    assert _ids(client, jid) == []


def test_available_trips_follow_the_journeys_vehicle(client, db):
    """The edit dialog asks with ?journey=<id>: the list must come from the
    journey's vehicle, otherwise ticking a trip the auto-fill cannot link
    would silently do nothing."""
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (2, 'EV2', 'dev2')")
    db.execute("UPDATE users SET active_vehicle_id = 2 WHERE id = 1")
    db.commit()
    mine = _trip(db, "2026-07-16T10:00:00")            # dev1 — the journey's car
    _trip(db, "2026-07-16T12:00:00", device="dev2")    # the active car
    db.execute("INSERT INTO journeys (id, device, title, date_from, date_to) "
               "VALUES (9, 'dev1', 'Finnland', '2026-07-15', '2026-07-20')")
    db.commit()
    got = client.get("/api/journeys/available-trips?from=2026-07-15&to=2026-07-20&journey=9").get_json()
    assert [t["id"] for t in got] == [mine]


def test_restore_takes_every_removed_trip_back_in(client, db):
    """One click instead of ticking each trip: the journey goes back to
    "everything in the period", and later trips keep joining."""
    a = _trip(db, "2026-07-15T10:00:00")
    b = _trip(db, "2026-07-16T10:00:00")
    jid = _create(client, excluded_ids=[a, b])
    assert _ids(client, jid) == []
    res = client.post(f"/api/journeys/{jid}/trips/restore").get_json()
    assert res["restored"] == 2
    assert _ids(client, jid) == [a, b]
    later = _trip(db, "2026-07-18T10:00:00")
    assert _ids(client, jid) == [a, b, later]


def test_orphaned_journey_follows_its_vehicle(client, db):
    """A journey created while the vehicle had no device tag kept the guess
    from config — it matched no trip and no vehicle, so nothing could join it
    automatically. Setting the tag brings the journey along."""
    from journey_sync import repair_orphan_journey_devices
    db.execute("INSERT INTO journeys (id, device, title, date_from, date_to) "
               "VALUES (5, 'id7', 'Finnland', '2026-07-15', '2026-07-20')")
    db.commit()
    a = _trip(db, "2026-07-16T10:00:00")          # dev1, the vehicle's real tag
    assert repair_orphan_journey_devices(db) == 1
    assert db.execute("SELECT device FROM journeys WHERE id = 5").fetchone()[0] == "dev1"
    assert _ids(client, 5) == [a]


def test_repair_keeps_hands_off_when_it_could_lose_trips(client, db):
    """Two vehicles → no way to tell where the journey belongs; a tag that its
    own trips carry → the journey is fine as it is. Both stay untouched."""
    from journey_sync import repair_orphan_journey_devices
    db.execute("INSERT INTO journeys (id, device, title, date_from, date_to) "
               "VALUES (5, 'id7', 'Finnland', '2026-07-15', '2026-07-20')")
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (2, 'EV2', 'dev2')")
    db.commit()
    assert repair_orphan_journey_devices(db) == 0
    db.execute("DELETE FROM vehicles WHERE id = 2")
    _trip(db, "2026-07-16T10:00:00", device="id7")   # the journey's own trips
    assert repair_orphan_journey_devices(db) == 0
    assert db.execute("SELECT device FROM journeys WHERE id = 5").fetchone()[0] == "id7"


def test_detail_grids_use_minmax_on_phones():
    """Regression: ``grid-template-columns:1fr`` in the phone media query lets
    long nowrap addresses inflate the column, so the page opened wider than
    the display in the iOS web app. Both detail pages must use minmax(0,1fr)."""
    import pathlib
    tpl = pathlib.Path(__file__).resolve().parents[1] / "templates"
    for name in ("journey_detail.html", "trip_detail.html"):
        src = (tpl / name).read_text(encoding="utf-8")
        assert ".detail-grid{grid-template-columns:1fr}" not in src, name
        assert "minmax(0,1fr)" in src, name


def test_overview_shows_price_per_kwh(client, db, app_module):
    from blueprints.journeys import preis_je_kwh
    jid = _create(client, excluded_ids=[])
    # average = total cost over total energy; min/max per charge; unpaid charges do not count
    for start, kwh, cost, pausch in (("2026-07-15T10:00:00", 40, 12.0, None),      # 30.0 ct
                                     ("2026-07-16T10:00:00", 20, None, 13.8),      # 69.0 ct (flat)
                                     ("2026-07-17T10:00:00", 30, None, None)):     # free: ignored
        db.execute("INSERT INTO charge_sessions (vehicle_plate, start_time, end_time, total_kwh, cost_total, cost_pauschale) "
                   "VALUES ('EV1', ?, ?, ?, ?, ?)", (start, start, kwh, cost, pausch))
    db.commit()
    rows = db.execute("SELECT * FROM charge_sessions").fetchall()
    p = preis_je_kwh(rows)
    assert p == {"avg": 43.0, "min": 30.0, "max": 69.0, "n": 2}
    html = client.get(f"/journeys/{jid}").get_data(as_text=True)
    assert "Preis je kWh" in html
    assert "⌀ 43.0 ct · min 30.0 · max 69.0" in html      # the first day's charge counts too
    assert "(3x)" in html
    # each charge row carries its own ct/kWh; a free charge shows none
    assert '<span class="jd-ct">30.0 ct</span>' in html and '<span class="jd-ct">69.0 ct</span>' in html
    assert html.count('<span class="jd-ct"></span>') == 1
    assert preis_je_kwh([]) is None


def test_gpx_export_is_valid_xml_with_a_special_title(client, db):
    import xml.etree.ElementTree as ET
    db.execute("INSERT INTO trips (device, start_time, end_time, distance_km, start_lat, start_lon, end_lat, end_lon) "
               "VALUES ('dev1', '2026-07-15T10:00:00', '2026-07-15T11:00:00', 50, 52.5, 13.4, 53.0, 12.0)")
    db.commit()
    jid = client.post("/api/journeys", json={"title": "Finnland & Schweden <2026>", "date_from": "2026-07-15T08:00",
                                             "date_to": "2026-07-20T18:00", "excluded_ids": []}).get_json()["id"]
    res = client.get(f"/api/journeys/{jid}/gpx")
    assert res.status_code == 200 and res.mimetype == "application/gpx+xml"
    assert res.headers["Content-Disposition"] == "attachment; filename=journey_Finnland_Schweden_2026_2026-07-15_2026-07-20.gpx"
    wurzel = ET.fromstring(res.data)                       # parses: the & and <> are escaped
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    assert wurzel.find("g:metadata/g:name", ns).text == "Finnland & Schweden <2026>"
    punkte = wurzel.findall("g:trk/g:trkseg/g:trkpt", ns)
    assert [(p.get("lat"), p.get("lon")) for p in punkte] == [("52.500000", "13.400000"), ("53.000000", "12.000000")]
    assert "<speed>" not in res.get_data(as_text=True)     # not a GPX 1.1 element


def test_journey_consumption_one_rule_and_charged_grid_side(client, db):
    # 200 km with 40 kWh, a 4 km hop with 2 kWh (below MIN_CONS_KM) and 100 km
    # without energy: only the first counts → 20.0 kWh/100km (was 42/304 km)
    for start, km, e in (("2026-07-15T10:00:00", 200.0, 40.0), ("2026-07-16T10:00:00", 4.0, 2.0),
                         ("2026-07-17T10:00:00", 100.0, None)):
        db.execute("INSERT INTO trips (device, start_time, end_time, distance_km, energy_kwh) VALUES ('dev1', ?, ?, ?, ?)",
                   (start, start, km, e))
    # a TeslaMate charge (battery side 27, grid 30 from the note) and a receipt of 20 kWh
    db.execute("INSERT INTO charge_sessions (vehicle_plate, start_time, end_time, total_kwh, is_external, session_number, note) "
               "VALUES ('EV1', '2026-07-16T12:00:00', '2026-07-16T13:00:00', 27, 1, 'TM-1', 'TeslaMate import (kwh_used=30.0, kw_max=11)')")
    db.execute("INSERT INTO charge_sessions (vehicle_plate, start_time, end_time, total_kwh, is_external, cost_total) "
               "VALUES ('EV1', '2026-07-17T12:00:00', '2026-07-17T13:00:00', 20, 1, 10)")
    db.commit()
    jid = _create(client, excluded_ids=[])
    html = client.get(f"/journeys/{jid}").get_data(as_text=True)
    assert "20.0 kWh/100km" in html
    assert "50.0 kWh (2x)" in html
