"""Vehicle model: capacity per battery, swap detection, efficiency per
vehicle calibrated against the BMS."""

import time
from datetime import datetime

import pytest

import fahrzeugmodell as fm


def _pts(spec):
    """[(date, cap), ...] → points"""
    return [{"ts": datetime.fromisoformat(d).timestamp(), "cap": c} for d, c in spec]


OLD = [(f"2025-{m:02d}-{d:02d}", 44.0 + (d % 3) * 0.4 - 1) for m in range(1, 8) for d in (5, 15, 25)]
NEW = [("2026-08-29", 48.1), ("2026-09-04", 46.3), ("2026-09-10", 50.3), ("2026-10-04", 52.8),
       ("2026-10-06", 53.7), ("2026-10-08", 48.1), ("2026-10-11", 51.0), ("2026-10-13", 52.8),
       ("2026-10-20", 41.5)]


def test_detects_swap_at_first_charge_after_it():
    ch = fm.detect_battery_change(_pts(OLD + NEW))
    assert ch["date"] == "2026-08-29" and ch["after"] > ch["before"] + 4
    assert fm.detect_battery_change(_pts(OLD)) is None


def test_segments_fit_separately():
    segs = [{"id": None, "from": None, "nominal": 54.0, "note": None},
            {"id": 1, "from": "2026-08-01", "nominal": 54.0, "note": None}]
    m = fm.capacity_model(_pts(OLD + NEW), segs, time.time())
    old, new = m["segments"]
    assert old["kind"] == "trend" and 42 < old["cap_now"] < 45
    assert new["kind"] == "level" and new["n"] == 9 and 49 < new["cap_now"] < 52
    assert m["cap_at"](datetime(2025, 3, 1).timestamp()) < 45
    assert m["cap_at"](datetime(2026, 9, 1).timestamp()) == new["cap_now"]


def test_new_battery_without_points_uses_its_nominal():
    segs = [{"id": None, "from": None, "nominal": 54.0, "note": None},
            {"id": 1, "from": "2026-10-20", "nominal": 57.5, "note": None}]
    m = fm.capacity_model(_pts(OLD), segs, time.time())
    assert m["current"]["kind"] == "nominal" and m["current"]["cap_now"] == 57.5


def _counter_trips(days=6):
    """Trips around home charges: counter +kwh while parked, SoC +gain."""
    trips, kw, soc = [], 1000.0, 30.0
    for d in range(1, days + 1):
        trips.append({"start_time": f"2026-05-{d:02d}T08:00:00", "end_time": f"2026-05-{d:02d}T09:00:00",
                      "kw_start": kw, "kw_end": kw + 2.0, "soc_start": soc + 50, "soc_end": soc})
        kw += 2.0 + 42.5                      # parked: 42.5 kWh into the battery = 50 % of 85
    return trips


def test_counter_capacity_and_efficiency():
    trips = _counter_trips()
    segs = [{"id": None, "from": None, "nominal": 86.0, "note": None}]
    pts = fm.counter_capacity_points(trips, segs)
    assert len(pts) == 5 and all(round(p["cap"], 1) == 85.0 for p in pts)
    sessions = [{"start_time": f"2026-05-{d:02d}T20:00:00", "total_kwh": 42.5 / 0.935, "is_external": 0,
                 "avg_kw": 11.0} for d in range(1, 4)]
    sessions += [{"start_time": f"2026-05-{d:02d}T20:00:00", "total_kwh": 42.6, "is_external": 1,
                  "avg_kw": 120.0} for d in range(4, 6)]
    res = fm.counter_efficiency(trips, sessions)
    assert res["ac"]["eff"] == 0.935 and res["ac"]["n"] == 3
    assert res["dc"] is None                     # 2 receipts are too few


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin) VALUES (1, 'admin', 'x', 1)")
    db.execute("INSERT INTO vehicles (id, plate, device, battery_capacity_kwh) VALUES (1, 'T1', 'dev1', 54)")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def test_swap_api_splits_capacity_and_recomputes_trips(app_module, db, client):
    for d, c in OLD + NEW:   # TM sessions: battery side, cap = kwh / 0.5
        db.execute("INSERT INTO charge_sessions (vehicle_plate, start_time, end_time, total_kwh, soc_start, "
                   "soc_end, is_external, session_number) VALUES ('T1', ?, ?, ?, 30, 80, 1, 'TM-1')",
                   (d + "T10:00:00", d + "T12:00:00", c * 0.5))
    db.execute("INSERT INTO trips (device, start_time, end_time, distance_km, soc_start, soc_end, is_manual) "
               "VALUES ('dev1', '2026-10-15T08:00:00', '2026-10-15T09:00:00', 50, 60, 40, 0)")
    db.commit()
    app_module._trend_cache.clear()
    m = client.get("/api/vehicles/T1/model").get_json()
    assert len(m["segments"]) == 1 and m["battery_change"]["date"] == "2026-08-29"
    r = client.post("/api/vehicles/T1/batteries", json={"installed_at": "2026-08-15", "nominal_kwh": 54})
    assert r.status_code == 200
    m = client.get("/api/vehicles/T1/model").get_json()
    assert [g["from"] for g in m["segments"]] == [None, "2026-08-15"] and m["battery_change"] is None
    e = db.execute("SELECT energy_kwh FROM trips").fetchone()[0]
    assert e == round(0.2 * m["segments"][1]["cap_now"], 2)          # new battery's level
    detail = client.get("/api/vehicles/T1/capacity-detail?days=36500").get_json()
    assert any(p["cap"] is None for p in detail["trend_line"])        # gap at the swap
    bid = db.execute("SELECT id FROM vehicle_batteries").fetchone()[0]
    assert client.delete(f"/api/vehicles/T1/batteries/{bid}").status_code == 200
    assert len(client.get("/api/vehicles/T1/model").get_json()["segments"]) == 1


