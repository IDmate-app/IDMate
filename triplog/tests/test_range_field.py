"""Range area from routes that end at the edge of the range (1.2.316): per
bearing the place where a route arrives with nothing left, found with two
or three routes; the area is one Valhalla contour cut to that rim."""

import json
import math

import range_field as rf
import wind_range as wr
from test_wind_range import MODEL, _circle, _enc

LAT, LON = 53.8, 10.4
N = wr.N_DIRS


def _bearing(lat0, lon0, lat1, lon1):
    x, y = wr._xy(lat0, lon0, lat1, lon1)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _road(speed_of=lambda th: 130.0, detour=1.25, n=120):
    """route_fn: a straight road to the target, `detour` × as long as the
    crow flies (the shape wiggles sideways), at speed_of(bearing) km/h."""
    def fn(lat0, lon0, lat1, lon1, use_ferry=None):
        th = _bearing(lat0, lon0, lat1, lon1)
        pts = [(lat0 + (lat1 - lat0) * i / n, lon0 + (lon1 - lon0) * i / n) for i in range(n + 1)]
        x, y = wr._xy(lat0, lon0, lat1, lon1)
        km = math.hypot(x, y)
        return {"trip": {"legs": [{"shape": _enc(pts), "maneuvers": [
            {"length": km, "time": km / speed_of(th) * 3600, "begin_shape_index": 0, "end_shape_index": n}]}]}}
    return fn


def _range(kwh, speed, detour=1.0):
    """Reach as the crow flies on a straight road at one speed (walked like the code does)."""
    prof = wr.route_profile(_road(lambda th: speed)(LAT, LON, *rf._target(LAT, LON, 0.0, 600.0)))
    return wr.range_along_route(kwh, MODEL, 15, prof, [], 0) / detour


def test_edge_is_where_a_route_arrives_with_nothing_left():
    f = rf.probe(40.0, MODEL, 15, LAT, LON, [], 0, [190] * N, route_fn=_road())
    want = _range(40.0, 130.0)
    assert f["used"] == 4 * N                              # the bearings in between as well, twice
    assert all(abs(r - want) < 6 for _, r, _ in f["rim"])  # every bearing: within a few km
    assert f["routes"] <= 3 * N + 2 * 3 * N                # the finer passes start at the edge: 1–2 routes each
    assert abs(f["dmax"] - want) < 6 and abs(f["safe"] - want) < 6
    assert rf.mask_km(f) % 5 == 0 and rf.mask_km(f) > f["dmax"]
    assert all(abs(d - want) < 6 for d in f["dirs"])


