"""FIXES 15.2 — vehicle-driven visibility of trip tags.

get_purpose_meta / get_preset_values, when `device` is set, return only vocabulary
relevant for that vehicle: used on a trip of the vehicle (automatically) OR is_main
OR explicitly assigned to the user. Without `device` = full catalog
(admin/management view). This keeps the dropdown small when there are multiple
vehicles/users, without altering the shared trips.
"""
import pytest


@pytest.fixture
def seeded(db):
    db.execute("INSERT INTO users (username, password_hash, is_admin) VALUES ('u', 'x', 0)")
    uid = db.execute("SELECT id FROM users ORDER BY id DESC LIMIT 1").fetchone()[0]
    db.executescript("""
        INSERT INTO purpose_meta (name) VALUES ('Arbeitsweg');
        INSERT INTO purpose_meta (name) VALUES ('Einkauf');
        INSERT INTO purpose_meta (name, is_main) VALUES ('Privat', 1);
        INSERT INTO trips (device, start_time, end_time, purpose, destination)
            VALUES ('dev1','2026-06-01T08:00:00','2026-06-01T08:30:00','Arbeitsweg','Büro');
        INSERT INTO trips (device, start_time, end_time, purpose)
            VALUES ('dev2','2026-06-01T09:00:00','2026-06-01T09:30:00','Einkauf');
        INSERT INTO preset_values (field, value) VALUES ('destination','Büro');
    """)
    db.commit()
    return db, uid


def test_purposes_scoped_to_vehicle_usage(app_module, seeded):
    db, uid = seeded
    names = lambda dev: sorted(p["name"] for p in app_module.get_purpose_meta(db, user_id=uid, device=dev))
    # used-on-vehicle + is_main (Privat) appear, vocabulary of other vehicle does not
    assert names("dev1") == ["Arbeitsweg", "Privat"]
    assert names("dev2") == ["Einkauf", "Privat"]


def test_full_catalog_without_device(app_module, seeded):
    db, _ = seeded
    # device=None -> admin/management view sees everything
    assert sorted(p["name"] for p in app_module.get_purpose_meta(db)) == ["Arbeitsweg", "Einkauf", "Privat"]


def test_explicit_assignment_makes_visible(app_module, seeded):
    db, uid = seeded
    # 'Einkauf' is not used on dev1 -> normally invisible...
    assert "Einkauf" not in [p["name"] for p in app_module.get_purpose_meta(db, user_id=uid, device="dev1")]
    # ...until the admin assigns it to the user (matrix, 15.7) — no second catalog entry
    db.execute("INSERT INTO purpose_visibility (name, user_id) VALUES ('Einkauf', ?)", (uid,))
    db.commit()
    assert "Einkauf" in [p["name"] for p in app_module.get_purpose_meta(db, user_id=uid, device="dev1")]
    assert db.execute("SELECT COUNT(*) FROM purpose_meta WHERE name='Einkauf'").fetchone()[0] == 1


def test_preset_values_scoped_to_vehicle(app_module, seeded):
    db, uid = seeded
    assert app_module.get_preset_values(db, "destination", device="dev1", user_id=uid) == ["Büro"]
    assert app_module.get_preset_values(db, "destination", device="dev2", user_id=uid) == []
    # full catalog without device
    assert app_module.get_preset_values(db, "destination") == ["Büro"]


def _client(app_module, db):
    """Test client with logged-in admin (from setup) — db fixture shares the DB."""
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    c = app_module.app.test_client()
    db.execute("INSERT INTO users (username, password_hash, is_admin) VALUES ('admin', ?, 1)",
               (app_module.generate_password_hash("Sup3r!secret"),))
    db.execute("INSERT INTO users (username, password_hash, is_admin) VALUES ('bob', ?, 0)",
               (app_module.generate_password_hash("Sup3r!secret"),))
    db.commit()
    return c


def test_value_management_user_scoped(app_module, db):
    """FIXES 15.6: everyone may rename/delete their own tags (created_by), admin
    all. Renaming a user purpose WITH a visibility row must not fail on the FK
    (purpose_visibility without ON UPDATE CASCADE -> defer_foreign_keys)."""
    c = _client(app_module, db)
    bob = app_module.app.test_client(); bob.post("/login", data={"username": "bob", "password": "Sup3r!secret"})
    adm = app_module.app.test_client(); adm.post("/login", data={"username": "admin", "password": "Sup3r!secret"})

    # bob creates his own purpose -> created_by + visibility row
    assert bob.post("/api/purpose-meta", json={"name": "BobPendeln"}).status_code == 200
    assert db.execute("SELECT COUNT(*) FROM purpose_visibility WHERE name='BobPendeln'").fetchone()[0] == 1
    # bob renames his own (FK-critical because of visibility row)
    assert bob.post("/api/admin/rename", json={"field": "purpose", "old_name": "BobPendeln", "new_name": "BobArbeit"}).status_code == 200
    assert db.execute("SELECT name FROM purpose_visibility").fetchone()[0] == "BobArbeit"  # visibility followed
    # foreign/legacy purpose -> bob 403, admin 200
    db.execute("INSERT INTO purpose_meta (name) VALUES ('Legacy')"); db.commit()
    assert bob.post("/api/admin/rename", json={"field": "purpose", "old_name": "Legacy", "new_name": "X"}).status_code == 403
    assert bob.post("/api/admin/delete-value", json={"field": "purpose", "name": "Legacy", "confirm": "Legacy"}).status_code == 403
    assert adm.post("/api/admin/delete-value", json={"field": "purpose", "name": "Legacy", "confirm": "Legacy"}).status_code == 200
    # bob deletes his own
    assert bob.post("/api/admin/delete-value", json={"field": "purpose", "name": "BobArbeit", "confirm": "BobArbeit"}).status_code == 200