def test_vehicle_efficiency_overrides_global(app_module, db, client):
    assert client.post("/api/vehicles/T1/efficiency", json={"eff": 0.951}).get_json()["used"] == 0.951
    assert app_module._get_charge_efficiency(db, "T1") == 0.951
    assert client.post("/api/vehicles/T1/efficiency", json={"eff": 0.5}).status_code == 400
    client.post("/api/vehicles/T1/efficiency", json={"eff": None})
    assert app_module._get_charge_efficiency(db, "T1") == app_module._get_charge_efficiency(db)


def test_kw_counter_wraps_and_dc_rule():
    assert fm.kw_delta(6524.3, 548.6) == pytest.approx(577.9, abs=0.01)
    assert fm.kw_delta(1316.8, 1317.7) == pytest.approx(0.9)
    assert fm.is_dc({"avg_kw": 95.0}) and not fm.is_dc({"avg_kw": 11.0}) and not fm.is_dc({"avg_kw": None})


def test_dc_efficiency_used_for_dc_charges(app_module, db, client):
    db.execute("UPDATE vehicles SET charge_efficiency = 0.95, charge_efficiency_dc = 0.97 WHERE plate = 'T1'")
    db.execute("INSERT INTO charge_sessions (vehicle_plate, start_time, end_time, total_kwh, soc_start, soc_end, "
               "is_external, avg_kw, cost_total) VALUES "
               "('T1', '2026-09-01T10:00:00', '2026-09-01T11:00:00', 30, 30, 85, 0, 11, NULL), "
               "('T1', '2026-09-05T10:00:00', '2026-09-05T10:30:00', 30, 30, 85, 1, 120, 15.0)")
    db.commit()
    pts, _ = app_module._capacity_points(db, "T1")
    assert [round(p["cap"], 1) for p in pts] == [51.8, 52.9]   # 30 × 0.95 / 0.55, 30 × 0.97 / 0.55


