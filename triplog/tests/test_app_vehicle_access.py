"""Per-user vehicle visibility (admin Users tab): which vehicles a user
sees. No assignment = all (backward compat); assignment = exactly those;
admins always all. Setting a disallowed vehicle active -> 403."""
import pytest


def _setup(app_module, db):
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    for nm, pl, dv in [("Auto1", "A-1", "dev1"), ("Auto2", "A-2", "dev2"), ("Auto3", "A-3", "dev3")]:
        db.execute("INSERT INTO vehicles (name, plate, device) VALUES (?, ?, ?)", (nm, pl, dv))
    db.execute("INSERT INTO users (username, password_hash, is_admin) VALUES ('admin', ?, 1)",
               (app_module.generate_password_hash("Sup3r!secret"),))
    db.execute("INSERT INTO users (username, password_hash, is_admin) VALUES ('bob', ?, 0)",
               (app_module.generate_password_hash("Sup3r!secret"),))
    db.commit()
    vids = [r[0] for r in db.execute("SELECT id FROM vehicles ORDER BY id")]
    bob_id = db.execute("SELECT id FROM users WHERE username='bob'").fetchone()[0]
    adm = app_module.app.test_client(); adm.post("/login", data={"username": "admin", "password": "Sup3r!secret"})
    bob = app_module.app.test_client(); bob.post("/login", data={"username": "bob", "password": "Sup3r!secret"})
    return adm, bob, vids, bob_id


def test_vehicle_access_restriction(app_module, db):
    adm, bob, vids, bob_id = _setup(app_module, db)
    # without assignment bob sees all 3
    assert len(bob.get("/api/vehicles").get_json()) == 3
    # admin restricts to Auto1+Auto2
    r = adm.post(f"/api/admin/users/{bob_id}/vehicles", json={"vehicle_ids": [vids[0], vids[1]]})
    assert r.status_code == 200
    plates = sorted(v["plate"] for v in bob.get("/api/vehicles").get_json())
    assert plates == ["A-1", "A-2"]
    # disallowed vehicle cannot be set active
    assert bob.post("/api/user/active-vehicle", json={"vehicle_id": vids[2]}).status_code == 403
    assert bob.post("/api/user/active-vehicle", json={"vehicle_id": vids[0]}).status_code == 200
    # list_users shows the assignment
    u = [u for u in adm.get("/api/admin/users").get_json() if u["username"] == "bob"][0]
    assert sorted(u["vehicle_ids"]) == sorted([vids[0], vids[1]])
    # empty list = all (reset)
    adm.post(f"/api/admin/users/{bob_id}/vehicles", json={"vehicle_ids": []})
    assert len(bob.get("/api/vehicles").get_json()) == 3


def test_admin_sees_all_vehicles(app_module, db):
    adm, bob, vids, bob_id = _setup(app_module, db)
    # even if an admin had assignment rows, they see all
    admin_id = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
    db.execute("INSERT INTO user_vehicle_access (user_id, vehicle_id) VALUES (?, ?)", (admin_id, vids[0]))
    db.commit()
    assert len(adm.get("/api/vehicles").get_json()) == 3
