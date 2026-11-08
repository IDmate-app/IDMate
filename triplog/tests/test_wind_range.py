"""Range per direction with wind (1.2.286) and the area composed from
several road contours, one per direction wedge."""

import math

import bms_learn as bl
import wind_range as wr

MODEL = {"a": 16.0, "b": 5.0, "c": 0.3, "temp": True, "n": 100, "km": 5000}


def _point(lat, lon, kmh, frm, temp=10, elev=None):
    return {"lat": lat, "lon": lon, "data": {"t": [0], "kmh": [kmh], "dir": [frm], "gust": [kmh], "temp": [temp],
                                             "elev": elev}}


def test_calm_is_the_same_everywhere_and_matches_the_model():
    d = wr.direction_ranges(50.0, MODEL, 10, 110, 53.8, 10.4, [], now=100)
    assert len(set(d)) == 1
    plain = bl.cruise_range(50.0, MODEL, 10, v=110)["km"]
    assert abs(d[0] - plain) <= 2


def test_north_wind_costs_range_to_the_north():
    pts = [_point(53.8 + i * 0.45, 10.4 + j * 0.75, 40, 0) for i in range(-4, 5) for j in range(-4, 5)]
    d = wr.direction_ranges(50.0, MODEL, 10, 110, 53.8, 10.4, pts, now=100)
    north, south, east = d[0], d[8], d[4]
    assert north < east < south
    assert south - north > 20


def test_levels():
    assert wr.levels([200] * 16) == [200]
    lv = wr.levels([150, 160, 200, 230])
    assert len(lv) == 4 and lv[0] == 150 and lv[-1] == 230


def _circle(lat0, lon0, r_km, n=180):
    ring = []
    for i in range(n + 1):
        a = 2 * math.pi * i / n
        la, lo = wr._ll(lat0, lon0, math.sin(a) * r_km, math.cos(a) * r_km)
        ring.append([lo, la])
    return {"type": "Feature", "properties": {"contour": float(r_km)},
            "geometry": {"type": "Polygon", "coordinates": [ring]}}


def test_compose_picks_contour_per_wedge_one_clean_outline():
    iso = {"type": "FeatureCollection", "features": [_circle(53.8, 10.4, r) for r in (100, 150, 200)]}
    dirs = [100] * 8 + [200] * 8            # east half short, west half long
    gj = wr.compose(iso, dirs, 53.8, 10.4)
    fill = gj["features"][0]["geometry"]["coordinates"]
    lines = gj["features"][1]["geometry"]["coordinates"]
    assert len(fill) == 1 and len(lines) == 1          # one area, one closed outline
    assert lines[0][0] == lines[0][-1]
    # outline points lie on the circles, except the two radial steps where
    # the wedges change contour (bearings 168.75° and 348.75°)
    for lo, la in lines[0]:
        x, y = wr._xy(53.8, 10.4, la, lo)
        r = math.hypot(x, y)
        ang = (math.degrees(math.atan2(x, y)) + 360) % 360
        near_step = min(abs((ang - b + 180) % 360 - 180) for b in (168.75, 348.75)) < 3
        assert near_step or min(abs(r - 100), abs(r - 200)) < 2.5

    def reach(deg):
        best = 0
        for poly in fill:
            for lo, la in poly[0]:
                x, y = wr._xy(53.8, 10.4, la, lo)
                ang = (math.degrees(math.atan2(x, y)) + 360) % 360
                if abs((ang - deg + 180) % 360 - 180) < 5:
                    best = max(best, math.hypot(x, y))
        return best
    assert abs(reach(45) - 100) < 3 and abs(reach(225) - 200) < 3


def test_compose_drops_holes_and_slivers():
    big = _circle(53.8, 10.4, 150)
    hole = [list(reversed(_circle(53.8, 10.4, 40)["geometry"]["coordinates"][0]))]
    big["geometry"]["coordinates"] += hole                      # a lake inside
    gj = wr.compose({"features": [big]}, None, 53.8, 10.4)
    assert len(gj["features"][1]["geometry"]["coordinates"]) == 1   # no ring for the hole


def test_render_with_dirs_composes(app_module, db, monkeypatch):
    import range_area as ra
    ra._fail_at.clear()
    monkeypatch.setattr(ra, "_down_until", 0.0)
    asked = []
    def fake(lat, lon, kms):
        asked.append(sorted(kms))
        return {"type": "FeatureCollection", "features": [_circle(lat, lon, k) for k in kms]}
    monkeypatch.setattr(ra, "_fetch", fake)
    dirs = [150] * 8 + [210] * 8
    assert ra.render_if_due(db, "dev1", 53.8, 10.4, [180], dirs=dirs, now=1000) == "rendered"
    rec = ra.load_last(db, "dev1")
    assert rec["dirs"] == dirs and asked[0] == wr.levels(dirs)
    assert rec["geojson"]["features"][0]["properties"]["kind"] == "fill"
    # same wind picture: nothing to do
    assert ra.render_if_due(db, "dev1", 53.8, 10.4, [180], dirs=dirs, now=5000) == "unchanged"
    # calm (all equal): back to a single contour without composing
    assert ra.render_if_due(db, "dev1", 53.8, 10.4, [180], dirs=[180] * 16, now=1000 + 700) == "rendered"
    assert "dirs" not in ra.load_last(db, "dev1")