def test_slow_roads_reach_further_than_the_motorway():
    # east: motorway at 130, west: country roads at 80
    f = rf.probe(40.0, MODEL, 15, LAT, LON, [], 0, [190] * N, route_fn=_road(lambda th: 130.0 if th < 180 else 80.0))
    east = [r for th, r, _ in f["rim"] if 20 < th < 160]
    west = [r for th, r, _ in f["rim"] if 200 < th < 340]
    assert abs(sum(east) / len(east) - _range(40.0, 130.0)) < 6
    assert abs(sum(west) / len(west) - _range(40.0, 80.0)) < 6
    assert min(west) > max(east) * 1.1
    assert f["dirs"][3 * N // 4] > f["dirs"][N // 4] * 1.1


def test_where_the_land_ends_the_rim_goes_on_and_the_contour_cuts():
    # every road ends after 60 km (coast): energy is left there
    def short(lat0, lon0, lat1, lon1, use_ferry=None):
        th, r = rf._polar(lat0, lon0, lat1, lon1)
        return _road()(lat0, lon0, *rf._target(lat0, lon0, th, min(r, 60.0)))
    f = rf.probe(40.0, MODEL, 15, LAT, LON, [], 0, [190] * N, route_fn=short)
    assert all(r > 60 for _, r, _ in f["rim"])             # not pulled in to the coast …
    gj = rf.compose({"features": [_circle(LAT, LON, 60)]}, f["rim"], LAT, LON)
    assert max(_extent(gj, LAT, LON)) < 65                 # … the land itself is the limit
    assert f["routes"] <= 2 * 4 * N                        # and no third route into the sea
    # a coast reached with energy to spare is no measured range (1.2.318:
    # it made the "near by road" distance far too small)
    assert f["safe"] is None and rf.safe_km(f) is None


def test_wide_gap_behind_a_bay_is_searched_on():
    """Up to 110 km the land is reached directly; everything further out
    lies behind a bay, 150 km of detour away. Reached and not reached are
    far apart after three routes: the search goes on until the edge is
    pinned near the shore (1.2.318, Sonderburg)."""
    def bay(lat0, lon0, lat1, lon1, use_ferry=None):
        th, r = rf._polar(lat0, lon0, lat1, lon1)
        def leg(a, b, n=40):
            return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]
        if r <= 110:
            pts = leg((lat0, lon0), (lat1, lon1), 120)
        else:                                              # around the bay: 75 km out to the side and back
            shore = rf._target(lat0, lon0, th, 105.0)
            side = rf._target(shore[0], shore[1], th + 90.0, 75.0)
            pts = leg((lat0, lon0), shore) + leg(shore, side)[1:] + leg(side, shore)[1:] + leg(shore, (lat1, lon1))[1:]
        km = r if r <= 110 else r + 150.0
        return {"trip": {"legs": [{"shape": _enc(pts), "maneuvers": [
            {"length": km, "time": km / 130.0 * 3600, "begin_shape_index": 0, "end_shape_index": len(pts) - 1}]}]}}
    assert _range(40.0, 130.0) > 130                       # the energy would do for more than 110 km
    seen = []
    p, asked, by_energy = rf._edge(40.0, MODEL, 15, LAT, LON, 0.0, 190 / wr.DETOUR, [], 0, None, bay, seen)
    assert asked > rf.MAX_ROUTES and by_energy
    reached = max(a["r"] for a in seen if a["left"] >= 0)
    missed = min(a["r"] for a in seen if a["left"] < 0)
    assert missed - reached <= rf.GAP_KM and reached <= 110 < missed
    assert reached <= p[1] <= missed


def test_second_half_of_the_bearings_only_when_fast():
    t = [0.0]
    def clock():
        t[0] += 2.0                                        # every look at the clock: 2 s later
        return t[0]
    slow = rf.probe(40.0, MODEL, 15, LAT, LON, [], 0, [190] * N, route_fn=_road(), budget_s=1, clock=clock)
    assert slow["used"] == N
    t[0] = 0.0
    mid = rf.probe(40.0, MODEL, 15, LAT, LON, [], 0, [190] * N, route_fn=_road(), budget_s=6, clock=clock)
    assert mid["used"] == 2 * N                            # 2 s after the first pass, 4 s after the second
    fast = rf.probe(40.0, MODEL, 15, LAT, LON, [], 0, [190] * N, route_fn=_road(), budget_s=60, clock=clock)
    assert fast["used"] == 4 * N


def test_long_ferry_ends_the_range_at_the_terminal():
    n = 200
    def ferry(lat0, lon0, lat1, lon1, use_ferry=None):
        # 30 km of road to the quay, 100 km of ferry, the far shore from 130 km on
        th, r = rf._polar(lat0, lon0, lat1, lon1)
        if r < 130:                                        # target in the sea: the road ends at the quay
            return _road()(lat0, lon0, *rf._target(lat0, lon0, th, min(r, 30.0)))
        pts = [(lat0 + (lat1 - lat0) * i / n, lon0 + (lon1 - lon0) * i / n) for i in range(n + 1)]
        k0, k1 = round(n * 30.0 / r), round(n * 130.0 / r)
        return {"trip": {"legs": [{"shape": _enc(pts), "maneuvers": [
            {"length": 30.0, "time": 30.0 / 100 * 3600, "begin_shape_index": 0, "end_shape_index": k0},
            {"type": 28, "length": 100.0, "time": 9 * 3600, "begin_shape_index": k0, "end_shape_index": k1},
            {"length": r - 130.0, "time": (r - 130.0) / 100 * 3600, "begin_shape_index": k1, "end_shape_index": n}]}]}}
    f = rf.probe(40.0, MODEL, 15, LAT, LON, [], 0, [190] * N, route_fn=ferry)
    # the far shore (130 km) is never part of the range, although the energy
    # would do: the range ends at the quay, at most a little out into the sea
    assert f["used"] == 4 * N and all(28 < r < 60 for _, r, _ in f["rim"])
    assert all(28 < r < 38 for th, r, _ in f["rim"] if round(th / (360 / N), 3).is_integer())


