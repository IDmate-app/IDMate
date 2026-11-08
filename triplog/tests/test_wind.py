"""Wind forecast raster (1.2.284): fixed raster independent of the car's
position, batched Open-Meteo fetch, refresh after 6 h, value of the hour."""

import json

import pytest


def test_raster_is_fixed_and_round():
    import wind
    a = set(wind.grid(53.80, 10.40, 100))
    b = set(wind.grid(53.83, 10.47, 100))          # car moved a few km
    assert len(a & b) > 0.8 * len(a)                # same raster points
    # spacing about 50 km between neighbours in a row
    row = sorted(p for p in a if p[0] == sorted(a)[len(a) // 2][0])
    d = wind._dist_km(row[0][0], row[0][1], row[1][0], row[1][1])
    assert 48 < d < 52
    # the car position itself is not one of the points
    assert (53.80, 10.40) not in a


def test_ensure_fetches_once_and_refreshes(app_module, db, monkeypatch):
    import config
    import wind
    monkeypatch.setattr(config, "WIND_SOURCE", "open-meteo")
    monkeypatch.setattr(wind, "_down_until", 0.0)
    calls = []
    def fake(points):
        calls.append(len(points))
        return [{"t": [1000, 4600], "kmh": [20, 30], "dir": [270, 280], "gust": [35, 45], "temp": [7, 8]}
                for _ in points]
    monkeypatch.setattr(wind, "_fetch", fake)
    n = wind.ensure(db, 53.8, 10.4, 150, now=2000)
    assert n == len(wind.grid(53.8, 10.4, 150)) and all(c <= wind.BATCH for c in calls)
    assert wind.ensure(db, 53.8, 10.4, 150, now=2000 + 3600) == 0        # still fresh
    assert wind.ensure(db, 53.8, 10.4, 150, now=2000 + 7 * 3600) == n    # after 6 h
    pts = wind.current(db, 53.8, 10.4, 150, now=2000)
    assert pts and pts[0]["kmh"] == 20 and pts[0]["dir"] == 270 and pts[0]["gust"] == 35


def test_ensure_backs_off_on_failure(app_module, db, monkeypatch):
    import config
    import wind
    monkeypatch.setattr(config, "WIND_SOURCE", "open-meteo")
    monkeypatch.setattr(wind, "_down_until", 0.0)
    calls = []
    def boom(points):
        calls.append(1)
        raise OSError("no network")
    monkeypatch.setattr(wind, "_fetch", boom)
    assert wind.ensure(db, 53.8, 10.4, 100, now=1000) == 0
    assert wind.ensure(db, 53.8, 10.4, 100, now=1500) == 0
    assert len(calls) == 1


def test_off_by_default(app_module, db, monkeypatch):
    import config
    import wind
    monkeypatch.setattr(config, "WIND_SOURCE", "")
    assert wind.ensure(db, 53.8, 10.4, 100) == 0
    db.execute("INSERT INTO users (id, username, password_hash, is_admin) VALUES (1, 'admin', 'x', 1)")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        assert c.get("/api/wind").get_json() == {"status": "off"}


def test_fetch_parses_open_meteo(monkeypatch):
    import wind
    body = json.dumps([{"latitude": 53.8, "longitude": 10.4, "hourly": {
        "time": [1000], "wind_speed_10m": [22.5], "wind_direction_10m": [250],
        "wind_gusts_10m": [40], "temperature_2m": [6.5]}}] * 2).encode()
    class R:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return body
    seen = {}
    monkeypatch.setattr(wind.urllib.request, "urlopen", lambda req, timeout: seen.setdefault("url", req.full_url) and R())
    out = wind._fetch([(53.8, 10.4), (54.2, 10.4)])
    assert "latitude=53.800%2C54.200" in seen["url"] and "forecast_hours=24" in seen["url"]
    assert out[0]["kmh"] == [22.5] and out[1]["dir"] == [250]


def test_page_carries_area_and_wind(app_module, db, monkeypatch):
    """1.2.293: stored area and the wind of this hour come with the page."""
    import config
    import re
    import time
    import range_area as ra
    import wind
    monkeypatch.setattr(config, "VALHALLA_URL", "http://valhalla:8002")
    monkeypatch.setattr(config, "WIND_SOURCE", "open-meteo")
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'dev1')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) "
               "VALUES (1, 'admin', 'x', 1, 1)")
    rec = {"key": [0, 0, [200]], "lat": 53.8, "lon": 10.4, "kms": [200], "ts": 1, "v": ra.GEOM_VERSION,
           "geojson": {"type": "FeatureCollection", "features": []}}
    db.execute("INSERT INTO range_area_last (device, data, ts) VALUES ('dev1', ?, 1)", (json.dumps(rec),))
    h = int(time.time() // 3600 * 3600)
    for la, lo in wind.grid(53.8, 10.4, 200)[:5]:
        db.execute("INSERT INTO wind_points (key, lat, lon, fetched, data) VALUES (?, ?, ?, ?, ?)",
                   (wind._key((la, lo)), la, lo, time.time(),
                    json.dumps({"t": [h], "kmh": [20], "dir": [270], "gust": [30], "temp": [9]})))
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        html = c.get("/").get_data(as_text=True)
    boot = json.loads(re.search(r'id="mapBoot">(.*?)</script>', html, re.S).group(1))
    assert boot["area"]["status"] == "ok" and boot["area"]["kms"] == [200]
    assert len(boot["wind"]["points"]) == 5