def test_mountains_to_the_south_cost_range_there():
    # calm; terrain rises 200 m per raster row to the south (Alps-like), flat north
    pts = [_point(53.8 + i * 0.45, 10.4 + j * 0.75, 0, 0, elev=max(0, -i) * 200)
           for i in range(-6, 7) for j in range(-4, 5)]
    d = wr.direction_ranges(50.0, MODEL, 10, 110, 53.8, 10.4, pts, now=100)
    north, south = d[0], d[8]
    assert south < north - 15
    # 1200 m climb ≈ 2200·9.81·1200/3.6e6/0.9 ≈ 8 kWh → clearly shorter
    flat = wr.direction_ranges(50.0, MODEL, 10, 110, 53.8, 10.4,
                               [dict(p, data=dict(p["data"], elev=0)) for p in pts], now=100)
    assert flat[0] == north and flat[8] > south


def test_downhill_wins_back_a_part():
    pts = [_point(53.8 + i * 0.45, 10.4 + j * 0.75, 0, 0, elev=1000 if i == 0 and j == 0 else 0)
           for i in range(-6, 7) for j in range(-4, 5)]
    d = wr.direction_ranges(50.0, MODEL, 10, 110, 53.8, 10.4, pts, now=100)
    flat = wr.direction_ranges(50.0, MODEL, 10, 110, 53.8, 10.4,
                               [dict(p, data=dict(p["data"], elev=0)) for p in pts], now=100)
    assert all(a >= b for a, b in zip(d, flat))     # starting high: downhill helps


# ── along routes (1.2.289): speed limits and elevation from Valhalla ────────

def _enc(pts):
    def one(v):
        v = ~(v << 1) if v < 0 else v << 1
        out = ""
        while v >= 0x20:
            out += chr((0x20 | (v & 0x1F)) + 63)
            v >>= 5
        return out + chr(v + 63)
    s, plat, plon = "", 0, 0
    for la, lo in pts:
        ila, ilo = round(la * 1e6), round(lo * 1e6)
        s += one(ila - plat) + one(ilo - plon)
        plat, plon = ila, ilo
    return s


def _resp(lat0, lon0, km, speed, elev=None, n=200):
    """Straight route north, km long, one maneuver at speed km/h."""
    pts = [wr._ll(lat0, lon0, 0.0, km * i / n) for i in range(n + 1)]
    leg = {"shape": _enc(pts), "maneuvers": [{"length": km, "time": km / speed * 3600,
                                              "begin_shape_index": 0, "end_shape_index": n}]}
    if elev is not None:
        leg["elevation"] = elev
    return {"trip": {"legs": [leg]}}


def test_polyline6_roundtrip():
    pts = [(53.8, 10.4), (53.81234, 10.39876), (48.2, 16.37)]
    out = wr.decode_polyline6(_enc(pts))
    assert all(abs(a - c) < 1e-6 and abs(b - d) < 1e-6 for (a, b), (c, d) in zip(pts, out))


def test_route_speed_limits_change_range():
    fast = wr.route_profile(_resp(53.8, 10.4, 400, 130))
    slow = wr.route_profile(_resp(53.8, 10.4, 400, 90))
    r_fast = wr.range_along_route(50.0, MODEL, 10, fast, [], now=0)
    r_slow = wr.range_along_route(50.0, MODEL, 10, slow, [], now=0)
    assert r_slow > r_fast + 15
    # motorway at top speed 130 = the same as the flat 130 estimate (avg 110)
    assert abs(r_fast - bl.cruise_range(50.0, MODEL, 10, v=110)["km"]) <= 3


def test_route_elevation_profile_costs_range():
    n = int(400 * 1000 / wr.ELEV_INTERVAL_M) + 1
    climb = [min(1500, i * 10) for i in range(n)]          # +10 m per 500 m up to 1500 m
    flat = wr.route_profile(_resp(53.8, 10.4, 400, 130, elev=[0] * n))
    hill = wr.route_profile(_resp(53.8, 10.4, 400, 130, elev=climb))
    assert hill["elev"] and wr.range_along_route(50.0, MODEL, 10, hill, [], 0) < \
        wr.range_along_route(50.0, MODEL, 10, flat, [], 0) - 20


def test_short_route_continues_straight():
    prof = wr.route_profile(_resp(53.8, 10.4, 60, 130))
    r = wr.range_along_route(50.0, MODEL, 10, prof, [], now=0)
    assert r > 150                                          # energy for far more than 60 km


def test_directions_fall_back_without_route():
    guess = [200] * 16
    def fn(la0, lo0, la1, lo1):
        if la1 > la0 + 0.5:                                # only the northern half has roads
            return _resp(la0, lo0, 400, 90)
        raise OSError("no route")
    out, used = wr.direction_ranges_routes(50.0, MODEL, 10, 53.8, 10.4, [], 0, guess, route_fn=fn)
    assert 0 < used < 16
    assert out[0] > 200 and out[8] == 200


def test_refine_runs_only_when_due(app_module, db, monkeypatch):
    import range_area as ra
    ra._fail_at.clear()
    monkeypatch.setattr(ra, "_down_until", 0.0)
    monkeypatch.setattr(ra, "_fetch", lambda lat, lon, kms: {
        "type": "FeatureCollection", "features": [_circle(lat, lon, k) for k in kms]})
    calls = []
    def refine():
        calls.append(1)
        return [160] * 8 + [240] * 8
    dirs = [150] * 8 + [210] * 8
    assert ra.render_if_due(db, "dev2", 53.8, 10.4, [180], dirs=dirs, now=1000, refine=refine) == "rendered"
    assert ra.load_last(db, "dev2")["dirs"] == [160] * 8 + [240] * 8
    assert ra.render_if_due(db, "dev2", 53.8, 10.4, [180], dirs=dirs, now=2000, refine=refine) == "unchanged"
    assert len(calls) == 1
