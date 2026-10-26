"""The charges list shows the vehicle picked in the header. The print page's
"back to overview" link used to carry ?vehicle=<plate>, which outranks the
header pick: after printing, switching the vehicle kept showing the old one."""

import re

import pytest


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO vehicles (id, plate, name, device) VALUES (1, 'NOX1', 'Nox', '')")
    db.execute("INSERT INTO vehicles (id, plate, name, device) VALUES (2, 'SIR1', 'Sirius', '')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) VALUES (1, 'admin', 'x', 1, 1)")
    meter = {"NOX1": 1000.0, "SIR1": 5000.0}
    for tag, plate in ((1, "NOX1"), (3, "SIR1"), (5, "NOX1"), (7, "SIR1")):
        for h in ("10:00", "10:15", "10:30"):
            db.execute("INSERT INTO charge_readings (timestamp, vehicle_plate, meter_start, meter_end, kwh) "
                       "VALUES (?, ?, ?, ?, 2.0)", (f"2026-09-{tag:02d}T{h}:00", plate, meter[plate], meter[plate] + 2.0))
            meter[plate] += 2.0
    db.commit()
    app_module.rebuild_charge_sessions(db)
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def _plates(client, db, url):
    html = client.get(url).get_data(as_text=True)
    ids = re.findall(r'<tr data-id="(\d+)"', html)
    return sorted({db.execute("SELECT vehicle_plate FROM charge_sessions WHERE id = ?", (int(i),)).fetchone()[0] for i in ids})


def test_list_follows_the_header_vehicle(client, db):
    assert _plates(client, db, "/charges?from=2026-09-01&to=2026-09-30") == ["NOX1"]
    client.post("/api/user/active-vehicle", json={"vehicle_id": 2})
    assert _plates(client, db, "/charges?from=2026-09-01&to=2026-09-30") == ["SIR1"]


def test_print_back_link_does_not_pin_the_vehicle(client, db):
    html = client.get("/charges/print?month=9&year=2026").get_data(as_text=True)
    assert 'class="btn-back" href="/charges"' in html
    assert "?vehicle=" not in html


def test_switching_the_vehicle_drops_a_pinned_vehicle_from_the_address(client):
    html = client.get("/charges").get_data(as_text=True)
    assert "searchParams.delete('vehicle')" in html
