"""Admin → Interfaces: consumption model view per vehicle and the counter of
requests to outside services (api_usage.py)."""
from datetime import date, datetime, timedelta

import pytest


@pytest.fixture
def env(app_module, db):
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    app_module._login_attempts.clear()
    db.execute("INSERT INTO vehicles (name, plate, device) VALUES ('ID.7', 'ID-7', 'dev1')")
    for name, admin in (("admin", 1), ("bob", 0)):
        db.execute("INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
                   (name, app_module.generate_password_hash("Sup3r!secret"), admin))
    db.commit()
    adm = app_module.app.test_client()
    adm.post("/login", data={"username": "admin", "password": "Sup3r!secret"})
    bob = app_module.app.test_client()
    bob.post("/login", data={"username": "bob", "password": "Sup3r!secret"})
    yield app_module, db, adm, bob
    app_module._login_attempts.clear()


def test_api_usage_counts_per_day_and_month(env):
    import api_usage
    app_module, db, adm, bob = env
    api_usage.count("open-meteo", 1, units=37)
    api_usage.count("open-meteo", 1, units=3)
    api_usage.count("dwd")
    api_usage.count("dwd")
    rows = {r["provider"]: r for r in adm.get("/api/admin/api-usage").get_json()}
    assert rows["open-meteo"]["requests_day"] == 2 and rows["open-meteo"]["units_day"] == 40
    assert rows["open-meteo"]["limit_day"] == 10000 and rows["open-meteo"]["limit_month"] == 300000
    assert rows["dwd"]["requests_month"] == 2 and rows["dwd"]["limit_day"] is None
    assert rows["github"]["requests_day"] == 0
    hist = adm.get("/api/admin/api-usage/dwd").get_json()
    assert len(hist) == 60 and hist[-1] == {"day": date.today().isoformat(), "requests": 2, "units": 2}
    assert adm.get("/api/admin/api-usage/nope").status_code == 404
    assert bob.get("/api/admin/api-usage").status_code == 403


def test_count_never_breaks_the_caller(monkeypatch):
    import api_usage
    import config
    monkeypatch.setattr(config, "DB_PATH", "/nonexistent/dir/x.db")
    api_usage.count("dwd")                      # no exception


def test_model_view(env):
    app_module, db, adm, bob = env
    t0 = datetime.now() - timedelta(days=60)
    k = 0
    for i in range(40):
        v = 40 + (i * 13) % 90
        km = 30 + i % 20
        h = km / v
        cons = 14 + 9 * (v / 100) ** 2
        start = t0 + timedelta(hours=8 * i)
        db.execute("INSERT INTO trips (device, start_time, end_time, distance_km, energy_kwh, temp_c, rain_level) "
                   "VALUES ('dev1', ?, ?, ?, ?, ?, ?)",
                   (start.isoformat(timespec="seconds"), (start + timedelta(hours=h)).isoformat(timespec="seconds"),
                    km, cons * km / 100, 5 + i % 20, i % 4))
        k += 1
    db.commit()
    vs = adm.get("/api/admin/consumption-model").get_json()
    assert [v["name"] for v in vs] == ["ID.7"]
    d = adm.get(f"/api/admin/consumption-model/{vs[0]['id']}").get_json()
    assert d["have"]["trips"] == 40 and d["need"]["trips"] == 20
    assert d["model"] and d["says"]["speed"][0]["v"] == 30
    w = {x["w"]: x["kwh100"] for x in d["says"]["wind"]}
    assert w[20] > w[0] > w[-20]                              # head wind costs, tail wind saves
    assert set(d["rain_trips"]) == {"0", "1", "2", "3"}
    assert len(d["trips"]) == 40 and all("model" in t for t in d["trips"])
    assert "lat" not in str(d["trips"][0])
    assert bob.get(f"/api/admin/consumption-model/{vs[0]['id']}").status_code == 403
    r = adm.get(f"/api/bms-learn/export?vehicle={vs[0]['id']}")
    assert r.status_code == 200 and "idmate-model-" in r.headers["Content-Disposition"]
    assert '"rain"' in r.get_data(as_text=True)


def test_admin_page_has_the_sections(env):
    app_module, db, adm, bob = env
    html = adm.get("/admin").get_data(as_text=True)
    assert 'id="mdlVehicle"' in html and 'id="apiuList"' in html and "const MDL_T" in html
