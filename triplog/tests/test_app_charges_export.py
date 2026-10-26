"""CSV export of the charge overview: the list's filters (vehicle, date range,
external charges on/off), the list's numbers (flat-rate valuation, Wh/km),
German Excel conventions (semicolon, decimal comma, BOM)."""

import pytest


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'dev1')")
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (2, 'EV2', 'dev2')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) "
               "VALUES (1, 'admin', 'x', 1, 1)")
    db.execute("INSERT INTO charge_tariffs (valid_from, pauschale_kwh) VALUES ('2020-01-01', 0.30)")
    rows = [  # plate, start, end, ext, kwh, cost_tibber, cost_pauschale, cost_total, odo, loc, note
        ('EV1', '2026-07-03T18:00:00', '2026-07-03T21:30:00', 0, 30.5, 8.10, 9.15, None, 12000, 'Garage', 'Wallbox'),
        ('EV1', '2026-07-10T12:00:00', '2026-07-10T12:40:00', 1, 44.0, None, None, 30.80, 12500, 'Ionity A7', 'unterwegs'),
        ('EV2', '2026-07-11T12:00:00', '2026-07-11T13:00:00', 0, 20.0, 5.0, 6.0, None, 500, 'Garage', 'anderes Auto'),
        ('EV1', '2026-06-20T12:00:00', '2026-06-20T13:00:00', 0, 10.0, 3.0, 3.0, None, 11000, 'Garage', 'zu alt'),
    ]
    for r in rows:
        db.execute("INSERT INTO charge_sessions (vehicle_plate, start_time, end_time, is_external, total_kwh, "
                   "cost_tibber, cost_pauschale, cost_total, odometer, location_name, note) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", r)
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def _rows(resp):
    body = resp.get_data()
    assert body.startswith(b"\xef\xbb\xbf")           # BOM for Excel
    lines = body[3:].decode("utf-8").strip().splitlines()
    return [l.split(";") for l in lines]


def test_export_follows_vehicle_and_period(client):
    r = client.get("/charges/export/csv?vehicle=EV1&from=2026-07-01&to=2026-07-31")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert "ladelog_EV1_2026-07-01_2026-07-31.csv" in r.headers["Content-Disposition"]
    head, *rows = _rows(r)
    assert head[:4] == ["Nr", "Datum", "Start", "Ende"] and "Pauschale (EUR)" in head
    assert [x[1] for x in rows] == ["2026-07-10", "2026-07-03"]        # newest first, EV2 and June out
    wallbox = dict(zip(head, rows[1]))
    assert wallbox["kWh"] == "30,50" and wallbox["Dynamisch (EUR)"] == "8,10"
    assert wallbox["Pauschale (EUR)"] == "9,15" and wallbox["Art"] == "Wallbox" and wallbox["Notiz"] == "Wallbox"
    assert wallbox["Ø ct/kWh"] == "26,6"                                # 8.10 / 30.5 * 100
    ext = dict(zip(head, rows[0]))
    assert ext["Art"] == "Extern" and ext["Extern gesamt (EUR)"] == "30,80"
    assert ext["Dynamisch (EUR)"] == "" and ext["Pauschale (EUR)"] == "" and ext["Ort"] == "Ionity A7"


def test_export_can_leave_external_charges_out(client):
    head, *rows = _rows(client.get("/charges/export/csv?vehicle=EV1&from=2026-07-01&to=2026-07-31&ext=0"))
    assert [dict(zip(head, r))["Art"] for r in rows] == ["Wallbox"]


def test_export_defaults_to_the_active_vehicle(client):
    head, *rows = _rows(client.get("/charges/export/csv?from=2026-06-01&to=2026-07-31"))
    assert {dict(zip(head, r))["Fahrzeug"] for r in rows} == {"EV1"}
    assert len(rows) == 3


def test_list_carries_the_export_button(client):
    html = client.get("/charges?vehicle=EV1&from=2026-07-01&to=2026-07-31").get_data(as_text=True)
    assert 'data-onclick="exportCsv"' in html and "/charges/export/csv" in html
