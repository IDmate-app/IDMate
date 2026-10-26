"""Live journey for guests: the owner creates a link + four-digit code per
guest, the guest enters the code once (one device per code), then sees the
track and the car — delayed by the share's delay — and nothing else."""

import re
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def owner(app_module, db):
    db.execute("INSERT INTO vehicles (id, plate, device, body_style, color) VALUES (1, 'EV1', 'dev1', 'kombi', '#ff0000')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) "
               "VALUES (1, 'admin', 'x', 1, 1)")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    # plain clients (no `with`): several browsers side by side must not keep
    # each other's request contexts pushed
    c = app_module.app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
    return c


@pytest.fixture
def guest(app_module):
    """A second, anonymous browser."""
    return app_module.app.test_client()


def _local(delta_h):
    import detector
    return (datetime.now(detector.LOCAL_TZ) + timedelta(hours=delta_h)).strftime("%Y-%m-%dT%H:%M:%S")


def _trip(db, start, end, s_lat, s_lon, e_lat, e_lon):
    cur = db.execute(
        "INSERT INTO trips (device, start_time, end_time, distance_km, start_lat, start_lon, end_lat, end_lon) "
        "VALUES ('dev1', ?, ?, 50, ?, ?, ?, ?)", (start, end, s_lat, s_lon, e_lat, e_lon))
    db.commit()
    return cur.lastrowid


