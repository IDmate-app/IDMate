"""rain_radar.py: DWD + RainViewer folded into one raster of grades (L-004, L-005, E-002)."""
import time

import pytest
from PIL import Image

import bms_learn as bl
import config
import rain_radar as rr

DWD_LIGHT = (0x33, 0xFF, 0xFF, 255)      # 0.1–0.2 mm/h
DWD_MOD = (0x4D, 0xB3, 0x1B, 255)        # 1–2 mm/h
DWD_HEAVY = (0xFF, 0x45, 0x01, 255)      # 15–30 mm/h
DWD_NODATA = (126, 126, 126, 77)


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(config, "RAIN_SOURCE", "dwd,rainviewer")
    rr._cache.clear()
    rr._dwd_pics.clear()
    rr._down_until.update({"dwd": 0.0, "rainviewer": 0.0})
    yield


def test_levels():
    assert rr.level_mmh(0.0) == 0 and rr.level_mmh(0.1) == 1 and rr.level_mmh(1.0) == 2 and rr.level_mmh(5.0) == 3
    assert rr.dwd_pixel((255, 255, 255, 0)) == 0.0
    assert rr.dwd_pixel(DWD_NODATA) is None
    assert rr.level_mmh(rr.dwd_pixel(DWD_LIGHT)) == 1
    assert rr.level_mmh(rr.dwd_pixel(DWD_MOD)) == 2
    assert rr.level_mmh(rr.dwd_pixel(DWD_HEAVY)) == 3
    # RainViewer Universal Blue: 5 dBZ beige (dry), 20 dBZ light, 30 dBZ moderate, 40 dBZ heavy
    assert rr.level_mmh(rr.rv_pixel((0x92, 0x88, 0x71, 0x64))) == 0
    assert rr.level_mmh(rr.rv_pixel((0x00, 0xa3, 0xe0, 0xff))) == 1
    assert rr.level_mmh(rr.rv_pixel((0x00, 0x55, 0x88, 0xff))) == 2
    assert rr.level_mmh(rr.rv_pixel((0xff, 0xaa, 0x00, 0xff))) == 3
    assert rr.rv_pixel((0, 0, 0, 0)) == 0.0


def test_raster_fixed_on_globe():
    a = {(i, j) for i, j, *_ in rr.cells(53.55, 10.0, 60)}
    b = {(i, j) for i, j, *_ in rr.cells(53.56, 10.02, 60)}
    assert len(a & b) > 0.9 * len(a)                 # same cells, not shifted with the car
    i, j = rr.cell_of(53.55, 10.0)
    la, lo = rr.cell_centre(i, j)
    assert abs(la - 53.55) < rr.LAT_STEP and abs(lo - 10.0) < 0.1


def _fake(colour_fn):
    def dwd_map(bbox, w, h, when=None):
        img = Image.new("RGBA", (w, h))
        px = img.load()
        for x in range(w):
            for y in range(h):
                px[x, y] = colour_fn(x / w, y / h, when)
        return img
    return dwd_map


