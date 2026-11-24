"""Read API /api/v1 (blueprints/readapi.py): off by default, one token per
vehicle (QR code, shown once), telegram answers with only the known fields."""
import base64
import json

import pytest


@pytest.fixture
def env(app_module, db):
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    app_module._login_attempts.clear()
    for nm, pl, dv in [("Auto1", "A-1", "dev1"), ("Auto2", "A-2", "dev2")]:
        db.execute("INSERT INTO vehicles (name, plate, device) VALUES (?, ?, ?)", (nm, pl, dv))
    db.execute("INSERT INTO users (username, password_hash, is_admin) VALUES ('admin', ?, 1)",
               (app_module.generate_password_hash("Sup3r!secret"),))
    db.execute("INSERT INTO users (username, password_hash, is_admin) VALUES ('bob', ?, 0)",
               (app_module.generate_password_hash("Sup3r!secret"),))
    db.commit()
    vids = [r[0] for r in db.execute("SELECT id FROM vehicles ORDER BY id")]
    adm = app_module.app.test_client()
    adm.post("/login", data={"username": "admin", "password": "Sup3r!secret"})
    with app_module._state_lock:
        app_module._state_cache.clear()
        app_module._state_cache["dev1"] = {
            "soc": 71.5, "range_km": 312, "power": -12.4, "speed": 87, "lat": 50.1, "lon": 12.3,
            "odometer": 23456.7, "charging": 0, "dc_charging": 0, "ext_temp": 9,
            "voltage": 380, "time": "2026-11-12T14:03:21Z"}
        app_module._state_cache["dev2"] = {"soc": 20, "time": "2026-11-12T10:00:00Z"}
    yield app_module, db, adm, vids
    with app_module._state_lock:
        app_module._state_cache.clear()
    app_module._login_attempts.clear()


def _key(adm, vid, **body):
    r = adm.post(f"/api/admin/read-api/{vid}/key", json=body)
    assert r.status_code == 200
    return r.get_json()


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def test_off_by_default_everything_is_404(env):
    app_module, db, adm, vids = env
    anon = app_module.app.test_client()
    tok = _key(adm, vids[0])["token"]          # a key may exist, the API is still off
    for path in ("health", "capabilities", "config", "status", "trips", "charging", "energy-model"):
        assert anon.get(f"/api/v1/{path}", headers=_hdr(tok)).status_code == 404, path


def test_enable_needs_admin(env):
    app_module, db, adm, vids = env
    bob = app_module.app.test_client()
    bob.post("/login", data={"username": "bob", "password": "Sup3r!secret"})
    assert bob.post("/api/admin/read-api", json={"enabled": True}).status_code in (302, 403)
    assert bob.post(f"/api/admin/read-api/{vids[0]}/key").status_code in (302, 403)
    anon = app_module.app.test_client()
    assert anon.get("/api/admin/read-api").status_code == 401
    assert anon.get("/api/v1/health").status_code == 404


def test_health_and_token_required(env):
    app_module, db, adm, vids = env
    assert adm.post("/api/admin/read-api", json={"enabled": True}).get_json()["enabled"] is True
    anon = app_module.app.test_client()
    assert anon.get("/api/v1/health").get_json() == {"ok": True, "api": 1}
    assert anon.get("/api/v1/status").status_code == 401
    assert anon.get("/api/v1/status", headers=_hdr("wrong")).status_code == 401
    # a web session is no substitute for the token
    assert adm.get("/api/v1/status").status_code == 401


