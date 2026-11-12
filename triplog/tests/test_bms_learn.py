"""BMS learning: the display SoC rebuilt from the CAN SoC, VW's range rebuilt
from display SoC × E ÷ the displayed average, an own range span from past
trips, and the dashboard routes (only with a CAN value younger than 5 min)."""

from datetime import datetime, timedelta, timezone

import bms_learn as bl


def pts(pairs, **extra):
    return [dict({"can_soc": c, "disp_soc": d, "bat_temp": 20, "ext_temp": 10}, **extra) for c, d in pairs]


def test_window_consumption_and_a_charge_in_between():
    # 60 km at 0.25 % SoC per km, 80 kWh: 20 kWh/100 km
    series = [(1000 + k, 90 - 0.25 * k) for k in range(61)]
    w = bl.window_consumption(series, 80.0)
    assert w[10] == 20.0 and w[30] == 20.0 and w[50] == 20.0 and w[100] is None
    # charged at km 1030, now at 1060: 30 km fit after the charge, 50 do not
    charged = [(1000 + k, 40 - 0.25 * k) for k in range(30)] + \
              [(1030 + k, 80 - 0.25 * k) for k in range(31)]
    w2 = bl.window_consumption(charged, 80.0)
    assert w2[10] == 20.0 and w2[30] == 20.0 and w2[50] is None


def test_display_model_learns_the_buffer():
    # VW-like: display = (CAN − 5) / 0.9, shown as whole numbers
    pairs = [(c, round((c - 5) / 0.9)) for c in (20, 35, 50, 62, 74, 88)]
    m = bl.fit_display(pts(pairs))
    assert m["terms"] == ["can"] and m["n"] == 6
    assert m["mae_loo"] < 0.6                              # rounding is all that is left
    assert abs(bl.predict_display(m, 74.0) - (74 - 5) / 0.9) < 0.7
    assert abs(bl.display_zero_can(m) - 5.0) < 0.8
    assert bl.fit_display(pts(pairs[:2])) is None          # too few points


def test_temperature_only_joins_with_points_and_spread():
    base = [(c, round((c - 5) / 0.9)) for c in (20, 30, 40, 50, 60, 70, 80, 90)]
    flat = bl.fit_display(pts(base))
    assert flat["terms"] == ["can"]                         # no temperature spread
    warm = [dict(p, ext_temp=t) for p, t in zip(pts(base), (-5, 0, 5, 10, 15, 20, 25, 30))]
    assert "ext_temp" in bl.fit_display(warm)["terms"]


def test_range_rebuilt_from_e_and_the_window_vw_follows():
    # the two real readings: 65 % 225 km 24.4, 62 % 213 km 25.6
    rows = [{"disp_soc": 65, "disp_range": 225, "disp_cons": 24.4, "cons_10": 30.0, "cons_30": 24.5, "cons_50": 22.0},
            {"disp_soc": 62, "disp_range": 213, "disp_cons": 25.6, "cons_10": 19.0, "cons_30": 25.4, "cons_50": 22.1}]
    r = bl.fit_range(rows)
    assert 84 < r["e_kwh"] < 88 and r["window_km"] == 30 and abs(r["cons_factor"] - 1.0) < 0.02
    assert r["range_mae"] < 6
    assert r["can_range_n"] == 0 and r["can_range_diff"] is None
    rows[0]["can_range"], rows[1]["can_range"] = 220, 209
    r = bl.fit_range(rows)
    assert r["can_range_n"] == 2 and r["can_range_diff"] == 4.5 and r["can_range_spread"] == 0.5
    assert r["can_range_reliable"] is False                     # 2 < 5 points


def test_range_comes_from_can_once_the_offset_is_stable():
    pts = [{"disp_soc": 60 + i, "disp_range": 200 + 3 * i + (i % 2), "can_range": 183 + 3 * i, "disp_cons": 23.0}
           for i in range(5)]
    off = bl.range_offset(pts)
    assert off["n"] == 5 and off["diff"] == 17 and off["spread"] <= 1 and off["reliable"]
    # a new point without typed range gets CAN + offset; typed ones stay
    pts.append({"disp_soc": 70, "disp_range": None, "can_range": 210, "disp_cons": 23.0})
    filled = bl.fill_range_from_can(pts)
    assert filled[-1]["disp_range"] == 227 and filled[-1]["range_from_can"]
    assert filled[0]["disp_range"] == 200 and "range_from_can" not in filled[0]
    assert bl.fit_range(pts)["n"] == 6
    # unstable offset: nothing is filled in
    pts[0]["disp_range"], pts[1]["disp_range"] = 260, 150
    pts[2]["disp_range"] = 240
    assert not bl.range_offset(pts)["reliable"]
    assert bl.fill_range_from_can(pts)[-1]["disp_range"] is None


