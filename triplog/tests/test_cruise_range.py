"""Range at motorway speed (1.2.280): consumption fitted on the own trips as
a + b·(v/100)² + c·cold, evaluated at 130 km/h and today's temperature."""

import bms_learn as bl


def _trips(a=16.0, b=5.0, c=0.3, n=60):
    out = []
    for i in range(n):
        v = 30 + (i * 7) % 95                    # 30 … 124 km/h
        t = -5 + (i * 3) % 30                    # -5 … 24 °C
        cons = a + b * (v / 100) ** 2 + c * max(0, bl.COLD_C - t)
        out.append({"km": 40, "cons": round(cons, 2), "v": v, "temp": t})
    return out


def test_fit_recovers_terms():
    m = bl.speed_model(_trips())
    assert abs(m["a"] - 16.0) < 0.2 and abs(m["b"] - 5.0) < 0.3 and abs(m["c"] - 0.3) < 0.03


def test_cruise_range_cold_costs_range():
    m = bl.speed_model(_trips())
    warm = bl.cruise_range(50.0, m, 20)
    cold = bl.cruise_range(50.0, m, -5)
    assert warm["v"] == 130 and cold["km"] < warm["km"]
    # 16 + 5·1.69 = 24.45 kWh/100 km → 50 kWh ≈ 204 km
    assert abs(warm["cons"] - 24.4) < 0.3 and abs(warm["km"] - 204) <= 3


def test_too_few_or_slow_trips_give_nothing():
    assert bl.speed_model(_trips(n=10)) is None
    slow = [dict(t, v=min(t["v"], 70)) for t in _trips()]
    assert bl.speed_model(slow) is None


def test_without_temperatures_still_fits():
    m = bl.speed_model([dict(t, temp=None) for t in _trips(c=0.0)])
    assert m and m["c"] == 0.0 and abs(m["b"] - 5.0) < 0.3


def test_outliers_are_dropped():
    tr = _trips(c=0.0) + [{"km": 40, "cons": 55.0, "v": 40, "temp": 10}] * 3
    m = bl.speed_model(tr)
    assert abs(m["b"] - 5.0) < 0.5