def test_status_telegram_only_known_fields(env):
    app_module, db, adm, vids = env
    adm.post("/api/admin/read-api", json={"enabled": True})
    tok = _key(adm, vids[0])["token"]
    anon = app_module.app.test_client()
    t = anon.get("/api/v1/status", headers=_hdr(tok)).get_json()
    assert (t["v"], t["src"], t["type"], t["ts"]) == (1, "backend", "status", "2026-11-12T14:03:21Z")
    # no range: without trips IDmate has no figure of its own, and what the
    # stick once stored as the car's range (312) is not served
    assert t["data"] == {"soc": 71.5, "power": -12.4, "speed": 87, "lat": 50.1,
                         "lon": 12.3, "odometer": 23456.7, "charging": False, "dc": False,
                         "t_ext": 9}                    # no t_batt, no voltage
    t2 = anon.get("/api/v1/status", headers=_hdr(tok)).get_json()
    assert t2["seq"] == t["seq"] + 1
    caps = anon.get("/api/v1/capabilities", headers=_hdr(tok)).get_json()
    assert caps["type"] == "caps"
    assert set(caps["data"]["status"]) == set(t["data"])
    assert "t_batt" not in caps["data"]["status"]


def test_range_is_idmates_own_and_charging_only_while_it_is(env):
    """The range is the figure of the dashboard (energy left ÷ consumption of
    the last trips), and "charging" is true only while the newest telegram
    says so — the state keeps the bit of the last charging stop for ever."""
    app_module, db, adm, vids = env
    adm.post("/api/admin/read-api", json={"enabled": True})
    tok = _key(adm, vids[0])["token"]
    db.execute("UPDATE vehicles SET battery_capacity_kwh = 80 WHERE id = ?", (vids[0],))
    for d in range(1, 6):
        db.execute("INSERT INTO trips (device, start_time, end_time, distance_km, energy_kwh) "
                   "VALUES ('dev1', ?, ?, 100, 20)", (f"2026-11-0{d}T08:00:00", f"2026-11-0{d}T09:00:00"))
    db.commit()
    anon = app_module.app.test_client()
    with app_module._state_lock:
        st = app_module._state_cache["dev1"]
        st.update(charging=1, dc_charging=1,
                  field_times={"charging": "2026-11-06T14:05:25Z", "dc_charging": "2026-11-06T14:05:25Z"})
    d = anon.get("/api/v1/status", headers=_hdr(tok)).get_json()["data"]
    assert d["range"] == round(71.5 / 100 * 80 / 20 * 100)               # 286 km, not the stored 312
    assert d["charging"] is False and d["dc"] is False                   # the bit is six days old
    with app_module._state_lock:
        app_module._state_cache["dev1"]["field_times"] = {"charging": "2026-11-12T14:03:10Z",
                                                          "dc_charging": "2026-11-06T14:05:25Z"}
    d = anon.get("/api/v1/status", headers=_hdr(tok)).get_json()["data"]
    assert d["charging"] is True and d["dc"] is False                    # charging now, on AC
    caps = anon.get("/api/v1/capabilities", headers=_hdr(tok)).get_json()["data"]["status"]
    assert "range" in caps
    # last_used_at is not written on every poll
    first = db.execute("SELECT last_used_at FROM read_api_keys").fetchone()[0]
    anon.get("/api/v1/status", headers=_hdr(tok))
    assert db.execute("SELECT last_used_at FROM read_api_keys").fetchone()[0] == first and first


def test_token_is_bound_to_its_vehicle(env):
    app_module, db, adm, vids = env
    adm.post("/api/admin/read-api", json={"enabled": True})
    t1, t2 = _key(adm, vids[0])["token"], _key(adm, vids[1])["token"]
    anon = app_module.app.test_client()
    assert anon.get("/api/v1/config", headers=_hdr(t1)).get_json()["vehicle"]["id"] == "dev1"
    assert anon.get("/api/v1/config", headers=_hdr(t2)).get_json()["vehicle"]["id"] == "dev2"
    assert anon.get("/api/v1/status", headers=_hdr(t2)).get_json()["data"] == {"soc": 20}


