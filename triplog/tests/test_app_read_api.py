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
    for path in ("health", "capabilities", "config", "status", "trips", "charging"):
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


def test_modules_default_and_change(env):
    app_module, db, adm, vids = env
    adm.post("/api/admin/read-api", json={"enabled": True})
    tok = _key(adm, vids[0])["token"]
    anon = app_module.app.test_client()
    cfg = anon.get("/api/v1/config", headers=_hdr(tok)).get_json()
    assert cfg["modules"] == ["dashboard"]
    assert cfg["interval"] == {"drive": 10, "park": 60}
    r = adm.post(f"/api/admin/read-api/{vids[0]}/modules", json={"modules": ["music", "dashboard", "x"]})
    assert r.get_json()["modules"] == ["dashboard", "music"]
    assert anon.get("/api/v1/config", headers=_hdr(tok)).get_json()["modules"] == ["dashboard", "music"]
    assert adm.post(f"/api/admin/read-api/{vids[1]}/modules", json={"modules": []}).status_code == 404


def test_trips_and_charging_of_the_vehicle_only_in_utc(env):
    app_module, db, adm, vids = env
    adm.post("/api/admin/read-api", json={"enabled": True})
    tok = _key(adm, vids[0])["token"]
    for dev, start in (("dev1", "2026-07-01T10:00:00"), ("dev1", "2026-07-02T10:00:00"),
                       ("dev2", "2026-07-03T10:00:00")):
        db.execute("INSERT INTO trips (device, start_time, end_time, distance_km, soc_start, soc_end, "
                   "end_address, note) VALUES (?, ?, ?, 12.34, 80, 75, 'Ziel', 'privat')",
                   (dev, start, start.replace("10:00", "10:30")))
    db.execute("INSERT INTO charges (device, start_time, end_time, type, energy_kwh) "
               "VALUES ('dev1', '2026-07-01T12:00:00', '2026-07-01T12:20:00', 'dc', 31.456)")
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