def test_too_few_routes_is_no_rim():
    def none(*a, **k):
        raise OSError("no route")
    assert rf.probe(40.0, MODEL, 15, LAT, LON, [], 0, [190] * N, route_fn=none) is None


def _rim(reach):
    return [(i * 5.0, reach(i * 5.0), reach(i * 5.0) * 1.25) for i in range(72)]


def _extent(gj, lat0, lon0):
    line = [f for f in gj["features"] if f["properties"]["kind"] == "line"][0]
    pts = [wr._xy(lat0, lon0, la, lo) for ring in line["geometry"]["coordinates"] for lo, la in ring]
    return (max(p[0] for p in pts), -min(p[0] for p in pts), max(p[1] for p in pts), -min(p[1] for p in pts))


def test_compose_cuts_the_contour_to_the_rim():
    mask = {"features": [_circle(LAT, LON, 240)]}
    gj = rf.compose(mask, _rim(lambda th: 200.0 if th < 180 else 120.0), LAT, LON)
    east, west, north, south = _extent(gj, LAT, LON)
    assert 190 < east < 205 and 115 < west < 125           # each side its own reach
    line = [f for f in gj["features"] if f["properties"]["kind"] == "line"][0]
    assert len(line["geometry"]["coordinates"]) == 1       # one clean outline
    # the contour wins where the land ends before the energy does
    small = rf.compose({"features": [_circle(LAT, LON, 90)]}, _rim(lambda th: 200.0), LAT, LON)
    assert max(_extent(small, LAT, LON)) < 95
    assert rf.compose(mask, [], LAT, LON) is None and rf.compose({"features": []}, _rim(lambda th: 1.0), LAT, LON) is None


def test_inside_the_shortest_range_by_road_everything_stays():
    """A peninsula behind a bay: on its bearing the rim lies in the water
    before it, but by road it is nearer than the shortest range of any
    bearing — it stays (1.2.317)."""
    mask = _circle(LAT, LON, 240)
    near = _circle(LAT, LON, 150)                          # all within 150 road km
    rim = _rim(lambda th: 100.0 if th < 180 else 200.0)    # east: the edge already at 100 km
    cut = rf.compose({"features": [mask, near]}, rim, LAT, LON)
    kept = rf.compose({"features": [mask, near]}, rim, LAT, LON, safe=150)
    assert 95 < _extent(cut, LAT, LON)[0] < 110            # rim alone: the east ends at 100 km
    assert 145 < _extent(kept, LAT, LON)[0] < 155          # with the road distance: out to 150 km
    assert abs(_extent(kept, LAT, LON)[1] - _extent(cut, LAT, LON)[1]) < 3      # the west as before
    assert rf.safe_km({"safe": 152.0, "dmax": 228.0}) == 150
    assert rf.safe_km({"safe": None, "dmax": 228.0}) is None
    assert rf.safe_km({"safe": 236.0, "dmax": 228.0}) is None      # no room inside the mask: not needed


def test_rim_table_is_smooth_around_the_circle():
    t = rf.rim_table([(350.0, 100.0, 125.0), (10.0, 200.0, 250.0), (180.0, 100.0, 125.0)])
    assert len(t) == rf.TABLE
    assert abs(t[0] - 150.0) < 3                           # halfway between 350° and 10°, across north
    assert max(abs(a - b) for a, b in zip(t, t[1:] + t[:1])) < 4      # no jumps