def test_compute_dwd_and_rainviewer_into_one_raster():
    now = 1_794_960_000
    # DWD: left half of the area covered (moderate rain), right half outside the composite
    mask = {"img": None}

    def covered(m, lat, lon):
        return lon < 10.0
    orig = rr._covered
    rr._covered = covered
    try:
        dwd = _fake(lambda fx, fy, when: DWD_MOD)
        rv_calls = []

        def rv_tile(prefix, x, y, _now):
            rv_calls.append((x, y))
            return Image.new("RGBA", (256, 256), (0xff, 0xaa, 0x00, 0xff))       # heavy everywhere
        res = rr.compute(53.55, 10.0, 40, now=now, dwd_map=dwd,
                         rv_frames=lambda n: [(now - 600 - k * 600, "https://rv/x") for k in range(13)],
                         rv_tile=rv_tile, mask=mask)
    finally:
        rr._covered = orig
    pub = rr.public(res)
    assert pub["cell_km"] == rr.CELL_KM and pub["levels"]["3"]
    kinds = [f["kind"] for f in pub["frames"]]
    assert kinds.count("now") == 1 and "past" in kinds and "forecast" in kinds
    now_f = next(f for f in pub["frames"] if f["kind"] == "now")
    assert set(now_f["src"]) == {"dwd", "rainviewer"}
    west = [c for c in now_f["cells"] if c[1] < 9.95]
    east = [c for c in now_f["cells"] if c[1] > 10.05]
    assert west and all(c[2] == 2 for c in west)           # DWD
    assert east and all(c[2] == 3 for c in east)           # RainViewer
    fc = [f for f in pub["frames"] if f["kind"] == "forecast"]
    assert all(f["src"] == ["dwd"] for f in fc)            # no RainViewer nowcast
    assert all(c[1] < 10.0 for f in fc for c in f["cells"])
    assert "_known" not in now_f and all(len(c) == 3 for c in now_f["cells"])


def test_level_at_and_range_term(monkeypatch):
    now = time.time()
    res = rr.compute(53.55, 10.0, 30, now=now, dwd_map=_fake(lambda fx, fy, w: DWD_HEAVY),
                     rv_frames=lambda n: [], rv_tile=None, mask={"img": None})
    monkeypatch.setattr(rr, "_covered", lambda m, la, lo: True)
    res = rr.compute(53.55, 10.0, 30, now=now, dwd_map=_fake(lambda fx, fy, w: DWD_HEAVY),
                     rv_frames=lambda n: [], rv_tile=None, mask={"img": None})
    monkeypatch.setattr(rr, "_last", res)
    assert rr.level_at(53.55, 10.0, now) == 3
    assert rr.level_at(53.55, 10.0, now + 6 * 3600) is None           # beyond the forecast
    assert rr.level_at(60.0, 25.0, now) is None                         # outside the area
    # the range: no effect until the model has learned rain
    model = {"a": 12.0, "b": 8.0}
    assert bl.rain_extra(model, 3) == 0.0
    assert bl.rain_extra({**model, "rain": {3: 2.5}}, 3) == 2.5
    assert bl.rain_extra({**model, "rain": {"2": 1.0}}, 2) == 1.0
    assert bl.rain_extra({**model, "rain": {3: 2.5}}, None) == 0.0


def test_off_without_source(monkeypatch):
    monkeypatch.setattr(config, "RAIN_SOURCE", "")
    assert not rr.enabled()
    assert rr.area(53.5, 10.0, 100) is None
    assert rr.public(None)["frames"] == []


def test_tiles_bounded():
    x0, y0, x1, y1 = rr.tiles_for(53.5, 10.0, 1500)
    assert (x1 - x0 + 1) * (y1 - y0 + 1) <= rr.MAX_TILES


def test_model_learns_rain_from_trips():
    """bms_learn.rain_effect: trips in rain above the model, less the dry trips' bias."""
    import random
    random.seed(1)
    model = {"a": 10.0, "b": 8.0, "c": 0.0, "p": 0.0, "e_start": 0.0}
    trips = []
    for k in range(60):
        lv = k % 4
        v = 60 + (k * 7) % 60
        cons = bl.consumption(model, v, 15) + 0.5 + {0: 0.0, 1: 1.0, 2: 2.0, 3: 3.5}[lv] + random.uniform(-0.2, 0.2)
        trips.append({"km": 40, "v": v, "temp": 15, "cons": cons, "stood_h": 0.1, "rain": lv})
    rain, n = bl.rain_effect(trips, model)
    assert n == {0: 15, 1: 15, 2: 15, 3: 15}
    assert abs(rain[1] - 1.0) < 0.3 and abs(rain[2] - 2.0) < 0.3 and abs(rain[3] - 3.5) < 0.3
    few = [t for t in trips if t["rain"] in (0, 3)][:20]
    rain, _n = bl.rain_effect(few, model)
    assert 1 not in rain and 2 not in rain
    assert bl.rain_effect([t for t in trips if t["rain"]], model)[0] == {}     # no dry trips: no baseline


