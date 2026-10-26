"""Analysis → "Fahrzeug": certificate-like report over the whole history."""

import pytest

import diagnose
import fahrzeugmodell as fm


def test_season_recuperation_charging_helpers():
    trips = [{"start_time": "2026-01-10T08:00:00", "distance_km": 50, "energy_kwh": 12.5, "kw_start": 10, "kw_end": 12}] * 3
    trips += [{"start_time": "2026-07-10T08:00:00", "distance_km": 50, "energy_kwh": 9.0, "kw_start": 10, "kw_end": 11.5}] * 3
    s = fm.season_consumption(trips)
    assert s["winter"]["cons"] == 25.0 and s["summer"]["cons"] == 18.0
    r = fm.recuperation(trips)
    assert r["kwh_100km"] == round((6 + 4.5) / 300 * 100, 1)
    c = fm.charging_summary([
        {"total_kwh": 40, "is_external": 0, "avg_kw": 11, "soc_start": 20, "soc_end": 80, "cost_pauschale": 12},
        {"total_kwh": 50, "is_external": 1, "avg_kw": 120, "soc_start": 10, "soc_end": 96, "cost_total": 30}])
    assert c["n"] == 2 and c["kwh_dc"] == 50 and c["full_pct"] == 50 and c["ct_per_kwh"] == round(42 / 90 * 100, 1)
    # a TeslaMate import at home is stored as external, the home location tells
    tm = [{"total_kwh": 30, "is_external": 1, "avg_kw": 10, "location_name": "Zuhause"},
          {"total_kwh": 10, "is_external": 1, "avg_kw": 100, "location_name": "SuC Reinfeld"}]
    c = fm.charging_summary(tm, {"Zuhause"})
    assert (c["kwh_home"], c["kwh_ext"], c["kwh_dc"]) == (30, 10, 10)


def test_overlap_sweep():
    rows = [(1, "2026-02-10T08:06:00", "2026-02-10T10:15:00"), (2, "2026-02-10T10:10:00", "2026-02-10T11:25:00"),
            (3, "2026-02-10T11:00:00", "2026-02-10T12:00:00"), (4, "2026-02-11T08:00", "2026-02-11T09:00")]
    assert diagnose.overlapping_trips(rows) == [3, 2]       # 1/2 overlap exactly 5 min: tolerated


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) VALUES (1, 'admin', 'x', 1, 1)")
    db.execute("INSERT INTO vehicles (id, plate, name, model, device, vin, battery_capacity_kwh) "
               "VALUES (1, 'EV1', 'Nox', 'ID.7', 'dev1', 'WVWZZZE1ZPP012345', 86)")
    kw = 1000.0
    for d in range(1, 13):
        db.execute("INSERT INTO trips (device, start_time, end_time, odo_start, odo_end, distance_km, soc_start, "
                   "soc_end, energy_kwh, kw_start, kw_end, is_manual) VALUES ('dev1', ?, ?, ?, ?, 60, 80, 66, 12.0, ?, ?, 0)",
                   (f"2026-05-{d:02d}T08:00:00", f"2026-05-{d:02d}T09:00:00", 1000 + d * 60, 1060 + d * 60, kw, kw + 1.8))
        kw += 1.8 + 42.5                     # parked: +50 % SoC = 42.5 kWh into an 85 kWh battery
        db.execute("UPDATE trips SET soc_end = 30 WHERE start_time = ?", (f"2026-05-{d:02d}T08:00:00",))
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def test_report_endpoint(app_module, client):
    app_module._trend_cache.clear(); app_module._eff_cache.clear()
    r = client.get("/api/vehicle-report")
    assert r.status_code == 200
    d = r.get_json()
    assert d["vehicle"]["plate"] == "EV1" and d["vehicle"]["vin"] == "WVW…2345"
    assert d["battery"]["source"] == "counter" and d["battery"]["nominal_kwh"] == 86
    assert d["mileage"]["trips"] == 12 and d["mileage"]["odometer"] == 1780
    assert d["consumption"]["seasons"]["spring"]["cons"] == 20.0
    assert len(d["meta"]["fingerprint"]) == 12
    assert client.get("/api/vehicle-report").get_json()["meta"]["fingerprint"] == d["meta"]["fingerprint"]


def test_analysis_page_has_vehicle_tab(client):
    html = client.get("/analysis").get_data(as_text=True)
    assert 'id="tab-fahrzeug"' in html and "loadVehicleReport" in html


