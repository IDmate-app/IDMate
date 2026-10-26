"""Regression: '€ per 100 km incl. external charges' chart in charge_print.

The HOME interval model (home-to-home charge intervals, odometer deltas) is
the REFERENCE. The 'incl. external' chart runs the SAME walk, just extended by
every segment touching a tracked external charge — so a month without external
charges shows exactly the home bar, and on a travel month the externally
driven km enter through the odometer stamps of the external sessions.

Two earlier denominators were wrong and stay guarded against:

1. home-to-home interval km with the FULL external cost on top (~61 EUR/100km
   for a month that really cost ~8);
2. SUM(trips.distance_km), which undercounts when the telemetry stick is
   offline on the road trip (462 trip-km against 2566 km between charges).
"""
import re
import json


def _setup(app_module, db):
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    db.execute("INSERT INTO vehicles (name, plate, device) VALUES ('Car', 'X-1', 'devX')")
    db.execute("INSERT INTO users (username, password_hash, is_admin) VALUES ('admin', ?, 1)",
               (app_module.generate_password_hash("Sup3r!secret"),))
    db.execute("INSERT OR REPLACE INTO charge_tariffs (valid_from, pauschale_kwh) VALUES ('2026-05-01', 0.34)")

    # May closes at odometer 20000 -> the June baseline.
    db.execute(
        "INSERT INTO trips (device, start_time, end_time, odo_end, distance_km) VALUES "
        "('devX', '2026-05-28 08:00', '2026-05-28 09:00', 20000, 50)"
    )
    # June: the car really drove 20000 -> 22566 = 2566 km, but the telemetry only
    # captured 462 km of trips (stick offline on the road trip). The odometer on
    # the charge sessions below is the reliable source.
    db.execute(
        "INSERT INTO trips (device, start_time, end_time, odo_end, distance_km) VALUES "
        "('devX', '2026-06-05 08:00', '2026-06-05 09:00', 20200, 200),"
        "('devX', '2026-06-18 08:00', '2026-06-18 09:00', 20462, 262)"
    )
    # Home wallbox sessions (odometer stamped by HA -> reliable). Only ONE
    # home-to-home interval exists (20000 -> 20500); everything after runs
    # through the external chargers of the road trip.
    db.execute(
        "INSERT INTO charge_sessions "
        "(vehicle_plate, start_time, total_kwh, meter_start, meter_end, odometer, cost_pauschale, is_external) VALUES "
        "('X-1', '2026-06-05 11:45', 100.0, 5000.0, 5100.0, 20000, 34.00, 0),"
        "('X-1', '2026-06-08 11:45', 100.0, 5100.0, 5200.0, 20500, 34.00, 0),"
        "('X-1', '2026-06-20 10:00', 100.0, 5200.0, 5300.0, 22566, 34.00, 0)"
    )
    # Dominant external charging on the road trip: total cost 97.50 €.
    db.execute(
        "INSERT INTO charge_sessions "
        "(vehicle_plate, start_time, total_kwh, odometer, cost_total, is_external, operator) VALUES "
        "('X-1', '2026-06-09 13:09', 26.82, 20800, 21.06, 1, 'opA'),"
        "('X-1', '2026-06-11 14:23', 55.10, 21300, 45.50, 1, 'opB'),"
        "('X-1', '2026-06-14 17:22', 21.49, 21900, 16.89, 1, 'opC'),"
        "('X-1', '2026-06-17 16:57', 15.54, 22300, 14.05, 1, 'opD')"
    )
    db.commit()

    vid = db.execute("SELECT id FROM vehicles WHERE plate='X-1'").fetchone()[0]
    c = app_module.app.test_client()
    c.post("/login", data={"username": "admin", "password": "Sup3r!secret"})
    c.post("/api/user/active-vehicle", json={"vehicle_id": vid})
    return c


def _charts(html):
    def grab(name):
        return json.loads(re.search(rf"const {name} = (\[.*?\]);", html).group(1))
    return (dict(zip(grab("labels"), grab("dataCons"))),
            dict(zip(grab("labels"), grab("dataCost"))),
            dict(zip(grab("labelsExt"), grab("dataCostExt"))))