def test_measured_pictures_are_fetched_once(monkeypatch):
    """One whole-composite picture per frame; next quarter hour only the new
    measured picture and the forecasts again; measured_only asks no forecast."""
    monkeypatch.setattr(rr, "_covered", lambda m, la, lo: True)
    rr._dwd_pics.clear()
    calls = []

    def dwd(bbox, w, h, when=None):
        calls.append((bbox, when))
        return Image.new("RGBA", (w, h), (255, 255, 255, 0))
    now = 1_794_960_000
    kw = dict(dwd_map=dwd, rv_frames=lambda n: [], rv_tile=None, mask={"img": None})
    res = rr.compute(53.55, 10.0, 30, now=now, measured_only=True, **kw)
    assert len(calls) == rr.PAST_MIN // rr.FRAME_MIN + 1 and res["_complete"] is False
    assert all(f["kind"] != "forecast" for f in res["frames"])
    assert len({c[0] for c in calls}) == 1                       # always the same whole-composite box
    calls.clear()
    res = rr.compute(53.55, 10.0, 30, now=now, **kw)
    assert len(calls) == rr.AHEAD_MIN // rr.FRAME_MIN and res["_complete"] is True   # only the forecasts
    calls.clear()
    rr.compute(48.1, 11.6, 30, now=now, **kw)                    # elsewhere in Germany: nothing new
    assert calls == []
    rr.compute(53.55, 10.0, 30, now=now + 15 * 60, **kw)
    assert len(calls) == 1 + rr.AHEAD_MIN // rr.FRAME_MIN       # the new "now" plus the forecasts


def test_area_asks_once_per_quarter_and_tile_set(monkeypatch):
    n = []
    monkeypatch.setattr(rr, "compute", lambda la, lo, r, now=None, measured_only=False: n.append(1) or
                        {"_center": (la, lo), "frames": [], "radius": r, "cell_km": 5, "levels": {},
                         "_complete": not measured_only})
    rr._cache.clear()
    now = 1_794_960_000
    rr.area(53.55, 10.0, 150, now=now, wait=True)
    rr.area(53.56, 10.02, 150, now=now + 60, wait=True)        # drove on a little: same tiles
    rr.area(53.55, 10.0, 150, now=now + 120, wait=True)
    assert len(n) == 1
    rr.area(53.55, 10.0, 150, now=now + 16 * 60, wait=True)    # next quarter hour
    assert len(n) == 2


def test_area_answers_at_once_then_completes(monkeypatch):
    """The map gets past and now at once (complete false), the forecast follows."""
    import threading
    done = threading.Event()
    calls = []

    def fake(la, lo, r, now=None, measured_only=False):
        calls.append(measured_only)
        if not measured_only:
            done.wait(5)
        return {"_center": (la, lo), "frames": [], "radius": r, "cell_km": 5, "levels": {},
                "_complete": not measured_only}
    monkeypatch.setattr(rr, "compute", fake)
    monkeypatch.setattr(rr, "_start_keeper", lambda: None)
    rr._cache.clear()
    rr._busy["on"] = False
    now = 1_794_970_000
    first = rr.area(53.55, 10.0, 150, now=now)
    assert first["_complete"] is False and rr.public(first)["complete"] is False
    again = rr.area(53.55, 10.0, 150, now=now + 2)                 # still working: no new requests
    assert again is first and calls.count(True) == 1
    done.set()
    for _ in range(50):
        if not rr._busy["on"]:
            break
        time.sleep(0.05)
    final = rr.area(53.55, 10.0, 150, now=now + 4)
    assert final["_complete"] is True and calls == [True, False]
