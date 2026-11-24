"""Logbook through the read API (L-012): GET /api/v1/logbook and /logbook/options
serve the month of the server's logbook to a display app; POST /api/v1/trips/<id>
changes purpose, partner, occasion and note - only where the admin allowed it
for the vehicle, only trips of the token's vehicle."""
import pytest


@pytest.fixture
def env(app_module, db):
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)  # login and admin page first
    app_module._login_attempts.clear()
    for nm, pl, dv in [("Auto1", "A-1", "dev1"), ("Auto2", "A-2", "dev2")]:
        db.execute("INSERT INTO vehicles (name, plate, device) VALUES (?, ?, ?)", (nm, pl, dv))
    db.execute("INSERT INTO users (username, password_hash, is_admin) VALUES ('admin', ?, 1)",
               (app_module.generate_password_hash("Sup3r!secret"),))
    db.execute("INSERT INTO purpose_meta (name, is_private) VALUES ('Privat', 1)")
    db.execute("INSERT INTO purpose_meta (name, is_private, is_main) VALUES ('Kunde', 0, 1)")
    trips = [  # device, start, end, odo_start, odo_end, km, purpose, from, to
        ("dev1", "2026-11-03T08:00:00", "2026-11-03T08:40:00", 1000.0, 1030.0, 30.0, "Kunde", "Zuhause", "Firma A"),
        ("dev1", "2026-11-03T17:00:00", "2026-11-03T17:20:00", 1030.0, 1040.0, 10.0, "Privat", "Firma A", "Markt"),
        ("dev1", "2026-11-04T09:00:00", "2026-11-04T09:30:00", 1040.0, 1055.0, 15.0, "Privat", "Markt", "Zuhause"),
        ("dev1", "2026-11-05T10:00:00", "2026-11-05T10:30:00", 1060.0, 1080.0, 20.0, None, "Zuhause", "Kunde B"),
        ("dev1", "2026-10-30T10:00:00", "2026-10-30T10:30:00", 990.0, 1000.0, 10.0, "Kunde", "X", "Y"),
        ("dev2", "2026-11-03T08:00:00", "2026-11-03T09:00:00", 5000.0, 5050.0, 50.0, "Kunde", "P", "Q"),
    ]
    for dv, a, b, o1, o2, km, p, f, to in trips:
        db.execute("INSERT INTO trips (device, start_time, end_time, odo_start, odo_end, distance_km, purpose, "
                   "start_address, end_address) VALUES (?,?,?,?,?,?,?,?,?)", (dv, a, b, o1, o2, km, p, f, to))
    db.commit()
    vids = [r[0] for r in db.execute("SELECT id FROM vehicles ORDER BY id")]
    adm = app_module.app.test_client()
    adm.post("/login", data={"username": "admin", "password": "Sup3r!secret"})
    adm.post("/api/admin/read-api", json={"enabled": True})
    tok = adm.post(f"/api/admin/read-api/{vids[0]}/key", json={}).get_json()["token"]
    tok2 = adm.post(f"/api/admin/read-api/{vids[1]}/key", json={}).get_json()["token"]
    app_module.app.config["WTF_CSRF_ENABLED"] = True    # the app's POST must pass without a CSRF token
    yield app_module, db, adm, vids, tok, tok2
    app_module.app.config["WTF_CSRF_ENABLED"] = False
    app_module._login_attempts.clear()


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _trip_id(db, start, device="dev1"):
    return db.execute("SELECT id FROM trips WHERE device = ? AND start_time = ?", (device, start)).fetchone()[0]


def test_month_rows_like_the_servers_logbook(env):
    app_module, db, adm, vids, tok, _ = env
    c = app_module.app.test_client()
    r = c.get("/api/v1/logbook?year=2026&month=11", headers=_hdr(tok))
    assert r.status_code == 200
    j = r.get_json()
    assert j["type"] == "logbook"
    d = j["data"]
    assert (d["year"], d["month"], d["edit"]) == (2026, 11, False)
    assert d["months"] == ["2026-11", "2026-10"]
    kinds = [row["kind"] for row in d["rows"]]
    # business trip, two private ones as one line, a 5 km odometer gap, a trip without purpose
    assert kinds == ["trip", "private", "gap", "open"]
    trip, private, gap, open_ = d["rows"]
    assert trip["purpose"] == "Kunde" and trip["from"] == "Zuhause" and trip["to"] == "Firma A"
    assert private["count"] == 2 and private["distance"] == 25.0 and len(private["trips"]) == 2
    assert "from" not in private          # private trips carry no places
    assert gap["distance"] == 5.0 and "trips" not in gap
    assert "purpose" not in open_
    t = d["totals"]
    assert t["trips"] == 4 and t["total"] == 75.0 and t["private"] == 25.0 and t["open"] == 20.0
    assert t["gaps"] == 5.0 and t["purposes"] == {"Kunde": 30.0}
    assert t["odo_start"] == 1000.0 and t["odo_end"] == 1080.0
    items = {i["id"]: i for i in d["items"]}
    assert len(items) == 4
    assert items[private["trips"][0]]["private"] is True
    assert items[trip["trips"][0]]["private"] is False
    assert all(i["start"].endswith("Z") for i in d["items"])