def test_efficiency_measured_automatically_and_bms_only_compared(app_module, db, client, monkeypatch):
    for t in _counter_trips(14):
        db.execute("INSERT INTO trips (device, start_time, end_time, kw_start, kw_end, soc_start, soc_end, "
                   "distance_km, is_manual) VALUES ('dev1', ?, ?, ?, ?, ?, ?, 20, 0)",
                   (t["start_time"], t["end_time"], t["kw_start"], t["kw_end"], t["soc_start"], t["soc_end"]))
    db.execute("UPDATE vehicles SET battery_capacity_kwh = 86 WHERE plate = 'T1'")
    db.commit()
    import blueprints.model as bm
    monkeypatch.setattr(bm, "_bms_daily", lambda dev, since: {"2026-05-14": 86.4})

    def sessions(n):
        for d in range(1, n + 1):
            db.execute("INSERT INTO charge_sessions (vehicle_plate, start_time, end_time, total_kwh, is_external, avg_kw) "
                       "VALUES ('T1', ?, ?, ?, 0, 11)", (f"2026-05-{d:02d}T20:00:00", f"2026-05-{d:02d}T23:00:00", 42.5 / 0.94))
        db.commit()
        app_module._trend_cache.clear(); app_module._eff_cache.clear()

    sessions(5)                                     # too few: global value, measurement shown
    e = client.get("/api/vehicles/T1/model").get_json()["efficiency"]
    assert e["ac"]["origin"] == "global" and e["ac"]["measured"]["n"] == 5 and e["dc"]["origin"] == "ac"
    db.execute("DELETE FROM charge_sessions"); sessions(13)
    m = client.get("/api/vehicles/T1/model").get_json()
    assert m["efficiency"]["ac"] == {"used": 0.94, "origin": "measured", "measured": m["efficiency"]["ac"]["measured"]}
    assert app_module._get_charge_efficiency(db, "T1") == 0.94
    assert m["source"] == "counter" and m["segments"][0]["cap_now"] == 85.0
    assert m["bms"]["latest"] == 86.4 and m["bms"]["deviation_pct"] == round((86.4 - 85) / 85 * 100, 1)
    client.post("/api/vehicles/T1/efficiency", json={"eff": 0.9, "kind": "ac"})       # fixed by hand
    e = client.get("/api/vehicles/T1/model").get_json()["efficiency"]
    assert e["ac"]["origin"] == "fixed" and e["ac"]["used"] == 0.9
    client.post("/api/vehicles/T1/efficiency", json={"eff": None, "kind": "ac"})      # back to automatic
    assert app_module._get_charge_efficiency(db, "T1") == 0.94


def test_flat_rate_one_rule():
    rate = fm.flat_rates([{"valid_from": "2025-01-01", "pauschale_kwh": 0.30},
                          {"valid_from": "2026-01-01", "pauschale_kwh": 0.32}])
    assert rate("2024-06-01T10:00:00") == 0.30          # before the first tariff: the first
    assert rate("2025-12-31") == 0.30 and rate("2026-01-01") == 0.32
    assert rate(None) == 0.32                            # no day: the newest
    assert fm.flat_rates([])("2026-01-01") == fm.DEFAULT_FLAT_RATE


def test_energy_book_sides_by_source():
    wb = fm.session_energy({"total_kwh": 10, "is_external": 0}, 0.9, 0.95)
    assert (wb["grid"], wb["batt"], wb["source"], wb["measured"]) == (10, 9.0, "wallbox", True)
    dc = fm.session_energy({"total_kwh": 40, "is_external": 1, "cost_total": 20, "avg_kw": 90}, 0.9, 0.95)
    assert dc["source"] == "receipt" and dc["batt"] == 38.0
    tm = fm.session_energy({"total_kwh": 30, "is_external": 1, "session_number": "TM-7",
                            "note": "TeslaMate import (kwh_used=33.4, kw_max=11)"}, 0.9)
    assert (tm["grid"], tm["batt"], tm["source"]) == (33.4, 30, "teslamate")
    tm2 = fm.session_energy({"total_kwh": 27, "is_external": 1, "session_number": "TM-8", "note": ""}, 0.9)
    assert tm2["grid"] == 30.0                           # no kwh_used: ÷ efficiency
    est = fm.session_energy({"total_kwh": 20, "is_external": 1, "soc_start": 20, "soc_end": 70}, 0.9, cap=50)
    assert (est["batt"], est["source"], est["measured"]) == (25.0, "estimate", False)
    assert fm.session_energy({"total_kwh": None, "is_external": 0}, 0.9) is None


def test_consumption_one_rule(app_module, db):
    import logging
    import migrations
    assert fm.trip_consumption(2.0, 9.9) is None and fm.trip_consumption(2.0, 10) == 20.0
    a = fm.avg_consumption([{"energy_kwh": 40, "distance_km": 200}, {"energy_kwh": 2, "distance_km": 4},
                            {"energy_kwh": None, "distance_km": 100}])
    assert (a["cons"], a["km"], a["n"]) == (20.0, 200.0, 1)
    db.execute("INSERT INTO trips (device, start_time, end_time, distance_km, energy_kwh, consumption) VALUES "
               "('d', '2026-01-01T10:00', '2026-01-01T10:10', 4, 2, 50.0), "
               "('d', '2026-01-02T10:00', '2026-01-02T10:40', 40, 8, 20.0)")
    migrations.m070_consumption_min_km(db, logging.getLogger("t"))
    assert [r[0] for r in db.execute("SELECT consumption FROM trips ORDER BY start_time")] == [None, 20.0]


