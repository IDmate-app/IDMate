"""Range area from an energy field (1.2.315): every route leaves samples
"energy left here"; the area is where the interpolated field is not
negative, inside one Valhalla contour as the mask."""

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


def test_slow_roads_reach_further_than_the_motorway():
    # east: motorway at 130, west: country roads at 80
    f = rf.rays(40.0, MODEL, 15, LAT, LON, [], 0, [190] * N, route_fn=_road(lambda th: 130.0 if th < 180 else 80.0))
    assert f["used"] == f["rays"] == 2 * N                 # the bearings in between as well
    east, west = f["dirs"][N // 4], f["dirs"][3 * N // 4]
    assert west > east * 1.1
    # samples: positive near the car, negative far out, none past the route's end
    near = [s for s in f["samples"] if s[3] < 50]
    far = [s for s in f["samples"] if s[3] > west + 30]
    assert near and all(s[2] > 0 for s in near) and far and all(s[2] < 0 for s in far)
    assert east <= f["dmax"] <= west + wr.STEP_KM
    assert rf.mask_km(f) % 5 == 0 and rf.mask_km(f) > f["dmax"]


def test_no_samples_past_the_end_of_a_route():
    # routes end after 60 km (coast): energy is left, nothing is made up beyond
    def short(lat0, lon0, lat1, lon1, use_ferry=None):
        th = math.radians(_bearing(lat0, lon0, lat1, lon1))
        la, lo = rf._target(lat0, lon0, math.degrees(th), 60.0)
        return _road()(lat0, lon0, la, lo)
    f = rf.rays(40.0, MODEL, 15, LAT, LON, [], 0, [190] * N, route_fn=short)
    assert max(s[3] for s in f["samples"]) <= 60 and all(s[2] > 0 for s in f["samples"])
    assert all(d > 100 for d in f["dirs"])                 # the range itself still carries on


def test_second_half_of_the_rays_only_when_fast():
    t = [0.0]
    def clock():
        t[0] += 2.0                                        # every look at the clock: 2 s later
        return t[0]
    slow = rf.rays(40.0, MODEL, 15, LAT, LON, [], 0, [190] * N, route_fn=_road(), budget_s=1, clock=clock)
    assert slow["rays"] == N
    fast = rf.rays(40.0, MODEL, 15, LAT, LON, [], 0, [190] * N, route_fn=_road(), budget_s=60, clock=clock)
    assert fast["rays"] == 2 * N


def test_long_ferry_counts_up_to_the_terminal():
    n = 200
    def ferry(lat0, lon0, lat1, lon1, use_ferry=None):
        pts = [(lat0 + (lat1 - lat0) * i / n, lon0 + (lon1 - lon0) * i / n) for i in range(n + 1)]
        x, y = wr._xy(lat0, lon0, lat1, lon1)
        km = math.hypot(x, y)
        return {"trip": {"legs": [{"shape": _enc(pts), "maneuvers": [
            {"length": km * 0.1, "time": km * 0.1 / 100 * 3600, "begin_shape_index": 0, "end_shape_index": 20},
            {"type": 28, "length": km * 0.6, "time": 9 * 3600, "begin_shape_index": 20, "end_shape_index": 140},
            {"length": km * 0.3, "time": km * 0.3 / 100 * 3600, "begin_shape_index": 140, "end_shape_index": n}]}]}}
    f = rf.rays(40.0, MODEL, 15, LAT, LON, [], 0, [190] * N, route_fn=ferry)
    assert f["dirs"] == [190] * N                          # no range from these routes: the estimate stays
    assert f["samples"] and max(s[3] for s in f["samples"]) < 60     # only the road to the terminal


def test_too_few_routes_is_no_field():
    def none(*a, **k):
        raise OSError("no route")
    assert rf.rays(40.0, MODEL, 15, LAT, LON, [], 0, [190] * N, route_fn=none) is None


def _samples(reach):
    """Synthetic field: reach(th) km as the crow flies, 1 kWh per 10 km."""
    out = []
    for i in range(72):
        th = i * 5.0
        for r in range(5, 260, 10):
            la, lo = rf._target(LAT, LON, th, r)
            out.append((la, lo, (reach(th) - r) / 10.0, r * 1.25))
    return out


def _extent(gj, lat0, lon0):
    line = [f for f in gj["features"] if f["properties"]["kind"] == "line"][0]
    pts = [wr._xy(lat0, lon0, la, lo) for ring in line["geometry"]["coordinates"] for lo, la in ring]
    return (max(p[0] for p in pts), -min(p[0] for p in pts), max(p[1] for p in pts), -min(p[1] for p in pts))


def test_compose_cuts_the_mask_where_the_energy_runs_out():
    mask = {"features": [_circle(LAT, LON, 240)]}
    gj = rf.compose(mask, _samples(lambda th: 200.0 if th < 180 else 120.0), LAT, LON)
    east, west, north, south = _extent(gj, LAT, LON)
    assert 185 < east < 215 and 105 < west < 135           # each side its own reach, no wedges, no steps
    line = [f for f in gj["features"] if f["properties"]["kind"] == "line"][0]
    assert len(line["geometry"]["coordinates"]) == 1       # one clean outline
    # the mask wins where the land ends before the energy does
    small = rf.compose({"features": [_circle(LAT, LON, 90)]}, _samples(lambda th: 200.0), LAT, LON)
    e, w, n, s = _extent(small, LAT, LON)
    assert max(e, w, n, s) < 95
    assert rf.compose(mask, [], LAT, LON) is None and rf.compose({"features": []}, _samples(lambda th: 1.0), LAT, LON) is None


def test_render_uses_the_field(app_module, db, monkeypatch):
    import range_area as ra
    ra._fail_at.clear()
    monkeypatch.setattr(ra, "_down_until", 0.0)
    asked = []
    def fetch(lat, lon, kms):
        asked.append(list(kms))
        return {"type": "FeatureCollection", "features": [_circle(lat, lon, k) for k in kms]}
    monkeypatch.setattr(ra, "_fetch", fetch)
    field = {"dirs": [150] * (N // 2) + [230] * (N // 2), "samples": _samples(lambda th: 180.0 if th < 180 else 120.0),
             "dmax": 228.0, "used": 64, "rays": 64, "seconds": 1.0}
    calls = []
    def field_fn():
        calls.append(1)
        return field
    assert ra.render_if_due(db, "devF", LAT, LON, [180], now=1000, field_fn=field_fn) == "rendered"
    rec = ra.load_last(db, "devF")
    assert asked == [[240]]                                # one contour: the mask, a little beyond dmax
    assert rec["dirs"] == field["dirs"] and rec["kms"] == [180]
    east, west, _, _ = _extent(rec["geojson"], LAT, LON)
    assert 165 < east < 195 and 105 < west < 135
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


def test_open_dashboard_renders_sooner(app_module, db, monkeypatch):
    """A changed area waits 10 min for its next render — 3 min while a
    dashboard is open, and the first look wakes the background loop."""
    import range_area as ra
    ra._fail_at.clear(); ra._watch.clear(); ra._took.clear(); ra.wake.clear()
    monkeypatch.setattr(ra, "_down_until", 0.0)
    monkeypatch.setattr(ra, "_fetch", lambda lat, lon, kms: {
        "type": "FeatureCollection", "features": [_circle(lat, lon, k) for k in kms]})
    assert ra.render_if_due(db, "devW", LAT, LON, [180], now=1000) == "rendered"
    ra._took.clear()                                       # (the test render took no time)
    # moved on 4 min later: nobody looks → wait
    assert ra.render_if_due(db, "devW", LAT + 0.3, LON, [180], now=1240) == "wait"
    ra.touch("devW", now=1240)
    assert ra.wake.is_set()                                # first look: the loop runs at once
    assert ra.render_if_due(db, "devW", LAT + 0.3, LON, [180], now=1241) == "rendered"
    # still watched, but the last render is only a minute old
    ra._took.clear()
    ra.touch("devW", now=1300)
    assert ra.render_if_due(db, "devW", LAT + 0.6, LON, [180], now=1301) == "wait"
    # a slow server: never closer together than four renders long
    ra._took["devW"] = 120
    assert ra.render_every("devW", 1301) == 480
    ra._watch.clear(); ra._took.clear(); ra.wake.clear()


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
    ra._fail_at.clear(); ra._watch.clear(); ra._took.clear()
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
