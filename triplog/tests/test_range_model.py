"""The consumption model behind the range (1.2.327, reworked on real trips
in 1.2.329): a car's own trips, cold per km, a fit that a few odd trips do
not bend, and what a start after standing costs, measured on short trips.
Heating as power × time is only taken when it clearly predicts trips left
out of the fit better. Plus: where in a trip a learning point was taken, the outside
temperature during a trip, and the export of everything as JSON."""

import json
import math
import random
import time
from datetime import datetime, timedelta, timezone

import bms_learn as bl

HOUR = {"a": 13.0, "b": 7.5, "p": 0.25}                 # heating 0.25 kW per K: per km more the slower
KM = {"a": 13.0, "b": 7.5, "c": 0.3}                    # cold costs per km


def trips(n, truth, seed=1, noise=0.05, outliers=0, start=0.0):
    """Invented trips with a known truth. start: kWh every trip costs once
    after the car stood (less after a short stop)."""
    r, out = random.Random(seed), []
    for i in range(n):
        temp = 10 - 12 * math.cos(2 * math.pi * r.random()) + r.gauss(0, 3)
        km, v = r.choice([(r.uniform(4, 25), r.uniform(28, 50)), (r.uniform(20, 70), r.uniform(50, 80)),
                          (r.uniform(80, 300), r.uniform(90, 120))])
        stood = r.choice([0.3, 1, 4, 9, 14, 40])
        cold = max(0, 15 - temp)
        cons = truth["a"] + truth["b"] * (v / 100) ** 2 + truth.get("c", 0) * cold + truth.get("p", 0) * cold * 100 / v \
            + start * (1 - math.exp(-stood / 2)) * 100 / km
        cons *= (1 + r.gauss(0, noise)) * (1.9 if i < outliers else 1)
        out.append({"km": km, "v": v, "temp": temp, "stood_h": stood, "kwh": cons * km / 100,
                    "cons": cons if km >= 10 else None})
    return out


def test_cold_per_km_is_the_simple_reading():
    m = bl.speed_model(trips(400, KM, noise=0.02))
    assert m["form"] == "km" and m["p"] == 0 and abs(m["c"] - 0.3) < 0.03 and abs(m["b"] - 7.5) < 0.4
    assert set(m["cv"]) == {"km", "hour"}                               # both were checked on trips left out


def test_heating_as_power_has_to_earn_its_place():
    # the trips show it clearly: slow cold trips cost far more per km
    m = bl.speed_model(trips(400, HOUR, noise=0.02))
    assert m["form"] == "hour" and m["c"] == 0 and abs(m["p"] - 0.25) < 0.03
    assert m["cv"]["hour"] <= m["cv"]["km"] * (1 - bl.CV_GAIN)
    assert abs(bl.consumption(m, 25, 2) - (13 + 7.5 * 0.0625 + 0.25 * 13 * 4)) < 1.0
    # a truth in between, with the scatter of real trips: no clear winner, the simple form stays
    mixed = bl.speed_model(trips(300, {"a": 13.0, "b": 7.5, "c": 0.2, "p": 0.08}, noise=0.10))
    assert mixed["form"] == "km" and mixed["p"] == 0


def test_the_start_after_standing_is_measured_on_short_trips():
    m = bl.speed_model(trips(500, KM, noise=0.02, start=0.4))
    assert m["start_n"] >= 15 and abs(m["e_start"] - 0.4) < 0.12
    assert abs(m["a"] - 13.0) < 0.5                                     # … and no longer sits in the base consumption
    none = bl.speed_model(trips(500, KM, noise=0.02))
    assert none["e_start"] < 0.1
    # too few short trips after a stop: nothing is assumed
    few = bl.speed_model([t for t in trips(500, KM, start=0.4) if t["km"] >= 25 or t["stood_h"] < 2][:300])
    assert few["e_start"] == 0 and few["start_n"] < 15


