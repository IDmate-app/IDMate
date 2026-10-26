"""Das Auto auf der Karte (1.2.147-dev): im Stand statt des blauen Punkts
eine Draufsicht in Karosserie und Wagenfarbe aus den Fahrzeug-Einstellungen.
Abgestimmt in der Review-Palette: Ansatz B (Draufsicht mit Scheiben),
Elektro-Proportionen, kleine Größe, Farbwähler. Ohne Einstellung bleibt
alles wie bisher."""

import pytest


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'dev1')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) "
               "VALUES (1, 'admin', 'x', 1, 1)")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def test_migration_adds_body_style_and_color(db):
    cols = {r["name"] for r in db.execute("PRAGMA table_info(vehicles)").fetchall()}
    assert {"body_style", "color"} <= cols


def test_vehicle_update_keeps_only_known_bodies_and_hex_colours(client, db):
    assert client.post("/api/vehicles/1", json={"body_style": "SUV", "color": "1A1C1F"}).get_json()["ok"]
    row = db.execute("SELECT body_style, color FROM vehicles WHERE id = 1").fetchone()
    assert (row["body_style"], row["color"]) == ("suv", "#1a1c1f")
    client.post("/api/vehicles/1", json={"body_style": "bus", "color": "rot"})
    row = db.execute("SELECT body_style, color FROM vehicles WHERE id = 1").fetchone()
    assert (row["body_style"], row["color"]) == (None, None)
    client.post("/api/vehicles/1", json={"body_style": "kombi", "color": "#b3261e"})
    client.post("/api/vehicles/1", json={"body_style": None})        # zurück zum Punkt
    row = db.execute("SELECT body_style, color FROM vehicles WHERE id = 1").fetchone()
    assert (row["body_style"], row["color"]) == (None, "#b3261e")


def test_map_page_gets_the_car_only_when_configured(client, db):
    html = client.get("/").get_data(as_text=True)
    assert "const KARTENAUTO = null;" in html
    assert "kartenauto.js" in html and "IDmateKartenauto.svg(KARTENAUTO.typ, KARTENAUTO.farbe" in html
    assert "breite: 13.2," in html        # halbiert, dann zehn Prozent größer
    db.execute("UPDATE vehicles SET body_style = 'limousine', color = '#f2f2f2' WHERE id = 1")
    db.commit()
    html = client.get("/").get_data(as_text=True)
    assert 'const KARTENAUTO = {"farbe": "#f2f2f2", "typ": "limousine"};' in html
    # Der Pfeil in Fahrt und der Punkt ohne Einstellung bleiben im Skript.
    assert 'polygon points="16,4 24,26 16,20 8,26"' in html and 'circle cx="16" cy="16" r="10"' in html


def test_drawing_script_carries_the_three_bodies(client):
    js = client.get("/static/kartenauto.js").get_data(as_text=True)
    assert "window.IDmateKartenauto" in js
    for typ in ("limousine:", "kombi:", "suv:"):
        assert typ in js
    for stueck in ("lichtVorn", "lichtHinten", "SPIEGEL_60", "SPIEGEL_64", 'stroke-width="7"', "linearGradient",
                   # Dach (genehmigt): Glasdach zwischen zwei Holmen, beim Kombi/SUV vor dem Heck endend
                   'dach: "M10 53 L50 53 C50.5 68 50 84 49 97 L11 97 C10 84 9.5 68 10 53 Z"',
                   'dach: "M10 53 L50 53 C51 68 51 84 50.5 98 L9.5 98 C9 84 9 68 10 53 Z"',
                   'dach: "M9 52 L55 52 C56 68 56 84 55.5 100 L8.5 100 C8 84 8 68 9 52 Z"',
                   'glas: dunkel ? "#66788a"', "glasrand",
                   # Frontscheibe als Trapez, nicht halbrund
                   'front: "M9 47 L13 31 Q30 27.5 47 31 L51 47 Z"', 'front: "M8 46 L12 29 Q32 25.5 52 29 L56 46 Z"'):
        assert stueck in js, stueck


def test_admin_offers_body_and_colour_per_vehicle(client):
    html = client.get("/admin").get_data(as_text=True)
    for stueck in ('data-args=\'[${v.id}, "body_style"]\'', 'data-args=\'[${v.id}, "color"]\'',
                   'type="color"', 'value="limousine"', 'value="kombi"', 'value="suv"',
                   "Auto auf der Karte", "Wagenfarbe auf der Karte"):
        assert stueck in html, stueck