def test_soc_profile_takes_out_the_soc_window():
    # a battery of 85 kWh whose top 20 % hold 10 % more energy per % than the
    # rest: charges low in the range read too low, charges at the top too high
    shape = [0.975, 0.975, 0.975, 0.975, 1.1]
    def kwh(s0, s1):
        return sum(o * w for o, w in zip(fm._band_overlap(s0, s1), shape)) / 100 * 85
    windows = [(20, 80), (10, 60), (30, 80), (60, 100), (50, 100), (15, 75), (40, 95), (25, 85)] * 4
    pts = [{"ts": 1.7e9 + i * 86400, "kwh": kwh(a, b), "s0": a, "s1": b, "cap": kwh(a, b) / (b - a) * 100}
           for i, (a, b) in enumerate(windows)]
    raw = [p["cap"] for p in pts]
    assert max(raw) - min(raw) > 3
    prof = fm.soc_profile(pts, lambda ts: 85.0)
    assert prof[4] > prof[0] + 0.05
    fixed = [p["cap"] for p in fm.apply_soc_profile(pts, prof)]
    # the prior keeps thinly covered bands near 1, so not all of it goes
    assert max(fixed) - min(fixed) < (max(raw) - min(raw)) / 2
    assert all(p["cap_raw"] for p in fm.apply_soc_profile(pts, prof))
    assert fm.soc_profile(pts[:5], lambda ts: 85.0) is None          # too few charges


def test_rolling_median_per_battery():
    segs = [{"from": None}, {"from": "2026-06-01"}]
    pts = [{"ts": 1.77e9 + i * 86400 * 10, "cap": 50 + (i % 3)} for i in range(12)]
    m = fm.rolling_median(pts, segs, n=5)
    assert len(m) == 12 and all(x["cap"] is None or 50 <= x["cap"] <= 52 for x in m)


def test_dc_points_on_the_ac_level():
    # 85 kWh; DC charges read 2 % more (cell losses counted as energy in)
    import random
    rnd = random.Random(7)
    pts = []
    for i in range(60):
        dc = i % 2 == 0
        cap = 85 * (1.02 if dc else 1.0) * (1 + rnd.uniform(-0.01, 0.01))
        pts.append({"ts": 1.77e9 + i * 86400, "cap": cap, "kwh": cap * 0.5, "s0": 20, "s1": 70, "dc": dc})
    off = fm.dc_offset(pts, lambda ts: 85.85)
    assert off == pytest.approx(0.02, abs=0.004)
    fixed = fm.apply_dc_offset(pts, off)
    dc = [p["cap"] for p in fixed if p["dc"]]
    ac = [p["cap"] for p in fixed if not p["dc"]]
    assert abs(sum(dc) / len(dc) - sum(ac) / len(ac)) < 0.4
    assert fixed[0]["kwh"] == pytest.approx(pts[0]["kwh"] / (1 + off))
    assert fm.dc_offset(pts[:12], lambda ts: 85.85) is None          # too few of each
    same = [dict(p, cap=85 * (1 + rnd.uniform(-0.01, 0.01))) for p in pts]
    assert fm.dc_offset(same, lambda ts: 85.0) is None               # no clear difference


def test_counter_charge_dc_by_power():
    trips = [{"start_time": "2026-09-10T08:00:00", "end_time": "2026-09-10T10:00:00", "kw_start": 100, "kw_end": 110,
              "soc_start": 80, "soc_end": 20},
             {"start_time": "2026-09-10T10:40:00", "end_time": "2026-09-10T18:00:00", "kw_start": 150, "kw_end": 160,
              "soc_start": 70, "soc_end": 25},
             {"start_time": "2026-09-11T07:00:00", "end_time": "2026-09-11T08:00:00", "kw_start": 190, "kw_end": 195,
              "soc_start": 60, "soc_end": 50}]
    c = fm.counter_charges(trips)
    assert [x["dc"] for x in c] == [True, False]          # 40 kWh in 40 min / 30 kWh overnight


def test_theil_sen_on_many_points_stays_robust():
    import random
    rnd = random.Random(3)
    xy = [(i, 50 - 0.01 * i + rnd.gauss(0, 1.5) + (15 if i % 40 == 0 else 0)) for i in range(1500)]
    m, b = fm.theil_sen(xy)
    assert m == pytest.approx(-0.01, abs=0.002) and b == pytest.approx(50, abs=0.5)
