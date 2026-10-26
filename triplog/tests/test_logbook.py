"""Monthly logbook: business trips one per line with places and purpose,
private trips in a row folded into one line (dates, odometer, km — no
places), a trip without purpose marked, odometer gaps shown; the PDF is
rendered on the server."""

import pytest

import fahrtenbuch

pytest.importorskip("fpdf")


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO vehicles (id, plate, name, device) VALUES (1, 'B-EV 1', 'Nox', 'dev1')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) VALUES (1, 'admin', 'x', 1, 1)")
    db.execute("INSERT OR REPLACE INTO purpose_meta (name, is_private) VALUES ('Privat', 1)")
    db.execute("INSERT OR REPLACE INTO purpose_meta (name, is_private) VALUES ('Kunde', 0)")
    fahrten = [
        # start, end, odo_start, odo_end, purpose, from, to, partner, occasion
        ("2026-09-01T08:00:00", "2026-09-01T09:00:00", 1000, 1050, "Kunde", "DE - 12345 Berlin", "DE - 20095 Hamburg", "Firma Łódź GmbH", "Wartung"),
        ("2026-09-02T18:00:00", "2026-09-02T18:30:00", 1050, 1060, "Privat", "Hamburg", "Supermarkt", "", ""),
        ("2026-09-03T10:00:00", "2026-09-03T10:20:00", 1060, 1070, "Privat", "Supermarkt", "Zuhause", "", ""),
        ("2026-09-05T07:00:00", "2026-09-05T08:00:00", 1082, 1120, "Kunde", "Hamburg", "Kiel", "Werft", "Besprechung"),
        ("2026-09-06T07:00:00", "2026-09-06T07:30:00", 1120, 1130, None, "Kiel", "Neumünster", "", ""),
        ("2026-10-01T07:00:00", "2026-10-01T07:30:00", 1130, 1140, "Kunde", "nächster Monat", "x", "", ""),
    ]
    for s, e, a, b, zweck, von, nach, partner, anlass in fahrten:
        db.execute("INSERT INTO trips (device, start_time, end_time, odo_start, odo_end, distance_km, purpose, "
                   "start_address, end_address, destination, visit_reason) VALUES ('dev1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (s, e, a, b, b - a, zweck, von, nach, partner, anlass))
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def test_month_rows_fold_private_trips_and_show_gaps(client, db):
    d = fahrtenbuch.monat_daten(db, "dev1", 2026, 9)
    arten = [r["art"] for r in d["rows"]]
    assert arten == ["fahrt", "privat", "luecke", "fahrt", "offen"]
    privat = d["rows"][1]
    assert (privat["anzahl"], privat["odo_start"], privat["odo_end"], privat["km"]) == (2, 1050, 1070, 20)
    assert "start" not in privat                                  # no places for private trips
    assert d["rows"][2]["km"] == 12                               # 1070 -> 1082
    s = d["summen"]
    assert (s["gesamt"], s["privat"], s["offen"], s["luecken"]) == (118, 20, 10, 12)
    assert s["zwecke"] == {"Kunde": 88} and s["dienstlich"] == 88
    assert (s["odo_anfang"], s["odo_ende"], s["fahrten"]) == (1000, 1130, 5)
    assert fahrtenbuch.tage_text(privat["von"], privat["bis"]) == "02.–03.09.2026"


def test_print_page_shows_the_month_and_offers_the_pdf(client):
    html = client.get("/trips/logbook?year=2026&month=9").get_data(as_text=True)
    assert "Fahrtenbuch" in html and "September 2026" in html and "B-EV 1" in html
    assert "Privatfahrten (2)" in html and "Lücke im km-Stand: 12,0 km" in html
    assert "Firma Łódź GmbH · Wartung" in html and "ohne Zweck" in html
    assert "1 Fahrt(en) ohne Zweck" in html
    assert 'href="/trips/logbook.pdf?year=2026&month=9"' in html
    assert "nächster Monat" not in html
    assert "/trips/logbook" in client.get("/trips").get_data(as_text=True)


def test_pdf_is_rendered_on_the_server(client):
    res = client.get("/trips/logbook.pdf?year=2026&month=9")
    assert res.status_code == 200 and res.mimetype == "application/pdf"
    assert res.data[:5] == b"%PDF-"
    assert "attachment; filename=Fahrtenbuch_B-EV_1_2026-09.pdf" == res.headers["Content-Disposition"]
    assert b"/Type /Page" in res.data


def test_default_month_is_the_previous_one(client, app_module):
    from blueprints import trips
    with app_module.app.test_request_context("/trips/logbook"):
        jahr, monat = trips._logbuch_monat()
    from datetime import datetime
    heute = datetime.now()
    assert (jahr, monat) == ((heute.year, heute.month - 1) if heute.month > 1 else (heute.year - 1, 12))