def test_qr_code_and_token_shown_once_and_only_hash_stored(env):
    app_module, db, adm, vids = env
    adm.post("/api/admin/read-api", json={"enabled": True, "url": "https://idmate.example/"})
    out = _key(adm, vids[0])
    assert out["url"] == "https://idmate.example/api/v1"
    assert base64.b64decode(out["qr"])[:8] == b"\x89PNG\r\n\x1a\n"
    stored = db.execute("SELECT token_hash FROM read_api_keys").fetchone()[0]
    assert out["token"] not in stored and len(stored) == 64
    listing = adm.get("/api/admin/read-api").get_json()
    assert out["token"] not in json.dumps(listing)
    assert [v["has_key"] for v in listing["vehicles"]] == [True, False]


def test_new_key_replaces_old_and_delete_revokes(env):
    app_module, db, adm, vids = env
    adm.post("/api/admin/read-api", json={"enabled": True})
    old = _key(adm, vids[0])["token"]
    new = _key(adm, vids[0])["token"]
    anon = app_module.app.test_client()
    assert anon.get("/api/v1/status", headers=_hdr(old)).status_code == 401
    assert anon.get("/api/v1/status", headers=_hdr(new)).status_code == 200
    assert adm.delete(f"/api/admin/read-api/{vids[0]}/key").status_code == 200
    assert anon.get("/api/v1/status", headers=_hdr(new)).status_code == 401
    # switching the API off again hides it at once
    tok = _key(adm, vids[0])["token"]
    adm.post("/api/admin/read-api", json={"enabled": False})
    assert anon.get("/api/v1/status", headers=_hdr(tok)).status_code == 404


def test_config_names_what_idmate_delivers(env):
    """Navigation, music and messenger are the app's own: IDmate neither
    delivers nor switches them, so there is nothing to tick."""
    app_module, db, adm, vids = env
    adm.post("/api/admin/read-api", json={"enabled": True})
    tok = _key(adm, vids[0])["token"]
    anon = app_module.app.test_client()
    cfg = anon.get("/api/v1/config", headers=_hdr(tok)).get_json()
    # the dashboard and the logbook (L-012) are IDmate's; editing the logbook is off until the admin allows it
    assert cfg["modules"] == ["dashboard", "logbook"]
    assert cfg["logbook"] == {"edit": False}
    assert cfg["interval"] == {"drive": 10, "park": 60}
    assert adm.post(f"/api/admin/read-api/{vids[0]}/modules", json={"modules": ["music"]}).status_code in (404, 405)
    page = adm.get("/admin").get_data(as_text=True)
    assert "Messenger" not in page and "read_api_mod" not in page


def test_trips_and_charging_of_the_vehicle_only_in_utc(env):
    app_module, db, adm, vids = env
    adm.post("/api/admin/read-api", json={"enabled": True})
    tok = _key(adm, vids[0])["token"]
    for dev, start in (("dev1", "2026-07-01T10:00:00"), ("dev1", "2026-07-02T10:00:00"),
                       ("dev2", "2026-07-03T10:00:00")):
        db.execute("INSERT INTO trips (device, start_time, end_time, distance_km, soc_start, soc_end, "
                   "end_address, note) VALUES (?, ?, ?, 12.34, 80, 75, 'Ziel', 'privat')",
                   (dev, start, start.replace("10:00", "10:30")))
    db.execute("INSERT INTO charge_sessions (vehicle_plate, is_external, start_time, end_time, total_kwh, "
               "avg_kw, cost_total) VALUES ('A-1', 1, '2026-07-01T12:00:00', '2026-07-01T12:20:00', "
               "31.456, 94.0, 18.5)")
    db.commit()
    anon = app_module.app.test_client()
    t = anon.get("/api/v1/trips?limit=1", headers=_hdr(tok)).get_json()
    assert t["type"] == "trip" and len(t["data"]["items"]) == 1
    item = t["data"]["items"][0]
    assert item["start"].endswith("Z") and item["start"] != "2026-07-02T10:00:00Z"   # local -> UTC
    assert item["distance"] == 12.3 and item["to"] == "Ziel" and "note" not in item
    assert len(anon.get("/api/v1/trips?limit=999", headers=_hdr(tok)).get_json()["data"]["items"]) == 2
    c = anon.get("/api/v1/charging", headers=_hdr(tok)).get_json()
    assert c["type"] == "charge"
    assert c["data"]["items"][0]["dc"] is True and c["data"]["items"][0]["energy"] == 31.46


