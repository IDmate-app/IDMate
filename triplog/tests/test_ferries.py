"""Long ferries on request (1.2.297): found by routing, terminal reachable
with energy left, range area around the port of arrival."""

import ferries
import wind_range as wr
from test_wind_range import MODEL, _enc

CRUISE = {"left": 50.0, "model": MODEL, "temp": 10, "v_eval": 110}


def _ferry_route(lat0, lon0, to_terminal=80, sea=250, after=100, n=200):
    total = to_terminal + sea + after
    pts = [wr._ll(lat0, lon0, 0.0, total * i / n) for i in range(n + 1)]
    i1 = round(n * to_terminal / total)
    i2 = round(n * (to_terminal + sea) / total)
    return {"trip": {"legs": [{"shape": _enc(pts), "maneuvers": [
        {"length": to_terminal, "time": to_terminal / 100 * 3600, "begin_shape_index": 0, "end_shape_index": i1},
        {"type": 28, "length": sea, "time": 9 * 3600, "begin_shape_index": i1, "end_shape_index": i2,
         "street_names": ["TT-Line"]},
        {"length": after, "time": after / 100 * 3600, "begin_shape_index": i2, "end_shape_index": n}]}]}}


def test_discover_finds_long_ferry_once():
    def fn(la0, lo0, la1, lo1):
        if la1 > la0 + 3:                      # only targets far north go by ferry
            return _ferry_route(la0, lo0)
        raise OSError("no route")
    found = ferries.discover(53.8, 10.4, route_fn=fn)
    assert len(found) == 1                     # same terminals from many targets: one ferry
    f = found[0]
    assert abs(f["km"] - 250) <= 3 and f["name"] == "TT-Line" and abs(f["via_km"] - 80) < 3


def test_short_ferries_are_ignored():
    found = ferries.discover(53.8, 10.4, route_fn=lambda *a: _ferry_route(53.8, 10.4, sea=19))
    assert found == []


def test_run_computes_range_after_arrival():
    calls = []
    def area(lat, lon, kms):
        calls.append(kms)
        return None
    ferries._run("devF", 53.8, 10.4, CRUISE, [], route_fn=lambda *a: _ferry_route(53.8, 10.4), area_fn=area)
    st = ferries.status("devF")
    assert st["status"] == "ok" and len(st["ferries"]) == 1
    f = st["ferries"][0]
    full = wr.range_along_route(50.0, MODEL, 10, wr.route_profile(_ferry_route(53.8, 10.4)), [], 0, until_km=0.0)
    assert 0 < f["kwh_left"] < full                # energy used up to the terminal, none on board
    assert f["km_after"] > 50 and calls == [[f["km_after"]]]


def test_terminal_out_of_reach_is_skipped():
    ferries._run("devG", 53.8, 10.4, dict(CRUISE, left=5.0), [],
                 route_fn=lambda *a: _ferry_route(53.8, 10.4, to_terminal=150), area_fn=lambda *a: (None, None))
    assert ferries.status("devG")["ferries"] == []


def test_endpoints_off_and_no_model(app_module, db, monkeypatch):
    import config
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'dev1')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) "
               "VALUES (1, 'admin', 'x', 1, 1)")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        monkeypatch.setattr(config, "VALHALLA_URL", "")
        assert c.post("/api/ferries/start").get_json() == {"status": "off"}
        monkeypatch.setattr(config, "VALHALLA_URL", "http://valhalla:8002")
        monkeypatch.setattr(app_module, "_vehicle_state", lambda dev: {"lat": 53.8, "lon": 10.4, "calc_range": 200})
        assert c.post("/api/ferries/start").get_json()["error"] == "no_model"
        assert "lat" not in c.get("/api/ferries").get_json()