def test_render_uses_the_field(app_module, db, monkeypatch):
    import range_area as ra
    ra._fail_at.clear()
    monkeypatch.setattr(ra, "_down_until", 0.0)
    asked = []
    def fetch(lat, lon, kms):
        asked.append(list(kms))
        return {"type": "FeatureCollection", "features": [_circle(lat, lon, k) for k in kms]}
    monkeypatch.setattr(ra, "_fetch", fetch)
    field = {"dirs": [150] * (N // 2) + [230] * (N // 2), "rim": _rim(lambda th: 180.0 if th < 180 else 120.0),
             "dmax": 228.0, "safe": 152.0, "used": 64, "routes": 130, "seconds": 1.0}
    calls = []
    def field_fn():
        calls.append(1)
        return field
    assert ra.render_if_due(db, "devF", LAT, LON, [180], now=1000, field_fn=field_fn) == "rendered"
    rec = ra.load_last(db, "devF")
    assert asked == [[150, 240]]                           # the mask a little beyond dmax, and the shortest range
    assert rec["dirs"] == field["dirs"] and rec["kms"] == [180]
    east, west, _, _ = _extent(rec["geojson"], LAT, LON)
    assert 170 < east < 185 and 145 < west < 155           # west: rim at 120, but all within 150 road km stays
    # nothing changed: no routes, no render
    assert ra.render_if_due(db, "devF", LAT, LON, [180], now=5000, field_fn=field_fn) == "unchanged"
    assert len(calls) == 1
    # no field (too few routes): the contour as before
    assert ra.render_if_due(db, "devG", LAT, LON, [180], now=1000, field_fn=lambda: None) == "rendered"
    assert ra.load_last(db, "devG")["geojson"]["features"]


def test_route_request_asks_for_the_maneuvers(monkeypatch):
    """Without the narrative Valhalla sends no maneuvers: no speed per
    section, no ferries (the routes then counted as flat 110 km/h)."""
    import io
    import urllib.request
    import config
    monkeypatch.setattr(config, "VALHALLA_URL", "http://valhalla:8002")
    sent = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout):
        sent.update(json.loads(req.data))
        return Resp(b'{"trip": {"legs": []}}')
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    wr._route(LAT, LON, 54.0, 10.5)
    assert sent.get("narrative", True) is True and sent["elevation_interval"] == wr.ELEV_INTERVAL_M


def test_looking_at_the_dashboard_never_renders(app_module, db, monkeypatch):
    """A changed area is rendered at most every 10 min — opening the page or
    the stored-area request changes nothing about that (1.2.318)."""
    import range_area as ra
    ra._fail_at.clear(); ra._took.clear()
    monkeypatch.setattr(ra, "_down_until", 0.0)
    monkeypatch.setattr(ra, "_fetch", lambda lat, lon, kms: {
        "type": "FeatureCollection", "features": [_circle(lat, lon, k) for k in kms]})
    assert not hasattr(ra, "touch") and not hasattr(ra, "wake")
    assert ra.render_if_due(db, "devW", LAT, LON, [180], now=1000) == "rendered"
    ra._took.clear()                                       # (the test render took no time)
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'devW')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) VALUES (1, 'a', 'x', 1, 1)")
    db.commit()
    import config
    monkeypatch.setattr(config, "VALHALLA_URL", "http://valhalla:8002")
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        assert c.get("/api/range-area/last").get_json()["status"] == "ok"
    # moved on, 4 and 9 min later: wait; after 10 min: render
    assert ra.render_if_due(db, "devW", LAT + 0.3, LON, [180], now=1240) == "wait"
    assert ra.render_if_due(db, "devW", LAT + 0.3, LON, [180], now=1540) == "wait"
    assert ra.render_if_due(db, "devW", LAT + 0.3, LON, [180], now=1601) == "rendered"
    # a slow server: never closer together than four renders long
    ra._took["devW"] = 200
    assert ra.render_every("devW") == 800
    ra._took.clear()