def test_charging_is_the_charge_book_not_the_detectors_raw_table(env):
    """The app showed another last charge and another energy than IDmate: the
    endpoint read the detector's table (charges), the pages read the charge
    book (charge_sessions). Now the book: wallbox meter and receipts, newest
    first, grid and battery side, of this vehicle only."""
    app_module, db, adm, vids = env
    adm.post("/api/admin/read-api", json={"enabled": True})
    tok = _key(adm, vids[0])["token"]
    db.execute("UPDATE vehicles SET charge_efficiency = 0.9 WHERE plate = 'A-1'")
    # what the detector saw: the same evening in two pieces, without the meter
    db.execute("INSERT INTO charges (device, start_time, end_time, type, energy_kwh) VALUES "
               "('dev1', '2026-07-05T18:00:00', '2026-07-05T19:00:00', 'ac', 9.1), "
               "('dev1', '2026-07-05T21:00:00', '2026-07-05T23:00:00', 'ac', 17.3)")
    cols = ("vehicle_plate, is_external, session_number, start_time, end_time, total_kwh, avg_kw, "
            "soc_start, soc_end, location_name, cost_total, note")
    db.execute(f"INSERT INTO charge_sessions ({cols}) VALUES "
               "('A-1', 0, '41', '2026-07-05T18:00:00', '2026-07-05T23:00:00', 30.0, 6.0, 40, 78, "
               "'Zuhause', NULL, 'privat'), "
               "('A-1', 1, NULL, '2026-07-03T09:00:00', '2026-07-03T09:25:00', 42.0, 100.8, 12, 60, "
               "'Schnelllader', 24.78, NULL), "
               "('A-1', 1, NULL, '2026-07-02T09:00:00', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL), "
               "('A-2', 0, '7', '2026-07-06T18:00:00', '2026-07-06T20:00:00', 11.0, 5.5, 50, 64, "
               "'Zuhause', NULL, NULL)")
    db.commit()
    anon = app_module.app.test_client()
    items = anon.get("/api/v1/charging", headers=_hdr(tok)).get_json()["data"]["items"]
    assert [i["start"][:10] for i in items] == ["2026-07-05", "2026-07-03", "2026-07-02"]   # A-1 only
    last = items[0]
    assert last["energy"] == 30.0 and last["energy_battery"] == 27.0        # meter / into the battery
    assert last["dc"] is False and last["at"] == "Zuhause"
    assert last["soc_start"] == 40 and last["soc_end"] == 78 and last["power_avg"] == 6.0
    assert last["end"].endswith("Z") and "note" not in last and "power_max" not in last
    assert items[1]["dc"] is True and items[1]["energy"] == 42.0
    assert set(items[2]) == {"id", "start"}                                   # nothing known: nothing sent


def test_wrong_tokens_are_rate_limited(env):
    app_module, db, adm, vids = env
    adm.post("/api/admin/read-api", json={"enabled": True})
    tok = _key(adm, vids[0])["token"]
    anon = app_module.app.test_client()
    codes = [anon.get("/api/v1/status", headers=_hdr(f"bad{i}")).status_code
             for i in range(app_module._LOGIN_MAX + 1)]
    assert codes[0] == 401 and codes[-1] == 429
    assert anon.get("/api/v1/status", headers=_hdr(tok)).status_code == 429   # also the right one, for now


def test_admin_page_has_the_section(env):
    app_module, db, adm, vids = env
    html = adm.get("/admin").get_data(as_text=True)
    assert 'id="readApiEnabled"' in html and "async function loadReadApi()" in html


# ── Display session: the original dashboard map for a display device ──────────