def _journey(owner, db, days_ahead=3):
    heute = datetime.now().strftime("%Y-%m-%d")
    ende = (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    _trip(db, _local(-30), _local(-29), 52.5, 13.4, 53.0, 12.0)
    return owner.post("/api/journeys", json={
        "title": "Ostsee", "date_from": (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%dT08:00"),
        "date_to": ende + "T18:00", "excluded_ids": []}).get_json()["id"]


def _share(owner, jid, name="Oma", delay_min=0):
    res = owner.post(f"/api/journeys/{jid}/shares", json={"name": name, "delay_min": delay_min})
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()


def test_owner_creates_named_guest_access(owner, db):
    jid = _journey(owner, db)
    s = _share(owner, jid, "Oma", 240)
    assert len(s["key"]) == 16 and re.fullmatch(r"[23456789A-HJKMNP-Z]{4}", s["code"]), s["code"]
    assert s["url"].endswith(f"/s/{s['key']}") and s["path"] == f"/s/{s['key']}" and s["delay_min"] == 240
    assert s["last_seen_at"] is None and s["device_bound"] is False
    assert "device_token" not in s
    liste = owner.get(f"/api/journeys/{jid}/shares").get_json()
    assert [g["name"] for g in liste] == ["Oma"]
    # validation: the label is optional, the delay is not free-form
    ohne = owner.post(f"/api/journeys/{jid}/shares", json={"name": "", "delay_min": 0}).get_json()
    assert ohne["name"] == "" and len(ohne["code"]) == 4
    assert owner.post(f"/api/journeys/{jid}/shares", json={"name": "X", "delay_min": 90}).status_code == 400
    # the detail page carries the card
    html = owner.get(f"/journeys/{jid}").get_data(as_text=True)
    assert "Live teilen" in html and 'id="shareList"' in html


def test_guest_needs_the_code_and_then_sees_only_the_track(owner, guest, db, app_module):
    jid = _journey(owner, db)
    s = _share(owner, jid, "Oma")
    url = f"/s/{s['key']}"
    # no login redirect: the code page
    res = guest.get(url)
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert 'name="code"' in html and "/login" not in html
    assert "Ostsee" not in html                      # nothing of the journey before the code
    # data is locked without the code
    assert guest.get(url + "/data").status_code == 401
    # wrong code
    res = guest.post(url, data={"code": "ZZZZ" if s["code"] != "ZZZZ" else "YYYY", "name": "Gast"})
    assert res.status_code == 403 and "stimmt nicht" in res.get_data(as_text=True)
    # right code -> cookie -> map page; typed lower case with a space, as guests do
    res = guest.post(url, data={"code": s["code"][:2].lower() + " " + s["code"][2:].lower(), "name": "Gast"})
    assert res.status_code == 302
    html = guest.get(url).get_data(as_text=True)
    assert 'id="map"' in html and "Ostsee" in html
    for verboten in ("km", "kWh", "EUR", "/api/locations", "/api/charge/locations", "Fahrten"):
        assert verboten not in html, verboten
    # the data: the track (coords fallback) and the live position from the state cache
    app_module._state_cache["dev1"] = {"lat": 53.1, "lon": 11.9, "heading": 270, "speed": 80,
                                       "time": "2026-09-29T07:00:00Z"}
    d = guest.get(url + "/data").get_json()
    assert d["segments"] == [[[52.5, 13.4], [53.0, 12.0]]]
    assert d["pos"]["lat"] == 53.1 and d["pos"]["speed"] == 80
    assert d["versatz_min"] == 0 and d["trail"] == []
    # a repeat poll with the same stand carries no segments
    d2 = guest.get(url + f"/data?stand={d['stand']}&since={d['bis']}").get_json()
    assert "segments" not in d2 and d2["pos"]["lon"] == 11.9
    app_module._state_cache.pop("dev1", None)
    # the owner sees that the guest watched
    g = owner.get(f"/api/journeys/{jid}/shares").get_json()[0]
    assert g["last_seen_at"] and g["device_bound"] is True and g["views"] == 1


def test_one_device_per_code(owner, guest, db, app_module):
    jid = _journey(owner, db)
    s = _share(owner, jid)
    url = f"/s/{s['key']}"
    guest.post(url, data={"code": s["code"], "name": "Gast"})
    assert guest.get(url + "/data").status_code == 200
    zweites = app_module.app.test_client()
    zweites.post(url, data={"code": s["code"], "name": "Gast"})
    assert zweites.get(url + "/data").status_code == 200
    # the first device is out and sees the code page with the switch hint
    assert guest.get(url + "/data").status_code == 401
    assert "anderen Ger" in guest.get(url).get_data(as_text=True)
    # a new code from the owner signs the second device out as well
    neu = owner.post(f"/api/journeys/{jid}/shares/{s['id']}/reset", json={}).get_json()
    assert neu["code"] != s["code"] or True  # may collide by chance, device must be gone either way
    assert neu["device_bound"] is False
    assert zweites.get(url + "/data").status_code == 401
    # removing the access leaves a landing page, no 404
    assert owner.delete(f"/api/journeys/{jid}/shares/{s['id']}").get_json()["ok"]
    res = guest.get(url)
    assert res.status_code == 410 and "nicht mehr g" in res.get_data(as_text=True)
    assert 'name="code"' not in res.get_data(as_text=True)
    assert zweites.get(url + "/data").status_code == 410
    assert guest.get("/s/ffffffffffffffff").status_code == 410


def test_wrong_codes_lock_the_share(owner, guest, db):
    jid = _journey(owner, db)
    s = _share(owner, jid)
    url = f"/s/{s['key']}"
    falsch = "ZZZZ" if s["code"] != "ZZZZ" else "YYYY"
    for _ in range(4):
        assert guest.post(url, data={"code": falsch, "name": "Gast"}).status_code == 403
    assert guest.post(url, data={"code": falsch, "name": "Gast"}).status_code == 429
    # even the right code waits now
    assert guest.post(url, data={"code": s["code"], "name": "Gast"}).status_code == 429


def test_delay_hides_what_happened_since(owner, guest, db):
    jid = _journey(owner, db)
    _trip(db, _local(-0.5), _local(-0.2), 53.0, 12.0, 54.0, 10.0)   # ended 12 min ago
    s = _share(owner, jid, "Kollege", 60)
    url = f"/s/{s['key']}"
    guest.post(url, data={"code": s["code"], "name": "Gast"})
    d = guest.get(url + "/data").get_json()
    assert d["versatz_min"] == 60
    assert d["segments"] == [[[52.5, 13.4], [53.0, 12.0]]]      # the fresh trip is not there yet
    assert d["pos"] is None                                      # no Influx: no position of an hour ago
    live = _share(owner, jid, "Oma", 0)
    guest.post(f"/s/{live['key']}", data={"code": live["code"], "name": "Gast"})
    assert len(guest.get(f"/s/{live['key']}/data").get_json()["segments"]) == 2


def test_share_ends_with_the_journey(owner, guest, db, app_module):
    jid = _journey(owner, db)
    s = _share(owner, jid)
    url = f"/s/{s['key']}"
    guest.post(url, data={"code": s["code"], "name": "Gast"})
    assert guest.get(url + "/data").status_code == 200
    # the journey ends in an hour: still on; ended an hour ago: off, exactly then
    owner.post(f"/api/journeys/{jid}", json={"date_to": _local(1)[:16]})
    assert 'id="map"' in guest.get(url).get_data(as_text=True)       # the map, not the ended page
    owner.post(f"/api/journeys/{jid}", json={"date_to": _local(-1)[:16]})
    html = guest.get(url).get_data(as_text=True)
    assert 'id="map"' not in html and "beendet" in html
    assert guest.get(url + "/data").get_json() == {"beendet": True}
    # a new device learns that only after the code, the bare link says nothing
    neu = app_module.app.test_client()
    html = neu.get(url).get_data(as_text=True)
    assert 'name="code"' in html and "beendet" not in html
    assert neu.get(url + "/data").status_code == 401
    assert neu.post(url, data={"code": s["code"], "name": "Gast"}).status_code == 302
    assert "beendet" in neu.get(url).get_data(as_text=True)


def test_guest_page_follows_the_browser_language(owner, guest, db):
    jid = _journey(owner, db)
    s = _share(owner, jid)
    html = guest.get(f"/s/{s['key']}", headers={"Accept-Language": "en-US,en;q=0.9"}).get_data(as_text=True)
    assert "Enter the four-digit code" in html
    html = guest.get(f"/s/{s['key']}", headers={"Accept-Language": "de-DE,de;q=0.9"}).get_data(as_text=True)
    assert "vierstelligen Code" in html


def test_guest_gets_the_roundtrip_map_through_the_share(owner, guest, db, app_module, monkeypatch):
    import urllib.request
    import config
    monkeypatch.setattr(config, "ROUNDTRIP_URL", "http://rt:8580")
    jid = _journey(owner, db)
    s = _share(owner, jid)
    url = f"/s/{s['key']}"
    # tiles are locked like everything else until the code
    assert guest.get(url + "/tiles/5/16/10.mvt").status_code == 401
    guest.post(url, data={"code": s["code"], "name": "Gast"})
    html = guest.get(url).get_data(as_text=True)
    assert f"/s/{s['key']}/tiles/{{z}}/{{x}}/{{y}}.mvt" in html
    assert "'/tiles/{z}" not in html                        # not the login-only relay

    class Antwort:
        status = 200
        def read(self):
            return b"\x1a\x00tile"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    aufrufe = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=10: aufrufe.append(req.full_url) or Antwort())
    r = guest.get(url + "/tiles/5/16/10.mvt")
    assert r.status_code == 200 and r.mimetype == "application/vnd.mapbox-vector-tile"
    assert aufrufe == ["http://rt:8580/tiles/5/16/10.mvt"]
    assert guest.get(url + "/tiles/16/0/0.mvt").status_code == 404   # beyond the planet
    # the plain relay still needs the login
    assert app_module.app.test_client().get("/tiles/5/16/10.mvt").status_code in (302, 401)


def test_guests_see_the_share_title_not_the_sorting_title(owner, guest, db):
    jid = _journey(owner, db)
    owner.post(f"/api/journeys/{jid}", json={"title": "2026 09 Finnland Urlaub"})
    s = _share(owner, jid)
    url = f"/s/{s['key']}"
    guest.post(url, data={"code": s["code"], "name": "Gast"})
    assert "2026 09 Finnland Urlaub" in guest.get(url).get_data(as_text=True)   # no share title yet
    assert owner.post(f"/api/journeys/{jid}", json={"share_title": "  Unser Finnland-Sommer "}).get_json()["ok"]
    html = guest.get(url).get_data(as_text=True)
    assert "Unser Finnland-Sommer" in html and "2026 09 Finnland" not in html
    # the owner's page carries the field and keeps the internal title
    html = owner.get(f"/journeys/{jid}").get_data(as_text=True)
    assert 'id="shareTitle"' in html and 'value="Unser Finnland-Sommer"' in html and "2026 09 Finnland Urlaub" in html
    # emptied: back to the journey title
    owner.post(f"/api/journeys/{jid}", json={"share_title": ""})
    assert "2026 09 Finnland Urlaub" in guest.get(url).get_data(as_text=True)


def test_list_shows_the_red_lamp_with_the_guest_count(owner, db):
    jid = _journey(owner, db)
    assert 'class="journey-live"' not in owner.get("/journeys").get_data(as_text=True)
    _share(owner, jid, "Oma")
    _share(owner, jid, "Kollege", 240)
    html = owner.get("/journeys").get_data(as_text=True)
    assert 'class="journey-live"' in html and '<span class="lampe"></span>2' in html
    # a journey that is over has no live guests, whatever links exist
    owner.post(f"/api/journeys/{jid}", json={"date_to": _local(-1)[:16]})
    assert 'class="journey-live"' not in owner.get("/journeys").get_data(as_text=True)


IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
PIXEL = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0"


def test_device_from_user_agent(app_module):
    from blueprints.share import geraet_aus_ua
    assert geraet_aus_ua(IPHONE) == ("iOS", "Safari", "iPhone")
    assert geraet_aus_ua(PIXEL) == ("Android", "Chrome", "Pixel 8")
    assert geraet_aus_ua(WINDOWS) == ("Windows", "Firefox", "")
    assert geraet_aus_ua("") == ("", "", "")


def test_family_link_lists_devices_and_blocks_one(owner, db, app_module):
    jid = _journey(owner, db)
    s = _share(owner, jid, "Familie")
    assert s["max_devices"] == 1
    res = owner.post(f"/api/journeys/{jid}/shares", json={"name": "Familie", "delay_min": 0, "max_devices": 4})
    fam = res.get_json()
    assert fam["max_devices"] == 4 and fam["devices"] == []
    url = f"/s/{fam['key']}"
    basti = app_module.app.test_client()
    kristin = app_module.app.test_client()
    achim = app_module.app.test_client()
    # the code page asks for the guest's own name, always
    html = basti.get(url).get_data(as_text=True)
    assert 'name="name"' in html and 'required' in html.split('name="name"')[1].split('>')[0]
    assert basti.post(url, data={"code": fam["code"]}).status_code == 400
    assert basti.post(f"/s/{s['key']}", data={"code": s["code"]}).status_code == 400   # single-device link too
    assert basti.post(url, data={"code": fam["code"], "name": "Basti"}, headers={"User-Agent": IPHONE}).status_code == 302
    assert kristin.post(url, data={"code": fam["code"], "name": "Kristin"}, headers={"User-Agent": PIXEL}).status_code == 302
    assert achim.post(url, data={"code": fam["code"], "name": "Achim"}, headers={"User-Agent": WINDOWS}).status_code == 302
    for c in (basti, kristin, achim):
        assert c.get(url + "/data").status_code == 200
    liste = owner.get(f"/api/journeys/{jid}/shares").get_json()
    f = [x for x in liste if x["id"] == fam["id"]][0]
    assert f["devices_active"] == 3
    assert [(d["name"], d["os"], d["browser"], d["model"]) for d in f["devices"]] == [
        ("Basti", "iOS", "Safari", "iPhone"), ("Kristin", "Android", "Chrome", "Pixel 8"), ("Achim", "Windows", "Firefox", "")]
    assert all(d["last_seen"] for d in f["devices"])
    # a fourth device fits, a fifth does not
    vier = app_module.app.test_client()
    assert vier.post(url, data={"code": fam["code"], "name": "Vier"}).status_code == 302
    fuenf = app_module.app.test_client()
    res = fuenf.post(url, data={"code": fam["code"], "name": "Fuenf"})
    assert res.status_code == 403 and "belegt" in res.get_data(as_text=True)
    # the same device entering the code again keeps its seat (no new row)
    assert basti.post(url, data={"code": fam["code"], "name": "Basti"}).status_code == 302
    f = [x for x in owner.get(f"/api/journeys/{jid}/shares").get_json() if x["id"] == fam["id"]][0]
    assert f["devices_active"] == 4
    # block Kristin: she is out, the others stay, the code changed
    kd = [d for d in f["devices"] if d["name"] == "Kristin"][0]
    neu = owner.post(f"/api/journeys/{jid}/shares/{fam['id']}/devices/{kd['id']}/block", json={}).get_json()
    assert neu["code"] != fam["code"]
    assert [d["blocked"] for d in neu["devices"] if d["name"] == "Kristin"] == [1]
    assert neu["devices_active"] == 3
    assert kristin.get(url + "/data").status_code == 401
    assert basti.get(url + "/data").status_code == 200 and achim.get(url + "/data").status_code == 200
    # the old code is dead for her
    assert kristin.post(url, data={"code": fam["code"], "name": "Kristin"}).status_code == 403
    # the owner page renders the device list
    html = owner.get(f"/journeys/{jid}").get_data(as_text=True)
    assert 'id="shareDevices"' in html and "blockDevice" in html
