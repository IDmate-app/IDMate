"""Charging curve on the charge detail page: the stick writes rows while the
car charges (DC every 15 s, AC every minute, firmware 1.7.6). The server shows
power, SoC and battery temperature from those rows; the trip detector still
splits the trip at the charging stop."""

from datetime import timedelta

import pytest

import detector
from factory import at, reading


def _drive(t0, minutes, soc0, soc1, od0):
    rows = []
    n = minutes * 2
    for i in range(n + 1):
        f = i / n
        rows.append(reading(at(t0, 30 * i), v=80, s=round(soc0 + (soc1 - soc0) * f, 1),
                            od=round(od0 + 1.3 * minutes * f, 1), la=51.0 + 0.2 * f, lo=12.0,
                            c=0, dc=0))
    return rows


def _fast_charge(t0, minutes, soc0, soc1):
    """A row every 15 s while fast charging, standing still, power negative."""
    rows = []
    n = minutes * 4
    for i in range(1, n + 1):
        f = i / n
        rows.append(reading(at(t0, 15 * i), v=0, s=round(soc0 + (soc1 - soc0) * f, 1),
                            p=-110.0 + 40 * f, bt=25 + 5 * f, c=1, dc=1, la=51.2, lo=12.0))
    return rows


def test_dense_charge_rows_split_the_trip():
    rows = _drive(0, 20, 60, 30, 1000) + _fast_charge(20, 17, 30, 70) + _drive(38, 20, 70, 50, 1026)
    trips = detector.detect_trips(rows, bat_kwh=77.0)
    assert len(trips) == 2
    assert trips[0]["soc_end"] <= 31 and trips[1]["soc_start"] >= 69


def test_dense_charge_rows_make_one_dc_charge():
    rows = _drive(0, 20, 60, 30, 1000) + _fast_charge(20, 17, 30, 70) + _drive(38, 20, 70, 50, 1026)
    charges = detector.detect_charges(rows)
    assert len(charges) == 1
    ch = charges[0]
    assert ch["type"] == "dc" and ch["max_power_kw"] >= 100
    # ~17 min at 70..110 kW -> roughly 25 kWh
    assert 20 < ch["energy_kwh"] < 32


def test_curve_points_standing_rows_charging_power_positive_forward_filled(app_module):
    from blueprints import charges as ch_bp
    charge = _fast_charge(3, 2, 39, 45)
    # the stick leaves unchanged values out: drop bt from every second row
    for i, r in enumerate(charge):
        if i % 2:
            r["bt"] = None
    # first row after waking: no charge flag yet, but standing -> shown too
    wake = [reading(at(2, 50), v=0, s=39, p=-5.0, bt=25)]
    rows = _drive(0, 2, 40, 39, 1000) + wake + charge
    pts = ch_bp.charge_curve_points(rows)
    assert len(pts) == 9                      # wake row + 2 min x 4 rows, no driving rows
    assert pts[0]["p"] == 5.0 and all(p["p"] > 0 for p in pts)
    assert all(p["bt"] is not None for p in pts)
    assert pts[-1]["s"] == 45


def test_standing_car_drawing_power_is_negative(app_module):
    from blueprints import charges as ch_bp
    pts = ch_bp.charge_curve_points([reading(at(0, 15 * k), v=0, s=50, p=3.0) for k in range(4)])
    assert all(p["p"] == -3.0 for p in pts)


def test_curve_points_downsampled(app_module):
    from blueprints import charges as ch_bp
    pts = ch_bp.charge_curve_points(_fast_charge(0, 200, 10, 90), max_points=100)
    assert len(pts) == 100


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin) "
               "VALUES (1, 'admin', 'x', 1)")
    db.execute("INSERT INTO vehicles (plate, device) VALUES ('EV1', 'dev1')")
    db.execute("INSERT INTO charge_sessions (id, vehicle_plate, start_time, end_time, is_external) "
               "VALUES (7, 'EV1', '2026-11-19T16:13:00', '2026-11-19T16:52:00', 1)")
    db.commit()
    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def test_curve_route_queries_the_vehicle_device(client, monkeypatch):
    from blueprints import charges as ch_bp
    seen = {}

    def fake(device, start, end, fixed=False):
        seen.update(device=device, start=start, end=end)
        return [{"t": "2026-11-19T16:30:00", "p": 105.0, "s": 60.0, "bt": 28.0}]
    monkeypatch.setattr(ch_bp, "_influx_charge_curve", fake)
    r = client.get("/api/charge/sessions/7/curve")
    assert r.status_code == 200
    assert r.get_json()["points"][0]["p"] == 105.0
    assert seen == {"device": "dev1", "start": "2026-11-19T16:13:00", "end": "2026-11-19T16:52:00"}
    assert r.get_json()["start"] == "2026-11-19T16:13:00" and r.get_json()["end"] == "2026-11-19T16:52:00"