def test_background_pass_wires_estimate_field_and_render(app_module, db, monkeypatch):
    """The per-vehicle step of the background loop: routes only through
    field_fn (when a render is due), wind off → no dirs in the key."""
    import config
    import range_area as ra
    monkeypatch.setattr(config, "WIND_SOURCE", "")
    monkeypatch.setattr(config, "VALHALLA_URL", "http://valhalla:8002")
    cruise = {"left": 40.0, "model": MODEL, "temp": 15, "v_eval": 110, "mode": "model", "km": 190}

    def inputs(d, v):
        inputs.cruise = cruise
        return (LAT, LON, [190])
    monkeypatch.setattr(app_module, "_range_inputs", inputs)
    monkeypatch.setattr(wr, "_route", _road(lambda th: 130.0 if th < 180 else 80.0))
    ra._fail_at.clear(); ra._took.clear()
    monkeypatch.setattr(ra, "_down_until", 0.0)
    monkeypatch.setattr(ra, "_fetch", lambda lat, lon, kms: {
        "type": "FeatureCollection", "features": [_circle(lat, lon, k) for k in kms]})
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'devB')")
    db.commit()
    v = db.execute("SELECT * FROM vehicles WHERE id = 1").fetchone()
    assert app_module._range_area_vehicle(db, v) == "rendered"
    rec = ra.load_last(db, "devB")
    assert rec["kms"] == [190] and len(rec["dirs"]) == N
    east, west, _, _ = _extent(rec["geojson"], LAT, LON)
    assert west > east + 15                                # slow roads to the west reach further
    assert app_module._range_area_vehicle(db, v) == "unchanged"


def test_point_check_answers_for_one_place(app_module, db, monkeypatch):
    """Long press on the map (1.2.319): one route to that place, energy left
    on arrival — reached, short by how much, or only by a long ferry."""
    import config
    monkeypatch.setattr(config, "VALHALLA_URL", "http://valhalla:8002")
    monkeypatch.setattr(config, "WIND_SOURCE", "")
    cruise = {"left": 40.0, "model": MODEL, "temp": 15, "v_eval": 110, "mode": "model", "km": 190}

    def inputs(d, v):
        inputs.cruise = cruise
        return (LAT, LON, [190])
    monkeypatch.setattr(app_module, "_range_inputs", inputs)
    monkeypatch.setattr(wr, "_route", _road())
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'devC')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) VALUES (1, 'a', 'x', 1, 1)")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    reach = _range(40.0, 130.0)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        near = rf._target(LAT, LON, 90.0, reach - 40)
        d = c.get(f"/api/range-check?lat={near[0]}&lon={near[1]}").get_json()
        assert d["status"] == "ok" and d["reached"] and abs(d["road_km"] - (reach - 40)) < 2
        assert d["left_kwh"] > 5 and 15 < d["left_share"] < 40 and d["empty_km"] is None and d["off_km"] < 1
        far = rf._target(LAT, LON, 90.0, reach + 30)
        d = c.get(f"/api/range-check?lat={far[0]}&lon={far[1]}").get_json()
        assert d["status"] == "ok" and not d["reached"] and d["left_kwh"] < -3
        assert abs(d["empty_km"] - reach) < 6                  # where the battery is empty on that route
        assert "lat" not in d and "lon" not in d               # no position in the answer
        # a long ferry on the way: not reached by rule, distance up to the terminal
        def ferry(lat0, lon0, lat1, lon1, use_ferry=None):
            n = 100
            pts = [(lat0 + (lat1 - lat0) * i / n, lon0 + (lon1 - lon0) * i / n) for i in range(n + 1)]
            return {"trip": {"legs": [{"shape": _enc(pts), "maneuvers": [
                {"length": 20, "time": 20 / 100 * 3600, "begin_shape_index": 0, "end_shape_index": 20},
                {"type": 28, "length": 80, "time": 5 * 3600, "begin_shape_index": 20, "end_shape_index": n,
                 "street_names": ["Hier - Dort"]}]}]}}
        monkeypatch.setattr(wr, "_route", ferry)
        port = rf._target(LAT, LON, 0.0, 100.0)
        d = c.get(f"/api/range-check?lat={port[0]}&lon={port[1]}").get_json()
        assert d["long_ferry"] and not d["reached"] and d["ferry_name"] == "Hier - Dort" and 18 < d["road_km"] < 22
        # no route, bad input, no model
        monkeypatch.setattr(wr, "_route", lambda *a, **k: {"trip": {"legs": []}})
        assert c.get(f"/api/range-check?lat={port[0]}&lon={port[1]}").get_json() == {"status": "noroute"}
        assert c.get("/api/range-check?lat=x&lon=1").status_code == 400
        cruise["model"] = None
        assert c.get(f"/api/range-check?lat={port[0]}&lon={port[1]}").get_json()["error"] == "no_model"