def test_a_few_odd_trips_do_not_bend_a_small_model():
    for seed in range(8):
        m = bl.speed_model(trips(60, KM, seed=seed, outliers=5))        # every twelfth trip reads 90 % too high
        for v, temp in ((110, 2), (60, 2), (110, 20)):
            truth = 13 + 7.5 * (v / 100) ** 2 + 0.3 * max(0, 15 - temp)
            assert abs(bl.consumption(m, v, temp) - truth) < 3.0, (seed, v, temp, m)


def test_the_start_costs_less_after_a_short_stop():
    m = {"a": 13.0, "b": 7.5, "c": 0.3, "e_start": 0.4}
    night, short = bl.warmup_kwh(m, 2, 12), bl.warmup_kwh(m, 2, 0.3)
    assert abs(night - 0.4) < 0.01 and short < 0.2 * night
    assert bl.warmup_kwh(m, 2, None) == 0.4                             # unknown: stood long
    assert bl.warmup_kwh(m, 25, 12) == night                            # the cold makes no difference to it
    assert bl.warmup_kwh(KM, 2, 12) == 0                                # an older model without it still computes
    assert abs(bl.consumption(KM, 110, 5) - (13 + 7.5 * 1.21 + 3.0)) < 1e-9
    # head wind raises the drag, not the heating
    assert abs(bl.consumption(HOUR, 100, 2, v_air=120) - bl.consumption(HOUR, 100, 2) - 7.5 * 0.44) < 1e-9


def test_where_in_a_trip_the_car_is():
    now = 100000.0
    # stood 9 h, set off 6 min ago, 4 km so far
    s = [(now - 9 * 3600 - 2000, 5000, 80), (now - 9 * 3600 - 360, 5010, 78)] + \
        [(now - 360 + 90 * k, 5010 + k, 78 - 0.3 * k) for k in range(1, 5)]
    c = bl.trip_context(s, now)
    assert c["trip_km"] == 4 and c["parked_h"] == 0 and abs(c["stood_h"] - 9.0) < 0.1 and 3 <= c["trip_min"] <= 6
    assert bl.fresh_trip(c)
    # 40 min after arriving: the trip is over, the car stands
    c2 = bl.trip_context(s, now + 2400)
    assert c2["trip_km"] == 4 and 0.6 < c2["parked_h"] < 0.75
    assert bl.trip_context([(now - 500, 5000, 80)], now)["parked_h"] is None      # no km in the series
    assert not bl.fresh_trip({"trip_km": None}) and not bl.fresh_trip({"trip_km": 35})


def test_a_point_from_the_first_km_stays_out_of_the_range_formula():
    good = [{"disp_soc": 60 + i, "disp_range": round((60 + i) * 0.82 / 23 * 100), "disp_cons": 23.0, "trip_km": 40}
            for i in range(4)]
    cold = {"disp_soc": 80, "disp_range": 285, "disp_cons": 40.0, "trip_km": 3}   # cold start on the display
    r = bl.fit_range(good + [cold])
    assert r["n"] == 4 and r["fresh_left_out"] == 1 and abs(r["e_kwh"] - 82) < 1
    assert bl.fit_range([cold])["n"] == 1                                # the only point there is still counts
    assert bl.fit_range(good + [dict(cold, trip_km=None)])["n"] == 5     # older points without the context


def test_outside_temperature_of_a_trip():
    utc = timezone.utc
    hourly = {"2026-11-12T05": 1.0, "2026-11-12T06": 3.0, "2026-11-12T13": 9.0}
    t = lambda h, m=0: datetime(2026, 11, 12, h, m, tzinfo=utc)
    assert bl.trip_temp(hourly, t(5, 40), t(6, 20)) == 2.0               # both hours
    assert bl.trip_temp(hourly, t(13, 5), t(13, 30)) == 9.0              # not the mean of the day
    assert bl.trip_temp(hourly, t(9), t(9, 30)) == round(13 / 3, 1)      # no value then: the day
    assert bl.trip_temp(hourly, datetime(2026, 11, 13, 9, tzinfo=utc), None) is None
    assert bl.trip_temp({}, t(5), t(6)) is None