def _display_client(app_module, adm, vid):
    adm.post("/api/admin/read-api", json={"enabled": True})
    tok = _key(adm, vid)["token"]
    c = app_module.app.test_client()
    r = c.get("/api/v1/display", headers=_hdr(tok))
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    return c, tok


def test_display_needs_the_token(env):
    app_module, db, adm, vids = env
    adm.post("/api/admin/read-api", json={"enabled": True})
    anon = app_module.app.test_client()
    assert anon.get("/api/v1/display").status_code == 401
    assert anon.get("/").status_code == 302                      # still the login page


def test_display_shows_the_dashboard_map_only(env):
    app_module, db, adm, vids = env
    c, _ = _display_client(app_module, adm, vids[0])
    r = c.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'id="map"' in html and "setFull(true);" in html
    assert "async function loadNotes" not in html and "async function bmsLoad" not in html  # no notes, no BMS learning
    # what the map reads works ...
    for path in ("/api/vehicle-state", "/api/trail", "/api/range-area/last", "/api/locations",
                 "/api/charge/locations", "/healthz"):
        assert c.get(path).status_code == 200, path
    # ... and it is the vehicle of the token
    assert c.get("/api/vehicle-state").get_json().get("soc") == 71.5


def test_display_session_cannot_do_anything_else(env):
    app_module, db, adm, vids = env
    c, _ = _display_client(app_module, adm, vids[0])
    for path in ("/admin", "/trips", "/charges", "/journeys", "/analysis", "/profile",
                 "/api/admin/read-api", "/api/admin/users", "/api/settings", "/api/notes",
                 "/api/vehicles", "/api/bms-learn", "/api/ha/vehicles"):
        assert c.get(path).status_code == 403, path
    # no writing at all, not even on endpoints it may read
    assert c.post("/api/locations", json={}).status_code == 403
    assert c.post("/api/user/active-vehicle", json={"vehicle_id": vids[1]}).status_code == 403
    assert c.post("/api/admin/read-api", json={"enabled": False}).status_code == 403
    assert c.delete(f"/api/admin/read-api/{vids[0]}/key").status_code == 403
    # and it stays on its own vehicle
    assert c.get("/api/vehicle-state").get_json().get("soc") == 71.5


def test_display_session_ends_with_the_access(env):
    app_module, db, adm, vids = env
    c, _ = _display_client(app_module, adm, vids[0])
    assert c.get("/").status_code == 200
    adm.delete(f"/api/admin/read-api/{vids[0]}/key")
    assert c.get("/").status_code == 302                         # back to the login page
    c2, _ = _display_client(app_module, adm, vids[0])
    adm.post("/api/admin/read-api", json={"enabled": False})
    assert c2.get("/api/vehicle-state").status_code == 401


def test_normal_login_is_untouched(env):
    app_module, db, adm, vids = env
    html = adm.get("/").get_data(as_text=True)
    assert "setFull(true);" not in html and "async function loadNotes" in html
    assert adm.get("/admin").status_code == 200


def test_address_for_the_qr_code_is_the_one_the_browser_uses(env):
    """Behind a proxy that does not pass the protocol on, the server sees
    http for a page opened over https. The QR code must carry https."""
    app_module, db, adm, vids = env
    adm.post("/api/admin/read-api", json={"enabled": True})
    seen = lambda **h: adm.get("/api/admin/read-api", headers=h).get_json()["url"]
    assert seen() == "http://localhost"                                   # what the server believes
    assert seen(Referer="https://localhost/admin") == "https://localhost"
    assert seen(Referer="https://evil.example/x") == "http://localhost"   # another host says nothing
    out = adm.post(f"/api/admin/read-api/{vids[0]}/key", json={},
                   headers={"Origin": "https://localhost"}).get_json()
    assert out["url"] == "https://localhost/api/v1"
    # an address typed in wins over everything
    adm.post("/api/admin/read-api", json={"url": "https://car.example/"})
    assert seen(Referer="https://localhost/admin") == "https://car.example"


