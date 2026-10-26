"""Charges reconstructed from trips/SoC at the HOME location must count as home.

Real case (February): the wallbox meter only started mid-month, so 12 earlier
home charges were stored as is_external=1 with location "Zuhause" and no cost.
They were missing from every home total (389.69 kWh instead of 927.72) and were
dragging the external average from 72.9 ct/kWh down to 7.0.

A charge location flagged is_home makes those charges home charges again:
billed at the flat rate, out of the external bucket.
"""
import re
import json


HOME_KWH = 538.03          # 12 reconstructed home charges, unpriced
METERED_KWH = 389.69       # wallbox-metered sessions
EXT_KWH = 57.39            # two real DC charges
EXT_COST = 41.87


def _setup(app_module, db, flag_home=True):
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    db.execute("INSERT INTO vehicles (name, plate, device) VALUES ('Nox', 'X-1', 'devX')")
    db.execute("INSERT INTO users (username, password_hash, is_admin) VALUES ('admin', ?, 1)",
               (app_module.generate_password_hash("Sup3r!secret"),))
    db.execute("INSERT OR REPLACE INTO charge_tariffs (valid_from, pauschale_kwh) VALUES ('2026-01-01', 0.34)")
    db.execute("INSERT INTO charge_locations (name, lat, lon, is_home) VALUES ('Zuhause', 52.0, 13.0, ?)",
               (1 if flag_home else 0,))
    db.execute("INSERT INTO charge_locations (name, lat, lon, is_home) VALUES ('Goldberg', 53.0, 12.0, 0)")

    db.execute("INSERT INTO trips (device, start_time, end_time, odo_end, distance_km) VALUES "
               "('devX', '2026-01-31 08:00', '2026-01-31 09:00', 300, 10)")
    db.execute("INSERT INTO trips (device, start_time, end_time, odo_end, distance_km) VALUES "
               "('devX', '2026-02-28 08:00', '2026-02-28 09:00', 3813, 100)")

    # 12 reconstructed home charges: external flag, home location, no cost/meter.
    for i in range(12):
        db.execute(
            "INSERT INTO charge_sessions (vehicle_plate, start_time, total_kwh, odometer,"
            " is_external, location_name) VALUES (?,?,?,?,1,'Zuhause')",
            ("X-1", f"2026-02-{i+3:02d}T16:00:00", HOME_KWH / 12, 300 + i * 100))
    # 10 metered wallbox sessions.
    for i in range(10):
        db.execute(
            "INSERT INTO charge_sessions (vehicle_plate, start_time, total_kwh, meter_start,"
            " meter_end, odometer, is_external, location_name) VALUES (?,?,?,?,?,?,0,'Zuhause')",
            ("X-1", f"2026-02-{i+16:02d} 21:00", METERED_KWH / 10,
             1000.0 + i * 40, 1000.0 + (i + 1) * 40, 1600 + i * 200))
    # Two real public DC charges, priced.
    db.execute("INSERT INTO charge_sessions (vehicle_plate, start_time, total_kwh, odometer,"
               " is_external, location_name, cost_total) VALUES"
               " ('X-1','2026-02-07 10:45', 29.11, 993, 1, 'Goldberg', 23.90),"
               " ('X-1','2026-02-07 17:57', 28.28, 1176, 1, 'Goldberg', 17.97)")
    db.commit()

    vid = db.execute("SELECT id FROM vehicles WHERE plate='X-1'").fetchone()[0]
    c = app_module.app.test_client()
    c.post("/login", data={"username": "admin", "password": "Sup3r!secret"})
    c.post("/api/user/active-vehicle", json={"vehicle_id": vid})
    return c


def _stats(c):
    r = c.get("/charges/print?year=2026&month=02&stats=1&ext_stats=1")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    labels = json.loads(re.search(r"const labels = (\[.*?\]);", html).group(1))
    kwh = json.loads(re.search(r"const dataKwh = (\[.*?\]);", html).group(1))
    return html, dict(zip(labels, kwh))


def test_home_flagged_location_counts_as_home_charge(app_module, db):
    c = _setup(app_module, db, flag_home=True)
    html, kwh_by_month = _stats(c)

    # Home energy = metered wallbox + reconstructed home charges.
    assert kwh_by_month["02/2026"] == round(METERED_KWH + HOME_KWH, 2)

    # External bucket holds ONLY the two real DC charges -> sane ct/kWh.
    assert f"{EXT_KWH:.2f} kWh" in html
    assert f"{EXT_COST / EXT_KWH * 100:.1f}" in html      # 72.9 ct/kWh


def test_without_home_flag_the_old_broken_split_remains(app_module, db):
    c = _setup(app_module, db, flag_home=False)
    _, kwh_by_month = _stats(c)
    # Unflagged: reconstructed charges stay external -> home total is short.
    assert kwh_by_month["02/2026"] == round(METERED_KWH, 2)