def _client(app_module, db):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) VALUES (1, 'u', 'x', 1, 1)")
    db.execute("INSERT INTO vehicles (id, plate, device, battery_capacity_kwh, bms_learn) VALUES (1, 'NOX', 'dev1', 84, 1)")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    c = app_module.app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
    return c


def _temp_of(start_utc):
    return float((start_utc.day * 5 + start_utc.hour * 3) % 16 - 3)      # -3 … 12 °C


def _seed_trips(db, n=90):
    """A winter of trips of all kinds: short and long, after a night or
    after an hour, colder and milder days."""
    import blueprints.bms as bms
    kinds = [(6.0, 35.0), (12.0, 35.0), (18.0, 45.0), (35.0, 65.0), (60.0, 75.0), (160.0, 105.0), (240.0, 115.0)]
    r, t, prev_end = random.Random(4), datetime(2026, 9, 1, 7, 0), None
    for i in range(n):
        km, v = kinds[i % len(kinds)]
        cold = max(0.0, 15 - _temp_of(bms._utc(t.isoformat())))
        w = 1 - math.exp(-((t - prev_end).total_seconds() / 3600) / 2) if prev_end else 1.0
        cons = 13 + 7.5 * (v / 100) ** 2 + 0.3 * cold + 0.4 * w * 100 / km
        end = t + timedelta(hours=km / v)
        db.execute("INSERT INTO trips (device, start_time, end_time, distance_km, energy_kwh, soc_start, soc_end, "
                   "start_lat, start_lon, start_address) VALUES ('dev1', ?, ?, ?, ?, 80, 60, 53.5, 10.0, 'Secret Street 1')",
                   (t.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S"), km,
                    cons * km / 100 * (1 + r.gauss(0, 0.02))))
        prev_end = end
        t = end + timedelta(hours=1 if i % 4 == 1 else 15)
    db.commit()


def test_export_and_dialog(app_module, db, monkeypatch):
    import blueprints.bms as bms
    now = time.time()
    # stood 10 h, on the road for 5 km
    series = [(now - 11 * 3600, 36000 - 5 - 20, 80), (now - 10 * 3600 - 400, 36000 - 5, 76)] + \
             [(now - 400 + 70 * k, 36000 - 5 + k, 76 - 0.4 * k) for k in range(1, 6)]
    monkeypatch.setattr(bms, "_series", lambda device, hours=36: series)
    monkeypatch.setattr(bms, "_hourly", lambda device, field="et": {"x": 2.0} if field == "et" else {})
    monkeypatch.setattr(bl, "trip_temp", lambda hourly, a, b: _temp_of(a) if hourly else None)
    bms._model_cache.clear()
    c = _client(app_module, db)
    _seed_trips(db)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    app_module._state_cache["dev1"] = {"soc": 74.0, "bat_temp": 4.0, "ext_temp": 2.0, "speed": 50, "odometer": 36000,
                                       "range_km": 228, "field_times": {"soc": stamp}}
    try:
        for soc in (70, 71, 72, 73, 74):
            d = c.post("/api/bms-learn", json={"disp_soc": str(soc), "disp_range": "230", "disp_cons": "400"}).get_json()
        assert d["n"] == 5 and len(d["points"]) == 3                     # the dialog lists the last three only
        assert d["live"]["fresh_trip"] and d["live"]["trip_km"] == 5
        cr = d["now"]["cruise"]
        assert cr["model"]["form"] == "km" and abs(cr["model"]["e_start"] - 0.4) < 0.1
        assert cr["parked_h"] == 0 and cr["warmup_kwh"] == 0                 # driving: nothing to take off
        r = c.get("/api/bms-learn/export")
        assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
        raw = r.get_data(as_text=True)
        x = json.loads(raw)
        assert len(x["points"]) == 5 and x["points"][0]["trip_km"] == 5 and abs(x["points"][0]["stood_h"] - 10) < 0.2
        assert x["speed_model"]["form"] == "km" and set(x["speed_model"]["cv"]) == {"km", "hour"}
        t = x["trips"][5]
        assert t["km"] == 160 and t["temp"] is not None and t["stood_h"] == 15.0 and t["in_model"]
        assert abs(t["model_cons"] - t["cons"]) < 1.0 and 0.3 < t["model_warmup_kwh"] < 0.5
        assert x["trips"][2]["stood_h"] == 1.0
        # nothing that says where the car was or which car it is
        for word in ("NOX", "dev1", "Secret", "start_lat", "53.5", "address"):
            assert word not in raw, word
        # the car stands in the cold: the warm-up comes off the energy for the range
        bms._model_cache.clear()
        monkeypatch.setattr(bms, "_series", lambda device, hours=36: [(t - 9 * 3600, o, s) for t, o, s in series])
        cold = c.get("/api/bms-learn").get_json()["now"]["cruise"]
        assert cold["parked_h"] > 8 and 0.3 < cold["warmup_kwh"] < 0.5
        assert abs((cr["left"] - cold["left"]) - cold["warmup_kwh"]) < 0.02 and cold["km"] < cr["km"]
    finally:
        app_module._state_cache.pop("dev1", None)
        bms._model_cache.clear()


