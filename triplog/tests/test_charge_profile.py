"""Charging power profile over all charges (analysis tab, Laden): per 5-% SoC
step and per time step since the start of the charge; each charge averaged
per step first, then mean/std/min/max across the charges."""

from datetime import timedelta

import pytest

import charge_profile as cp
from factory import at, reading


def _charge(t0, minutes, soc0, soc1, p0, p1, bt, dc=1, every_s=15):
    rows = []
    n = int(minutes * 60 / every_s)
    for k in range(n):
        f = k / max(n - 1, 1)
        p = p0 + (p1 - p0) * f
        rows.append(reading(at(t0, every_s * k), v=0, s=round(soc0 + (soc1 - soc0) * f, 1),
                            p=-p, i=-p * 1000 / 400, u=400.0, bt=bt, c=1, dc=dc))
    return rows


def _drive(t0, minutes):
    return [reading(at(t0, 30 * k), v=80, p=15.0, s=50, c=0, dc=0) for k in range(minutes * 2)]


def test_split_sessions_and_type():
    rows = _charge(0, 20, 20, 60, 120, 60, 30) + _drive(25, 30) + _charge(60, 60, 30, 50, 11, 11, 20, dc=0, every_s=60)
    sess = cp.split_sessions(rows)
    assert len(sess) == 2
    assert [cp.session_type(s) for s in sess] == ["dc", "ac"]


def test_negative_power_while_standing_counts_without_flag():
    rows = [reading(at(0, 15 * k), v=0, p=-90.0, s=40 + k, c=None, dc=None) for k in range(5)]
    sess = cp.split_sessions(rows)
    assert len(sess) == 1 and cp.session_type(sess[0]) == "dc"


def test_gap_splits_a_charge():
    rows = _charge(0, 5, 20, 30, 100, 100, 25) + _charge(30, 5, 30, 40, 100, 100, 25)
    assert len(cp.split_sessions(rows)) == 2


def test_profile_mean_min_max_across_charges():
    warm = _charge(0, 20, 20, 60, 120, 120, 30)       # 120 kW all the way
    cold = _charge(100, 20, 20, 60, 40, 40, 5)        # cold battery: 40 kW
    prof = cp.build_profile(warm + _drive(50, 10) + cold, "dc")
    assert prof["sessions"] == 2 and prof["counts"] == {"dc": 2, "ac": 0}
    b30 = next(it for it in prof["by_soc"] if it["x"] == 30)
    assert b30["n"] == 2
    assert b30["p"]["mean"] == 80.0 and b30["p"]["min"] == 40.0 and b30["p"]["max"] == 120.0
    assert b30["p"]["std"] == 40.0
    assert b30["bt"]["min"] == 5.0 and b30["bt"]["max"] == 30.0
    assert b30["i"]["max"] == 300.0                   # current positive
    t0 = prof["by_time"][0]
    assert t0["x"] == 0 and prof["time_step_min"] == 2 and t0["n"] == 2
    assert [it["x"] for it in prof["by_time"]][:3] == [0, 2, 4]


def test_long_charge_does_not_outweigh_short_one():
    long_ = _charge(0, 60, 40, 45, 50, 50, 20, every_s=5)     # many rows in 40-45 %
    short = _charge(200, 2, 40, 44, 150, 150, 30)
    b40 = next(it for it in cp.build_profile(long_ + _drive(100, 5) + short, "dc")["by_soc"] if it["x"] == 40)
    assert b40["p"]["mean"] == 100.0


def test_ac_profile_only_ac():
    rows = _charge(0, 20, 20, 60, 120, 60, 30) + _drive(25, 30) + _charge(60, 120, 30, 50, 11, 11, 20, dc=0, every_s=60)
    prof = cp.build_profile(rows, "ac")
    assert prof["sessions"] == 1 and prof["time_step_min"] == 10
    assert all(it["p"]["max"] <= 11 for it in prof["by_soc"])


def test_merge_windows_pads_clips_and_merges():
    a, b = at(0), at(20)
    c = at(25)
    ws = cp.merge_windows([(a, b), (c, at(40)), (at(0), at(0) + timedelta(days=3)), (None, None)])
    assert len(ws) == 1
    assert ws[0][0] == a - cp.WINDOW_PAD
    assert ws[0][1] == a + cp.WINDOW_MAX + cp.WINDOW_PAD


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin) VALUES (1, 'admin', 'x', 1)")
    db.execute("INSERT INTO vehicles (plate, device) VALUES ('EV1', 'dev1')")
    db.execute("INSERT INTO charges (device, start_time, end_time, type) VALUES ('dev1', '2026-11-19T16:26:00', '2026-11-19T16:42:00', 'dc')")
    db.commit()
    app_module.app.config["TESTING"] = True
    cp._cache.clear()
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def test_route_uses_charge_windows_and_returns_profile(client, monkeypatch):
    seen = {}

    def fake_fetch(device, windows, client=None):
        seen["device"], seen["windows"] = device, windows
        return _charge(0, 20, 20, 60, 120, 60, 30)
    monkeypatch.setattr(cp, "fetch_rows", fake_fetch)
    r = client.get("/api/charge/power-profile?type=dc")
    assert r.status_code == 200
    d = r.get_json()
    assert d["sessions"] == 1 and d["by_soc"] and d["by_time"]
    assert seen["device"] == "dev1" and len(seen["windows"]) == 1
    # second call comes from the cache
    seen.clear()
    assert client.get("/api/charge/power-profile?type=dc").get_json()["sessions"] == 1
    assert seen == {}


def test_analysis_page_has_profile_container(client):
    r = client.get("/analysis")
    assert r.status_code == 200
    assert b'id="chartsChargeProfile"' in r.data and b"/api/charge/power-profile" in r.data