def test_cost_per_100km_incl_external_uses_odometer_distance(app_module, db):
    c = _setup(app_module, db)
    r = c.get("/charges/print?year=2026&month=06&stats=1&ext_stats=1")
    assert r.status_code == 200
    cons, cost, cost_ext = _charts(r.get_data(as_text=True))

    # Home charts: ONLY the home-to-home interval counts — every km driven on
    # external energy is filtered out (external charges break the chain).
    assert cons["06/2026"] == round(100.0 / 500 * 100, 2)      # 20.0 kWh/100km
    assert cost["06/2026"] == round(34.00 / 500 * 100, 2)      # 6.8 €/100km

    # Incl. external = same walk + ext segments. The first June session opens
    # the chain (its fill covers unknown km), the walk then spans the full
    # 20000 -> 22566 = 2566 km including the road trip.
    walk_cost = 34.00 * 2 + (21.06 + 45.50 + 16.89 + 14.05)
    walk_km = 22566 - 20000
    june = cost_ext["06/2026"]
    assert june == round(walk_cost / walk_km * 100, 2)          # = 6.45 €/100 km

    # Regression guards for the two denominators that were wrong before:
    #   home-to-home interval km + FULL ext cost -> ~26-61 €/100 km
    #   SUM(trips.distance_km) (462 km)          -> ~36 €/100 km
    trip_km = 200 + 262
    assert june != round(walk_cost / trip_km * 100, 2)
    assert june < 15


def test_cost_per_100km_matches_home_chart_in_ext_free_month(app_module, db):
    """A month with wallbox charges only must show the SAME €/100km bar on the
    'incl. external charges' page as on the home statistics page — the home
    interval model is the reference, the ext chart adapts to it."""
    c = _setup(app_module, db)
    db.execute(
        "INSERT INTO charge_sessions "
        "(vehicle_plate, start_time, total_kwh, meter_start, meter_end, odometer, cost_pauschale, is_external) VALUES "
        "('X-1', '2026-07-03 11:45', 50.0, 5300.0, 5350.0, 22800, 17.00, 0),"
        "('X-1', '2026-07-21 11:45', 50.0, 5350.0, 5400.0, 23200, 17.00, 0)"
    )
    db.commit()

    r = c.get("/charges/print?year=2026&month=07&stats=1&ext_stats=1")
    assert r.status_code == 200
    cons, cost, cost_ext = _charts(r.get_data(as_text=True))

    # Home walk July: 22566 -> 22800 -> 23200 = 634 km, 100 kWh, 34 €.
    interval_km = 23200 - 22566
    assert cons["07/2026"] == round(100.0 / interval_km * 100, 2)   # 15.77
    assert cost["07/2026"] == round(34.00 / interval_km * 100, 2)   # 5.36
    assert cost_ext["07/2026"] == cost["07/2026"]   # no ext -> identical bars


def test_boundary_ext_charge_does_not_leak_into_ext_free_month(app_module, db):
    """The road trip ends with external charges on June 28; July charges only
    at home. The ext->home segment crossing the month boundary must book into
    JUNE (the ext side) — otherwise July shows different bars despite zero
    external sessions. A restarted charge at the same stop (d = 0) keeps its
    receipt in the pot without adding km."""
    c = _setup(app_module, db)
    db.execute(
        "INSERT INTO charge_sessions "
        "(vehicle_plate, start_time, total_kwh, odometer, cost_total, is_external, operator) VALUES "
        "('X-1', '2026-06-28 19:00', 30.0, 23000, 24.00, 1, 'opE'),"
        "('X-1', '2026-06-28 20:30',  8.0, 23000,  6.00, 1, 'opE')"
    )
    db.execute(
        "INSERT INTO charge_sessions "
        "(vehicle_plate, start_time, total_kwh, meter_start, meter_end, odometer, cost_pauschale, is_external) VALUES "
        "('X-1', '2026-07-02 11:45', 50.0, 5300.0, 5350.0, 23400, 17.00, 0),"
        "('X-1', '2026-07-21 11:45', 50.0, 5350.0, 5400.0, 23800, 17.00, 0)"
    )
    db.commit()

    r = c.get("/charges/print?year=2026&month=07&stats=1&ext_stats=1")
    assert r.status_code == 200
    cons, cost, cost_ext = _charts(r.get_data(as_text=True))

    # July home walk: only 23400 -> 23800 (the segment from 23000 has an ext side).
    assert cost["07/2026"] == round(17.00 / 400 * 100, 2)           # 4.25
    assert cost_ext["07/2026"] == cost["07/2026"]                   # invariant

    # June absorbs the trip tail: km 22566->23000->23400 plus the d=0 receipt.
    june_km = (22566 - 20000) + (23000 - 22566) + (23400 - 23000)
    june_cost = 34.00 * 2 + (21.06 + 45.50 + 16.89 + 14.05) + 24.00 + 6.00 + 17.00
    assert cost_ext["06/2026"] == round(june_cost / june_km * 100, 2)
