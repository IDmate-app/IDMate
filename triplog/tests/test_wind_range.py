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


def test_compose_picks_contour_per_wedge_and_drops_cut_edges():
    iso = {"type": "FeatureCollection", "features": [_circle(53.8, 10.4, r) for r in (100, 150, 200)]}
    dirs = [100] * 8 + [200] * 8            # east half short, west half long
    gj = wr.compose(iso, dirs, 53.8, 10.4)
    fill = gj["features"][0]["geometry"]["coordinates"]
    lines = gj["features"][1]["geometry"]["coordinates"]
    assert len(fill) == 16
    # every outline point lies on one of the circles (no radial cut edges)
    for chain in lines:
        for lo, la in chain:
            x, y = wr._xy(53.8, 10.4, la, lo)
            r = math.hypot(x, y)
            assert min(abs(r - 100), abs(r - 200)) < 2
    # bearing 45° (north-east) reaches ~100 km, 225° (south-west) ~200 km
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