def test_monthly_consumption_and_temperature_fit():
    trips = []
    for m, cons, temp in ((1, 25.0, 0), (2, 24.0, 2), (3, 22.0, 7), (4, 20.5, 11), (5, 19.0, 16), (6, 18.0, 20),
                          (7, 18.0, 22), (8, 18.5, 21)):
        trips.append({"start_time": f"2026-{m:02d}-10T08:00:00", "distance_km": 400, "energy_kwh": cons * 4})
    months = fm.monthly_consumption(trips)
    assert [x["cons"] for x in months][:3] == [25.0, 24.0, 22.0]
    temps = {1: 0, 2: 2, 3: 7, 4: 11, 5: 16, 6: 20, 7: 22, 8: 21}
    for x in months:
        x["temp"] = temps[int(x["month"][5:7])]
    fit = fm.temperature_fit(months)
    assert 17.0 <= fit["at_ref"] <= 18.5 and 2.5 <= fit["per_10c_colder"] <= 4.0 and fit["months"] == 8
    assert fm.temperature_fit(months[:3]) is None


def test_stored_efficiency_back_to_automatic(db):
    import logging
    import migrations
    db.execute("INSERT INTO vehicles (plate, device, charge_efficiency, charge_efficiency_dc) VALUES ('X', 'd', 0.933, 0.995)")
    db.commit()
    migrations.m068_efficiency_automatic(db, logging.getLogger("t"))
    assert tuple(db.execute("SELECT charge_efficiency, charge_efficiency_dc FROM vehicles").fetchone()) == (None, None)


def test_data_accuracy_score():
    best = fm.data_accuracy(100, 0, 10000, 0, 0)
    assert best["score"] == 100 and "grade" not in best
    nox = fm.data_accuracy(91, 77, 37699, 2, 5)
    assert nox["score"] == 90 and nox["parts"] == {"energy": 91, "gaps": 98, "findings": 70}
    poor = fm.data_accuracy(40, 2000, 10000, 5, 10)
    assert poor["score"] < 60


def test_report_carries_accuracy(app_module, client):
    app_module._trend_cache.clear(); app_module._eff_cache.clear()
    d = client.get("/api/vehicle-report").get_json()
    acc = d["quality"]["accuracy"]
    assert 0 <= acc["score"] <= 100 and set(acc["parts"]) == {"energy", "gaps", "findings"}


def _rest(month_shift_v):
    """Rest samples over six months: voltage curve 1 V per % around 80 %,
    0.5 V per % around 30 %; the top moves by month_shift_v per month."""
    from datetime import datetime
    out = []
    for m in range(6):
        ts0 = datetime(2026, 3 + m, 10).timestamp()
        for k in range(8):
            s = 74 + k * 1.5
            out.append({"ts": ts0 + k * 3600, "s": s, "u": 418 + (s - 80) * 1.0 + m * month_shift_v, "bt": 20})
            s = 24 + k * 1.5
            out.append({"ts": ts0 + k * 3600, "s": s, "u": 374 + (s - 30) * 0.5, "bt": 20})
    return out


def test_buffer_watch_hint():
    w = fm.buffer_watch(_rest(0.0))
    assert w["top"]["slope_v_per_pct"] == pytest.approx(1.0, abs=0.01)
    assert len(w["top"]["months"]) == 6 and w["top"]["hint"] == "none" and w["bottom"]["hint"] == "none"
    w = fm.buffer_watch(_rest(0.4))                    # +2 V at 80 % over the months ≈ 2 % SoC
    assert w["top"]["hint"] == "released" and w["top"]["shift_pct"] == pytest.approx(1.6, abs=0.1)
    assert fm.buffer_watch(_rest(0.0)[:6]) == {}


def test_bms_resets():
    daily = {"2026-04-01": 86.4, "2026-04-10": 85.5, "2026-04-20": 84.9, "2026-04-21": 86.4,
             "2026-05-01": 85.0, "2026-05-02": 86.35, "2026-05-03": 86.2}
    r = fm.bms_resets(daily)
    assert r == {"ceiling": 86.4, "resets": ["2026-04-21", "2026-05-02"]}
    assert fm.bms_resets({}) is None


def test_model_and_report_carry_the_buffer_watch(app_module, client, monkeypatch):
    from blueprints import model
    model._buffer_cache.clear()
    monkeypatch.setattr(model, "_rest_samples", lambda device: _rest(0.0))
    app_module._trend_cache.clear(); app_module._eff_cache.clear()
    d = client.get("/api/vehicles/EV1/model").get_json()
    assert d["buffer"]["top"]["hint"] == "none"
    assert client.get("/api/vehicle-report").get_json()["battery"]["buffer"]["bottom"]["center"] == 30.0
    model._buffer_cache.clear()