def test_curve_route_unknown_session(client):
    assert client.get("/api/charge/sessions/999/curve").status_code == 404


def test_charge_detail_page_has_curve_card(client, monkeypatch):
    r = client.get("/charges/7")
    assert r.status_code == 200
    assert b'id="curveCard"' in r.data and b"/api/charge/sessions/7/curve" in r.data


def test_driving_after_the_charge_never_counts(app_module):
    """Bug 19.11.2026: the stick leaves unchanged values out; the charge flag
    was carried into the drive after the stop, so the chart showed driving."""
    from blueprints import charges as ch_bp
    rows = _fast_charge(0, 2, 60, 64)
    rows += [reading(at(3, 30 * k), v=70, s=64, p=18.0) for k in range(10)]   # no c/dc in these rows
    pts = ch_bp.charge_curve_points(rows)
    assert len(pts) == 8 and all(p["p"] > 50 for p in pts)


def test_curve_clips_strictly_to_given_times(app_module):
    from blueprints import charges as ch_bp
    rows = _fast_charge(0, 10, 30, 60)
    pts = ch_bp.charge_curve_points(rows, start=at(2), end=at(4))
    assert pts and all("T" in p["t"] for p in pts)
    assert len(pts) == 9                       # 2 min x 4 rows + the row at 4:00


def test_refine_window_moves_only_seen_edges():
    import charge_profile as cp
    rows = _drive(0, 10, 50, 40, 1000)[:-1] + _fast_charge(15, 10, 40, 60) + _drive(40, 10, 60, 50, 1013)
    s, e = at(10), at(40)
    # stick saw the whole charge: SoC 40 -> 60 like before/after the stop
    ns, ne = cp.refine_window(rows, s, e, 40, 60)
    assert ns == at(15, 15) and ne == at(15, 15 * 40)
    # charge began while the stick slept (SoC before 30) and went on after (70): edges stay
    assert cp.refine_window(rows, s, e, 30, 70) == (s, e)
    # nothing seen -> None
    assert cp.refine_window(_drive(0, 10, 50, 40, 1000), s, e, 40, 60) is None


def test_refine_external_times_updates_auto_session_once(app_module, db, monkeypatch):
    import charge_profile as cp
    from blueprints import charges as ch_bp
    db.execute("INSERT INTO vehicles (plate, device) VALUES ('EV1', 'dev1')")
    db.execute("INSERT INTO charge_sessions (id, vehicle_plate, start_time, end_time, is_external, total_kwh, soc_start, soc_end) "
               "VALUES (5, 'EV1', datetime('now','localtime','-3 hours'), datetime('now','localtime','-2 hours'), 1, 20, 40, 60)")
    db.commit()
    sess = db.execute("SELECT start_time, end_time FROM charge_sessions WHERE id = 5").fetchone()
    s, e = cp._local(sess["start_time"].replace(" ", "T")), cp._local(sess["end_time"].replace(" ", "T"))
    calls = []

    def fake_fetch(device, windows, client=None):
        calls.append(windows)
        t0 = s + timedelta(minutes=10)
        return [reading(t0 + timedelta(seconds=15 * k), v=0, s=40 + k * 0.1, p=-100.0, c=1, dc=1) for k in range(200)]
    monkeypatch.setattr(cp, "fetch_rows", fake_fetch)
    assert ch_bp.refine_external_times(db, "EV1", "dev1") == 1
    r = db.execute("SELECT start_time, end_time, duration_minutes, manual_fields FROM charge_sessions WHERE id = 5").fetchone()
    assert r["manual_fields"] == "refined" and r["duration_minutes"] == 49
    assert ch_bp.refine_external_times(db, "EV1", "dev1") == 0 and len(calls) == 1


def test_times_from_the_invoice_are_fixed(app_module, client, db):
    r = client.post("/api/charge/sessions/7", json={"start_time": "2026-11-19T16:15:00", "end_time": "2026-11-19T16:50:00"})
    assert r.status_code == 200
    mf = db.execute("SELECT manual_fields FROM charge_sessions WHERE id = 7").fetchone()["manual_fields"]
    assert "start_time" in mf and "end_time" in mf