def test_stored_http_guess_of_this_host_does_not_reach_the_qr_code(env):
    """Up to 1.2.339 switching the API on stored the address the page showed —
    the server's guess, http behind a proxy without X-Forwarded-Proto. From
    then on every QR code carried http://, which an Android app does not
    open. Such a value counts as not entered; the page no longer sends it."""
    app_module, db, adm, vids = env
    https = {"Origin": "https://localhost"}
    adm.post("/api/admin/read-api", json={"enabled": True, "url": "http://localhost"})   # the old page
    got = adm.get("/api/admin/read-api", headers=https).get_json()
    assert got["url"] == "https://localhost" and got["url_saved"] == ""
    out = adm.post(f"/api/admin/read-api/{vids[0]}/key", json={}, headers=https).get_json()
    assert out["url"] == "https://localhost/api/v1"
    # somebody who is on http has it as before, and another host entered stays
    assert adm.get("/api/admin/read-api").get_json()["url_saved"] == "http://localhost"
    adm.post("/api/admin/read-api", json={"url": "http://192.168.0.9:5000"})
    got = adm.get("/api/admin/read-api", headers=https).get_json()
    assert got["url"] == got["url_saved"] == "http://192.168.0.9:5000"
    # the field carries the entry, the placeholder the address in use
    page = adm.get("/admin").get_data(as_text=True)
    assert "readApiData.url_saved || ''" in page and ".placeholder = readApiData.url" in page


