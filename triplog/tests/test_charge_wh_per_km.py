"""Per-row Wh/km must come from the METERED energy, never from soc_end.

Real case (July, session 203): 33.78 kWh metered, SoC 43.8 -> 75.2 %, then 57 km
driven. The old soc_end-based formula read the drop 75.2 -> 69.9 (next charge)
as "5 % over 57 km" and printed 80 Wh/km — physically impossible. soc_end is
unreliable because the telemetry falls asleep before the charge completes (the
implied charging efficiency of that session was 77 %, of another one 52 %).

The energy balance between two consecutive charge STARTS uses only soc_start:
    consumption = battery_kwh(this charge) - (soc_start(next) - soc_start(this)) * cap
which restores 151 Wh/km for the same row.
"""
import pytest


CAP = 83.6          # battery capacity anchored below
EFF = 0.90          # charge efficiency (grid -> battery)


def _mk(db, app_module, rows):
    db.execute("INSERT INTO vehicles (name, plate, device, battery_capacity_kwh)"
               " VALUES ('Nox','X-1','devX',?)", (CAP,))
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('charge_grid_efficiency', ?)",
               (str(EFF),))
    for i, (start, kwh, soc_s, soc_e, odo) in enumerate(rows):
        # `distance` is the stored odo delta to the NEXT charge (set by rebuild).
        dist = (rows[i + 1][4] - odo) if i + 1 < len(rows) else None
        db.execute(
            "INSERT INTO charge_sessions (vehicle_plate, start_time, total_kwh, soc_start,"
            " soc_end, odometer, distance, is_external) VALUES ('X-1',?,?,?,?,?,?,0)",
            (start, kwh, soc_s, soc_e, odo, dist))
    db.commit()


def test_metered_energy_survives_a_broken_soc_end(app_module, db):
    # 203: 33.78 kWh, soc_start 43.8 -> next charge starts at 69.9, 57 km driven.
    # soc_end is deliberately understated (75.2) the way the real telemetry does.
    _mk(db, app_module, [
        ("2026-07-04 10:15", 33.78, 43.8, 75.2, 23900),
        ("2026-07-05 12:45", 19.94, 69.9, 90.7, 23957),
        ("2026-07-06 12:45", 25.88, 53.0, 81.0, 24158),
    ])
    real = app_module._real_consumption(db, "X-1")
    assert real is not None
    sid = db.execute("SELECT id FROM charge_sessions ORDER BY start_time LIMIT 1").fetchone()[0]

    # Assert the MODEL, not hard-coded constants: read efficiency/capacity back
    # from the same helpers the implementation uses.
    eff = app_module._get_charge_efficiency(db)
    cap = app_module.get_bat_kwh(db, "devX", at_time="2026-07-04 10:15")
    expected = round((33.78 * eff - (69.9 - 43.8) / 100.0 * cap) * 1000.0 / 57)

    assert real["per_row"][sid] == expected
    # Plausible consumption — decisively not the old soc_end-based 80 Wh/km.
    assert 120 <= real["per_row"][sid] <= 200


def test_window_average_ignores_soc_end_garbage(app_module, db):
    # soc_end is nonsense everywhere (41 after charging 30 kWh from SoC 40!) —
    # the window Ø must come purely from metered kWh + boundary soc_start.
    _mk(db, app_module, [
        ("2026-07-01 10:00", 30.0, 40.0, 41.0, 10000),
        ("2026-07-03 10:00", 25.0, 50.0, 99.0, 10150),
        ("2026-07-05 10:00", 20.0, 45.0, 45.5, 10290),
    ])
    real = app_module._real_consumption(db, "X-1")
    eff = app_module._get_charge_efficiency(db)
    cap = app_module.get_bat_kwh(db, "devX", at_time="2026-07-03 10:00")

    batt_in = (30.0 + 25.0) * eff                       # calibrated meter values
    delta = (45.0 - 40.0) / 100.0 * cap                 # boundary soc_start only
    expected = round((batt_in - delta) / 290 * 100.0, 1)
    assert real["cons_batt"] == expected


def test_run_breaks_at_charge_with_unknowable_energy(app_module, db):
    # The 2nd charge has neither kWh nor SoC — its delivered energy is
    # unknowable, so the chain must split there. The first run then lacks a
    # usable right anchor (no soc_start) and is dropped entirely; only the
    # clean run after the hole may enter the average.
    _mk(db, app_module, [
        ("2026-07-01 10:00", 30.0, 40.0, 80.0, 10000),
        ("2026-07-02 10:00", None, None, None, 10150),
        ("2026-07-03 10:00", 25.0, 50.0, 80.0, 10300),
        ("2026-07-05 10:00", 20.0, 45.0, 80.0, 10450),
    ])
    real = app_module._real_consumption(db, "X-1")
    eff = app_module._get_charge_efficiency(db)
    cap = app_module.get_bat_kwh(db, "devX", at_time="2026-07-03 10:00")

    expected = round((25.0 * eff - (45.0 - 50.0) / 100.0 * cap) / 150 * 100.0, 1)
    assert real["cons_batt"] == expected
    assert real["km"] == 150            # the broken stretch is not counted


def test_implausible_result_is_suppressed(app_module, db):
    # A charge that delivers almost nothing over a long drive -> broken inputs.
    _mk(db, app_module, [
        ("2026-07-04 10:15", 1.0, 50.0, 51.0, 23900),
        ("2026-07-05 12:45", 19.94, 49.5, 90.7, 24200),
    ])
    real = app_module._real_consumption(db, "X-1")
    sid = db.execute("SELECT id FROM charge_sessions ORDER BY start_time LIMIT 1").fetchone()[0]
    assert real is None or sid not in (real["per_row"] or {})