def test_only_the_tokens_vehicle(env):
    app_module, db, adm, vids, tok, tok2 = env
    c = app_module.app.test_client()
    d = c.get("/api/v1/logbook?year=2026&month=11", headers=_hdr(tok2)).get_json()["data"]
    assert [i["from"] for i in d["items"]] == ["P"]
    assert c.get("/api/v1/logbook", headers=_hdr("wrong")).status_code == 401
    assert c.get("/api/v1/logbook?year=2026&month=13", headers=_hdr(tok)).status_code == 400


def test_options_purposes_partners_occasions(env):
    app_module, db, adm, vids, tok, _ = env
    c = app_module.app.test_client()
    d = c.get("/api/v1/logbook/options", headers=_hdr(tok)).get_json()["data"]
    assert {"name": "Privat", "private": True} in d["purposes"]
    assert d["purposes"][0]["name"] == "Kunde"           # the pinned main purpose first
    assert d["edit"] is False and d["destinations"] == [] and d["visit_reasons"] == []


def test_edit_needs_the_admins_permission(env):
    app_module, db, adm, vids, tok, _ = env
    c = app_module.app.test_client()
    tid = _trip_id(db, "2026-11-05T10:00:00")
    assert c.post(f"/api/v1/trips/{tid}", headers=_hdr(tok), json={"purpose": "Kunde"}).status_code == 403
    app_module.app.config["WTF_CSRF_ENABLED"] = False
    assert adm.post(f"/api/admin/read-api/{vids[0]}/logbook-edit", json={"on": True}).get_json()["logbook_edit"] is True
    assert adm.get("/api/admin/read-api").get_json()["vehicles"][0]["logbook_edit"] is True
    app_module.app.config["WTF_CSRF_ENABLED"] = True
    r = c.post(f"/api/v1/trips/{tid}", headers=_hdr(tok),
               json={"purpose": "Kunde", "destination": " Firma B ", "visit_reason": "Wartung", "note": "Termin 10 Uhr"})
    assert r.status_code == 200, r.get_json()
    item = r.get_json()["data"]
    assert item["purpose"] == "Kunde" and item["destination"] == "Firma B" and item["note"] == "Termin 10 Uhr"
    row = db.execute("SELECT purpose, destination, visit_reason, note FROM trips WHERE id = ?", (tid,)).fetchone()
    assert tuple(row) == ("Kunde", "Firma B", "Wartung", "Termin 10 Uhr")
    # "" clears a field; the server's own logbook shows the change
    assert c.post(f"/api/v1/trips/{tid}", headers=_hdr(tok), json={"note": ""}).status_code == 200
    assert db.execute("SELECT note FROM trips WHERE id = ?", (tid,)).fetchone()[0] is None
    d = c.get("/api/v1/logbook?year=2026&month=11", headers=_hdr(tok)).get_json()["data"]
    assert d["edit"] is True and [row["kind"] for row in d["rows"]][-1] == "trip"


def test_edit_rules(env):
    app_module, db, adm, vids, tok, tok2 = env
    app_module.app.config["WTF_CSRF_ENABLED"] = False
    adm.post(f"/api/admin/read-api/{vids[0]}/logbook-edit", json={"on": True})
    app_module.app.config["WTF_CSRF_ENABLED"] = True
    c = app_module.app.test_client()
    tid = _trip_id(db, "2026-11-05T10:00:00")
    other = _trip_id(db, "2026-11-03T08:00:00", "dev2")
    assert c.post(f"/api/v1/trips/{other}", headers=_hdr(tok), json={"purpose": "x"}).status_code == 404
    assert c.post(f"/api/v1/trips/{tid}", headers=_hdr(tok2), json={"purpose": "x"}).status_code == 403
    assert c.post(f"/api/v1/trips/{tid}", headers=_hdr(tok), json={}).status_code == 400
    assert c.post(f"/api/v1/trips/{tid}", headers=_hdr(tok), json={"purpose": 5}).status_code == 400
    assert c.post(f"/api/v1/trips/{tid}", headers=_hdr(tok), json={"note": "x" * 1001}).status_code == 400
    # other columns are not reachable
    assert c.post(f"/api/v1/trips/{tid}", headers=_hdr(tok), json={"distance_km": 1}).status_code == 400
    assert db.execute("SELECT distance_km FROM trips WHERE id = ?", (tid,)).fetchone()[0] == 20.0


def test_new_code_keeps_permission_and_config_tells_it(env):
    app_module, db, adm, vids, tok, _ = env
    app_module.app.config["WTF_CSRF_ENABLED"] = False
    adm.post(f"/api/admin/read-api/{vids[0]}/logbook-edit", json={"on": True})
    new = adm.post(f"/api/admin/read-api/{vids[0]}/key", json={}).get_json()["token"]
    c = app_module.app.test_client()
    cfg = c.get("/api/v1/config", headers=_hdr(new)).get_json()
    assert cfg["logbook"] == {"edit": True} and "logbook" in cfg["modules"]
    assert c.get("/api/v1/config", headers=_hdr(tok)).status_code == 401   # the old code is gone
    assert adm.post(f"/api/admin/read-api/{vids[1] + 99}/logbook-edit", json={"on": True}).status_code == 404
