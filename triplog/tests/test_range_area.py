"""Road-based range area from an own Valhalla (1.2.273): request format,
background computation with cache, failure back-off, API validation."""

import time

import pytest


GJ = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"contour": 294, "metric": "distance"},
     "geometry": {"type": "Polygon", "coordinates": [[[13, 52], [14, 52], [14, 53], [13, 52]]]}}]}


def _wait(ra, k, s=2.0):
    t0 = time.time()
    while k in ra._busy and time.time() - t0 < s:
        time.sleep(0.01)


def test_off_without_valhalla(monkeypatch):
    import config
    import range_area as ra
    monkeypatch.setattr(config, "VALHALLA_URL", "")
    assert ra.area(52.5, 13.4, [200]) == {"status": "off"}


def test_request_uses_distance_contours_ascending():
    import range_area as ra
    b = ra.request_body(52.5, 13.4, [294, 198])
    assert b["costing"] == "auto" and b["polygons"] is True
    assert b["contours"] == [{"distance": 198.0}, {"distance": 294.0}]
    assert b["locations"] == [{"lat": 52.5, "lon": 13.4}]


def test_pending_then_cached_and_grid(monkeypatch):
    import config
    import range_area as ra
    monkeypatch.setattr(config, "VALHALLA_URL", "http://valhalla:8002")
    ra._cache.clear()
    monkeypatch.setattr(ra, "_down_until", 0.0)
    calls = []
    monkeypatch.setattr(ra, "_fetch", lambda lat, lon, kms: calls.append(kms) or GJ)
    assert ra.area(52.5, 13.4, [198, 294]) == {"status": "pending"}
    _wait(ra, ra.key(52.5, 13.4, [198, 294]))
    assert ra.area(52.5, 13.4, [198, 294]) == {"status": "ok", "geojson": GJ}
    # a few hundred metres and a few km of range further: same cache entry
    assert ra.area(52.505, 13.405, [201, 292])["status"] == "ok"
    assert len(calls) == 1


def test_failure_is_remembered(monkeypatch):
    import config
    import range_area as ra
    monkeypatch.setattr(config, "VALHALLA_URL", "http://valhalla:8002")
    ra._cache.clear()
    monkeypatch.setattr(ra, "_down_until", 0.0)
    def boom(*a):
        raise OSError("connection refused")
    monkeypatch.setattr(ra, "_fetch", boom)
    assert ra.area(54.0, 10.0, [250])["status"] == "pending"
    _wait(ra, ra.key(54.0, 10.0, [250]))
    r = ra.area(54.0, 10.0, [250])
    assert r["status"] == "error" and "connection refused" in r["error"]


def test_unreachable_backs_off_for_all_positions(monkeypatch):
    """Connection refused while Valhalla still builds: one warning, then no
    request for any other position until the back-off ends."""
    import urllib.error
    import config
    import range_area as ra
    monkeypatch.setattr(config, "VALHALLA_URL", "http://valhalla:8002")
    ra._cache.clear()
    monkeypatch.setattr(ra, "_down_until", 0.0)
    calls = []
    def refused(lat, lon, kms):
        calls.append(kms)
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    monkeypatch.setattr(ra, "_fetch", refused)
    assert ra.area(54.0, 10.0, [250])["status"] == "pending"
    _wait(ra, ra.key(54.0, 10.0, [250]))
    assert ra._down_until > time.time()
    # another position and range: no new request
    assert ra.area(48.2, 16.4, [180, 260])["status"] == "error"
    assert len(calls) == 1


def test_api_validates(app_module, db, monkeypatch):
    import config
    db.execute("INSERT INTO users (id, username, password_hash, is_admin) VALUES (1, 'admin', 'x', 1)")
    db.commit()
    monkeypatch.setattr(config, "VALHALLA_URL", "")
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        assert c.get("/api/range-area?lat=52.5&lon=13.4&km=198,294").get_json() == {"status": "off"}
        assert c.get("/api/range-area?lat=52.5&lon=13.4&km=2").status_code == 400
        assert c.get("/api/range-area?lat=x&lon=13.4&km=200").status_code == 400
        assert c.get("/api/range-area?lat=52.5&lon=13.4&km=1,2,3").status_code == 400


# ── pre-rendered area per vehicle (1.2.279) ─────────────────────────────────