def test_consumption_typed_as_wh_per_km_or_kwh_per_100(app_module):
    import blueprints.bms as bms
    assert bms._cons({"disp_cons": "228"}) == 22.8
    assert bms._cons({"disp_cons": "22,8"}) == 22.8
    assert bms._cons({"disp_cons": ""}) is None
    for bad in ("2", "900", "95"):
        try:
            bms._cons({"disp_cons": bad})
            assert bad == "95"                                  # 95 kWh/100 km: out of range
        except ValueError:
            pass


def test_real_range_from_trips_at_a_similar_temperature():
    trips = [{"km": 50, "cons": 18.0, "temp": 15}] * 4 + [{"km": 50, "cons": 26.0, "temp": -2}] * 4
    cold = bl.real_range(52.0, trips, -1.5)
    assert cold["km_mid"] == 200 and cold["temp_band"] == 4.0   # only the cold trips
    assert bl.real_range(52.0, trips[:1], -1.5) is None          # not enough km
    assert bl.real_range(0, trips, 10) is None


def _client(app_module, db, enabled=True):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) VALUES (1, 'u', 'x', 1, 1)")
    db.execute("INSERT INTO vehicles (id, plate, device, battery_capacity_kwh, bms_learn) VALUES (1, 'NOX', 'dev1', 84, ?)",
               (1 if enabled else 0,))
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    c = app_module.app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
    return c


def _state(app_module, age_s):
    t = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    app_module._state_cache["dev1"] = {"soc": 74.0, "bat_temp": 12.0, "ext_temp": -1.5, "speed": 80,
                                       "odometer": 36000, "range_km": 228, "field_times": {"soc": t}}


def test_save_only_with_a_fresh_can_value(app_module, db, monkeypatch):
    import blueprints.bms as bms
    monkeypatch.setattr(bms, "_series", lambda device, hours=36: [])
    c = _client(app_module, db)
    try:
        _state(app_module, 600)                                   # 10 min old
        r = c.post("/api/bms-learn", json={"disp_soc": "72", "disp_range": "230", "disp_cons": "24,4"})
        assert r.status_code == 409 and "10" in r.get_json()["error"]
        _state(app_module, 20)
        r = c.post("/api/bms-learn", json={"disp_soc": "72", "disp_range": "230", "disp_cons": "244"})
        d = r.get_json()
        assert r.status_code == 200 and d["n"] == 1 and d["live"]["fresh"]
        p = d["points"][0]
        assert (p["disp_soc"], p["disp_range"], p["disp_cons"], p["can_soc"], p["ext_temp"], p["can_range"]) == \
            (72, 230, 24.4, 74, -1.5, 228)
        assert d["range"]["can_range_diff"] == 2 and d["range"]["can_range_n"] == 1
        lr = d["learn"]
        assert lr["n"] == 1 and lr["range_n"] == 1 and lr["range_need"] == 5 and not lr["range_from_can"]
        assert c.post("/api/bms-learn", json={"disp_soc": "172"}).status_code == 400
        assert c.delete(f"/api/bms-learn/{p['id']}").get_json()["n"] == 0
    finally:
        app_module._state_cache.pop("dev1", None)


def test_not_enabled_vehicle_cannot_save(app_module, db):
    c = _client(app_module, db, enabled=False)
    try:
        _state(app_module, 5)
        assert c.post("/api/bms-learn", json={"disp_soc": "72"}).status_code == 400
        assert c.get("/api/bms-learn").get_json()["enabled"] is False
        # since 1.2.287 the range box stays, the learning button and dialog go
        page = c.get("/").get_data(as_text=True)
        assert 'id="bmsBox"' in page
        assert 'data-onclick="openBms"' not in page and 'id="bmsOv"' not in page
    finally:
        app_module._state_cache.pop("dev1", None)


def test_a_frozen_can_range_is_not_a_range(app_module):
    """The stick stopped sending the range; the last value of months ago
    (86.4, by its looks the capacity) stayed in every learning point and
    next to today's SoC."""
    pts = [{"disp_soc": s, "disp_range": r, "disp_cons": 22.0, "can_range": 86.4}
           for s, r in ((30, 116), (27, 103), (74, 293), (47, 191), (59, 237))]
    off = bl.range_offset(pts)
    assert off["n"] == 0 and off["diff"] is None and not off["reliable"]
    assert bl.fit_range(pts)["can_range_n"] == 0
    # a real one moves with the display
    real = [dict(p, can_range=p["disp_range"] - 17) for p in pts]
    assert bl.range_offset(real)["reliable"] and bl.range_offset(real)["diff"] == 17
    # two points at the same charge are no proof either way
    assert bl.range_offset(pts[:2])["n"] == 2
    cur = app_module.range_is_current
    assert cur("2026-11-12T11:30:00Z", "2026-11-12T11:37:00Z")          # came with the SoC
    assert cur("2026-11-10T18:00:00Z", "2026-11-10T18:02:00Z")          # parked for days: both old, still the car's range
    assert not cur("2026-08-01T10:00:00Z", "2026-11-12T11:37:00Z")      # the SoC went on for months without it
    assert not cur(None, "2026-11-12T11:37:00Z") and cur("2026-11-12T11:30:00Z", None)