def test_energy_model_is_the_vehicles_own_consumption_by_speed(env, monkeypatch):
    """/energy-model: what the range on the dashboard is computed with —
    kWh/100 km by speed at the outside temperature of now, capacity, the
    energy of a cold start, and what the car really used lately."""
    from datetime import datetime, timedelta
    import bms_learn as bl
    from blueprints import bms
    app_module, db, adm, vids = env
    adm.post("/api/admin/read-api", json={"enabled": True})
    tok = _key(adm, vids[0])["token"]
    anon = app_module.app.test_client()
    monkeypatch.setattr(bms, "_series", lambda device, hours=36: [])
    monkeypatch.setattr(bms, "_hourly", lambda device, field="et": {})
    bms._model_cache.clear()
    monkeypatch.setattr(app_module, "get_bat_kwh", lambda *a, **k: 81.44)
    import blueprints.readapi as readapi
    monkeypatch.setattr(readapi, "get_bat_kwh", lambda *a, **k: 81.44)

    # nothing learned yet: the capacity, and nothing made up
    d = anon.get("/api/v1/energy-model", headers=_hdr(tok)).get_json()
    assert d["type"] == "energy_model" and d["data"] == {"capacity": 81.4}

    # a year of trips that follow 15 + 5·(v/100)² + 0.4·(15 − T) kWh/100 km
    now = datetime.now().replace(microsecond=0)
    n = 0
    for day in range(2, 122):
        v = (35, 60, 85, 110, 125)[day % 5]
        temp = (-2, 4, 9, 14, 21)[(day // 5) % 5]
        km = (18, 40, 75, 140, 60)[day % 5]
        cons = 15 + 5 * (v / 100) ** 2 + 0.4 * max(0, 15 - temp)
        start = now - timedelta(days=day, hours=3)
        db.execute("INSERT INTO trips (device, start_time, end_time, distance_km, energy_kwh, temp_c) "
                   "VALUES ('dev1', ?, ?, ?, ?, ?)",
                   (start.isoformat(), (start + timedelta(hours=km / v)).isoformat(), km,
                    round(km * cons / 100, 3), temp))
        n += 1
    # this morning: 100 km in one hour, 22 kWh; and 20 km in half an hour, 4 kWh
    for back, hours, km, kwh in ((4, 1.0, 100, 22.0), (2, 0.5, 20, 4.0)):
        start = now - timedelta(hours=back)
        db.execute("INSERT INTO trips (device, start_time, end_time, distance_km, energy_kwh, temp_c) "
                   "VALUES ('dev1', ?, ?, ?, ?, 5)",
                   (start.isoformat(), (start + timedelta(hours=hours)).isoformat(), km, kwh))
    db.commit()
    with app_module._state_lock:
        app_module._state_cache["dev1"]["ext_temp"] = 5.0
    d = anon.get("/api/v1/energy-model", headers=_hdr(tok)).get_json()["data"]
    assert d["capacity"] == 81.4 and d["t_ext"] == 5.0
    table = {p["v"]: p["kwh100"] for p in d["consumption"]}
    assert list(table) == list(range(30, 161, 10))
    for v in (30, 80, 130, 160):                        # the law the trips were made with, at 5 °C
        assert abs(table[v] - (15 + 5 * (v / 100) ** 2 + 0.4 * 10)) < 0.6
    assert d["recent"] == {"hours": 6, "distance": 120.0, "consumption": 21.7, "speed": 80}
    assert d["average"]["days"] == 28 and d["average"]["distance"] > 120
    assert d["basis"]["trips"] >= 100 and d["basis"]["speed_max"] == 125
    assert "charge_curve" not in d                       # not measured by IDmate (yet): left out
    # warmer outside: the same car needs less
    with app_module._state_lock:
        app_module._state_cache["dev1"]["ext_temp"] = 20.0
    warm = anon.get("/api/v1/energy-model", headers=_hdr(tok)).get_json()["data"]
    assert warm["consumption"][5]["kwh100"] < d["consumption"][5]["kwh100"] - 3
    # the other vehicle's token gets the other vehicle
    tok2 = _key(adm, vids[1])["token"]
    assert "consumption" not in anon.get("/api/v1/energy-model", headers=_hdr(tok2)).get_json()["data"]
    bms._model_cache.clear()


def test_rain_is_the_raster_of_the_map(env, monkeypatch):
    """/rain (L-005, E-002): the rain raster of the vehicle's range area in the
    telegram envelope; empty frames while off or without an area."""
    import config
    import range_area
    import rain_radar
    app_module, db, adm, vids = env
    adm.post("/api/admin/read-api", json={"enabled": True})
    tok = _key(adm, vids[0])["token"]
    anon = app_module.app.test_client()
    monkeypatch.setattr(config, "RAIN_SOURCE", "")
    d = anon.get("/api/v1/rain", headers=_hdr(tok)).get_json()
    assert d["type"] == "rain" and d["data"]["frames"] == [] and d["data"]["cell_km"] == rain_radar.CELL_KM
    assert anon.get("/api/v1/rain").status_code == 401

    monkeypatch.setattr(config, "RAIN_SOURCE", "dwd")
    asked = []
    monkeypatch.setattr(range_area, "load_last",
                        lambda db, dev: {"lat": 53.5, "lon": 10.0, "kms": [150, 200]} if dev == "dev1" else None)
    fake = {"cell_km": 5.0, "radius": 250, "levels": rain_radar.LEVEL_TEXT, "_center": (53.5, 10.0),
            "frames": [{"t": "2026-11-17T23:40:00Z", "kind": "now", "src": ["dwd"],
                        "cells": [[53.5162, 9.9848, 2]], "_known": {}, "_ts": 0},
                       {"t": "2026-11-17T23:55:00Z", "kind": "forecast", "src": [], "cells": [],
                        "_known": {}, "_ts": 0}]}
    monkeypatch.setattr(rain_radar, "area", lambda la, lo, km, **k: asked.append((la, lo, km)) or fake)
    d = anon.get("/api/v1/rain", headers=_hdr(tok)).get_json()["data"]
    assert asked == [(53.5, 10.0, 200)]
    assert d["frames"] == [{"t": "2026-11-17T23:40:00Z", "kind": "now", "src": ["dwd"],
                            "cells": [[53.5162, 9.9848, 2]]}]          # frames without data left out
    assert d["radius"] == 250 and d["levels"]["2"]