def test_render_if_due_stores_and_throttles(app_module, db, monkeypatch):
    import range_area as ra
    ra._fail_at.clear()
    monkeypatch.setattr(ra, "_down_until", 0.0)
    calls = []
    monkeypatch.setattr(ra, "_fetch", lambda lat, lon, kms: calls.append(kms) or GJ)
    assert ra.load_last(db, "dev1") is None
    assert ra.render_if_due(db, "dev1", 52.5, 13.4, [198, 294], now=1000) == "rendered"
    rec = ra.load_last(db, "dev1")
    assert rec["geojson"] == GJ and rec["kms"] == [198, 294] and rec["ts"] == 1000
    # same place and range: nothing to do, also much later
    assert ra.render_if_due(db, "dev1", 52.501, 13.401, [199, 292], now=99999) == "unchanged"
    # moved: waits until 10 min after the last render, then renders
    assert ra.render_if_due(db, "dev1", 53.0, 10.0, [198, 294], now=1000 + 300) == "wait"
    assert ra.render_if_due(db, "dev1", 53.0, 10.0, [198, 294], now=1000 + 601) == "rendered"
    assert len(calls) == 2
    assert ra.load_last(db, "dev1")["lat"] == 53.0


def test_render_failure_keeps_last(app_module, db, monkeypatch):
    import range_area as ra
    ra._fail_at.clear()
    monkeypatch.setattr(ra, "_down_until", 0.0)
    monkeypatch.setattr(ra, "_fetch", lambda lat, lon, kms: GJ)
    ra.render_if_due(db, "dev1", 52.5, 13.4, [250], now=1000)
    def boom(*a):
        raise OSError("timeout")
    monkeypatch.setattr(ra, "_fetch", boom)
    assert ra.render_if_due(db, "dev1", 48.2, 16.4, [250], now=5000) == "failed"
    assert ra.load_last(db, "dev1")["lat"] == 52.5
    # no new attempt for 10 min after a failure
    assert ra.render_if_due(db, "dev1", 48.2, 16.4, [250], now=5300) == "wait"
    ra._fail_at.clear()


def test_last_endpoint(app_module, db, monkeypatch):
    import config
    import range_area as ra
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'dev1')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) "
               "VALUES (1, 'admin', 'x', 1, 1)")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        monkeypatch.setattr(config, "VALHALLA_URL", "")
        assert c.get("/api/range-area/last").get_json() == {"status": "off"}
        monkeypatch.setattr(config, "VALHALLA_URL", "http://valhalla:8002")
        assert c.get("/api/range-area/last").get_json() == {"status": "none"}
        monkeypatch.setattr(ra, "_down_until", 0.0)
        monkeypatch.setattr(ra, "_fetch", lambda lat, lon, kms: GJ)
        ra.render_if_due(db, "dev1", 52.5, 13.4, [250], now=1000)
        d = c.get("/api/range-area/last").get_json()
        assert d["status"] == "ok" and d["ts"] == 1000
        assert d["geojson"]["features"][1]["properties"]["kind"] == "line"   # cleaned outline


def test_range_inputs_bms_or_fallback(app_module, db, monkeypatch):
    db.execute("INSERT INTO vehicles (id, plate, device, bms_learn) VALUES (1, 'EV1', 'dev1', 0)")
    db.commit()
    v = db.execute("SELECT * FROM vehicles WHERE id = 1").fetchone()
    monkeypatch.setattr(app_module, "_vehicle_state",
                        lambda dev: {"lat": 52.5, "lon": 13.4, "calc_range": 240})
    assert app_module._range_inputs(db, v) == (52.5, 13.4, [240])
    monkeypatch.setattr(app_module, "_vehicle_state", lambda dev: {"calc_range": 240})
    assert app_module._range_inputs(db, v) is None


def test_area_from_an_older_version_is_rendered_again(app_module, db, monkeypatch):
    import json
    import range_area as ra
    ra._fail_at.clear()
    monkeypatch.setattr(ra, "_down_until", 0.0)
    monkeypatch.setattr(ra, "_fetch", lambda lat, lon, kms: GJ)
    assert ra.render_if_due(db, "dev3", 52.5, 13.4, [250], now=1000) == "rendered"
    assert ra.render_if_due(db, "dev3", 52.5, 13.4, [250], now=1100) == "unchanged"
    rec = ra.load_last(db, "dev3")
    rec.pop("v")                                        # as stored by 1.2.289
    db.execute("UPDATE range_area_last SET data = ? WHERE device = 'dev3'", (json.dumps(rec),))
    db.commit()
    assert ra.render_if_due(db, "dev3", 52.5, 13.4, [250], now=1200) == "rendered"
