"""Visits view: outings from leaving home to coming back, on-site blocks of
stops within 10 km, charging stops on the way count as travel."""

import besuche

HOME = (52.8105, 13.3745)
SITE = (52.5165, 12.9300)        # a customer site
RETHE = (52.5065, 12.9505)        # 1.7 km away
BAHNHOF = (52.4895, 13.2065)     # station, 17 km from the site
BRAAK = (52.6175, 13.2585)


def trip(st, et, a, b, purpose="Dienstfahrt", dest="", reason="", a_addr=None, b_addr=None):
    return {"start_time": st, "end_time": et,
            "start_lat": a and a[0], "start_lon": a and a[1], "end_lat": b and b[0], "end_lon": b and b[1],
            "start_address": a_addr, "end_address": b_addr, "purpose": purpose,
            "destination": dest, "visit_reason": reason, "distance_km": 10}


def build(trips, charges=()):
    return besuche.build_outings(trips, list(charges), [HOME], {"Zuhause"})


def test_day_with_two_sites_and_charge_on_the_way_home():
    trips = [
        trip("2026-10-19T06:48:00", "2026-10-19T07:31:00", HOME, BAHNHOF, dest="Kunde B", b_addr="Am Bahnhof"),
        trip("2026-10-19T10:09:00", "2026-10-19T10:37:00", BAHNHOF, RETHE, dest="Kunde B", reason="Rückfahrt"),
        trip("2026-10-19T11:04:00", "2026-10-19T11:10:00", RETHE, RETHE, dest="Kunde A"),
        trip("2026-10-19T12:07:00", "2026-10-19T12:15:00", RETHE, SITE, dest="Kunde A"),
        trip("2026-10-19T15:06:00", "2026-10-19T15:41:00", SITE, BRAAK, dest="Kunde A", reason="Rückfahrt"),
        trip("2026-10-19T16:05:00", "2026-10-19T16:31:00", BRAAK, HOME, dest="Kunde A", reason="Rückfahrt"),
    ]
    charge = {"start_time": "2026-10-19T15:44:00", "end_time": "2026-10-19T16:02:00"}
    [o] = build(trips, [charge])
    assert o["complete"] and o["minutes"] == 583            # 06:48 – 16:31
    assert [(s["start"].strftime("%H:%M"), s["end"].strftime("%H:%M")) for s in o["sites"]] == \
        [("07:31", "10:09"), ("10:37", "15:06")]            # Braak (charging) is travel
    assert o["sites"][0]["destinations"] == ["Kunde B"]
    assert o["sites"][1]["destinations"] == ["Kunde A"]     # not the "Rückfahrt" from Kunde B


def test_missing_position_on_the_way_back_counts_as_home_when_next_starts_there():
    trips = [
        trip("2026-10-16T23:50:00", "2026-10-17T00:27:00", HOME, BAHNHOF, dest="Kunde B"),
        trip("2026-10-17T07:19:00", "2026-10-17T08:04:00", None, None, dest="Kunde B", reason="Rückfahrt"),
        trip("2026-10-19T06:48:00", "2026-10-19T07:31:00", HOME, BAHNHOF, dest="Kunde B"),
    ]
    outings = build(trips)
    night = outings[-1]
    assert night["complete"] and night["sites"][0]["minutes"] == 412   # 00:27 – 07:19


def test_home_by_address_and_two_outings_a_day_newest_first():
    trips = [
        trip("2026-10-21T06:08:00", "2026-10-21T06:44:00", None, SITE, a_addr="Zuhause"),
        trip("2026-10-21T14:40:00", "2026-10-21T15:34:00", SITE, None, b_addr="Zuhause"),
        trip("2026-10-21T16:23:00", "2026-10-21T16:45:00", HOME, BRAAK, purpose="Privatfahrt"),
        trip("2026-10-21T17:28:00", "2026-10-21T17:46:00", BRAAK, HOME, purpose="Privatfahrt"),
    ]
    first, second = build(trips)
    assert first["start"].hour == 16 and first["purpose"] == "Privatfahrt"
    assert second["sites"][0]["minutes"] == 476 and second["purpose"] == "Dienstfahrt"


def test_overlapping_hand_filled_trips_give_no_site_but_a_warning():
    trips = [trip("2026-10-22T06:46:00", "2026-10-22T22:55:00", None, None, a_addr="Zuhause"),
             trip("2026-10-22T06:46:00", "2026-10-22T22:55:00", None, None, b_addr="Zuhause")]
    [o] = build(trips)
    assert o["overlap"] and o["sites"] == []


def test_visits_page_renders(app_module, db):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) VALUES (1, 'ingo', 'x', 1, 1)")
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'dev1')")
    db.execute("INSERT INTO locations (name, lat, lon) VALUES ('Zuhause', ?, ?)", HOME)
    for t in (trip("2026-10-20T06:06:00", "2026-10-20T07:18:00", HOME, SITE, dest="Kunde A"),
              trip("2026-10-20T14:59:00", "2026-10-20T16:03:00", SITE, HOME)):
        db.execute("""INSERT INTO trips (device, start_time, end_time, start_lat, start_lon, end_lat, end_lon,
                      purpose, destination) VALUES ('dev1', ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (t["start_time"], t["end_time"], t["start_lat"], t["start_lon"], t["end_lat"], t["end_lon"],
                    t["purpose"], t["destination"]))
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        html = c.get("/trips/visits?from=2026-10-20&to=2026-10-20").get_data(as_text=True)
    assert "Di 20.10.2026" in html
    tt = '<span class="tt">{}</span><span class="sep">–</span><span class="tt">{}</span>'
    assert tt.format("06:06", "16:03") in html and tt.format("07:18", "14:59") in html
    assert 'class="dot"' not in html.split('<table class="visits">', 1)[1]
    assert "nicht zu Hause" in html and "Kunde A" in html


def test_visits_purpose_filter(app_module, db):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) VALUES (1, 'ingo', 'x', 1, 1)")
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'dev1')")
    db.execute("INSERT INTO locations (name, lat, lon) VALUES ('Zuhause', ?, ?)", HOME)
    rows = (trip("2026-10-20T06:06:00", "2026-10-20T07:18:00", HOME, SITE, dest="Kunde A"),
            trip("2026-10-20T14:59:00", "2026-10-20T16:03:00", SITE, HOME),
            trip("2026-10-21T16:23:00", "2026-10-21T16:45:00", HOME, BRAAK, purpose="Privatfahrt"),
            trip("2026-10-21T17:28:00", "2026-10-21T17:46:00", BRAAK, HOME, purpose="Privatfahrt"))
    for t in rows:
        db.execute("""INSERT INTO trips (device, start_time, end_time, start_lat, start_lon, end_lat, end_lon,
                      purpose, destination) VALUES ('dev1', ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (t["start_time"], t["end_time"], t["start_lat"], t["start_lon"], t["end_lat"], t["end_lon"],
                    t["purpose"], t["destination"]))
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        all_ = c.get("/trips/visits?from=2026-10-20&to=2026-10-21").get_data(as_text=True)
        assert "Di 20.10.2026" in all_ and "Mi 21.10.2026" in all_
        only = c.get("/trips/visits?p=Dienstfahrt").get_data(as_text=True)
        assert "Di 20.10.2026" in only and "Mi 21.10.2026" not in only
        assert 'href="/trips/visits?all=1"' in only                       # reset chip
        assert "Mi 21.10.2026" not in c.get("/trips/visits").get_data(as_text=True)   # kept in session
        assert "Mi 21.10.2026" in c.get("/trips/visits?all=1").get_data(as_text=True)