def test_before_setting_off_the_forecast_gives_the_temperature(app_module, db, monkeypatch):
    """Standing, the car's sensor holds yesterday's value or the garage. The
    temperature that comes with the wind forecast is used instead, so the
    range before the trip already knows the weather."""
    import blueprints.bms as bms
    import config
    import wind
    lat, lon = wind.grid(54.0, 10.0, 0.0)[0]                             # a raster point: the car stands there
    hour = int(time.time() // 3600) * 3600
    data = {"t": [hour - 3600, hour, hour + 3600], "kmh": [10, 10, 10], "dir": [270, 270, 270],
            "gust": [20, 20, 20], "temp": [1.0, -3.5, -4.0]}
    db.execute("INSERT INTO wind_points (key, lat, lon, data, fetched) VALUES (?, ?, ?, ?, ?)",
               (wind._key((lat, lon)), lat, lon, json.dumps(data), time.time()))
    db.commit()
    assert wind.temp_at(db, lat, lon) == -3.5 and wind.temp_at(db, lat, lon, hour + 3700) == -4.0
    assert wind.temp_at(db, lat, lon, hour + 9 * 3600) is None            # the forecast ends before
    assert wind.temp_at(db, -30.0, 100.0) is None                         # nothing stored there

    monkeypatch.setattr(config, "WIND_SOURCE", "open-meteo")
    app_module._state_cache["dev1"] = {"lat": lat, "lon": lon}
    try:
        garage = {"ext_temp": 14.0, "speed": 0}
        assert bms._outside_temp(db, "dev1", garage, 9.0) == (-3.5, "forecast")       # stood over night
        assert bms._outside_temp(db, "dev1", garage, None) == (-3.5, "forecast")      # no km for days
        assert bms._outside_temp(db, "dev1", {"ext_temp": -2.0, "speed": 0}, 0.0) == (-2.0, "car")     # on a trip, at a light
        assert bms._outside_temp(db, "dev1", {"ext_temp": -2.0, "speed": 80}, None) == (-2.0, "car")   # driving
        monkeypatch.setattr(config, "WIND_SOURCE", "")
        assert bms._outside_temp(db, "dev1", garage, 9.0) == (14.0, "car")            # no forecast set up
        monkeypatch.setattr(config, "WIND_SOURCE", "open-meteo")
        app_module._state_cache["dev1"] = {}
        assert bms._outside_temp(db, "dev1", garage, 9.0) == (14.0, "car")            # position unknown
    finally:
        app_module._state_cache.pop("dev1", None)
