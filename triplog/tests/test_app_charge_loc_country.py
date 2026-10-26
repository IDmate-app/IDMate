"""A new charge geofence gets its country code from its coordinates.

Reported: "IDMate does not detect the country code of new geofences." The
editor asked the reverse geocoder, but the form pre-set the field to "DE"
and the suggestion only filled an EMPTY field — so it never did, and every
geofence abroad was saved as DE unless someone typed the code. Now the form
of a new geofence starts empty, the suggestion fills it, and moving the
marker (or closing a polygon) asks again."""

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


def test_reverse_geocode_api_returns_the_country_code(client, monkeypatch):
    import geocoder
    monkeypatch.setattr(geocoder, "reverse_geocode", lambda lat, lon: ("FI - 96100 Rovaniemi", "fi"))
    d = client.get("/api/reverse-geocode?lat=66.5&lon=25.7").get_json()
    assert d == {"name": "FI - 96100 Rovaniemi", "country_code": "FI"}


def test_reverse_geocode_api_fails_soft(client, monkeypatch):
    import geocoder

    def kaputt(lat, lon):
        raise OSError("nominatim down")
    monkeypatch.setattr(geocoder, "reverse_geocode", kaputt)
    d = client.get("/api/reverse-geocode?lat=66.5&lon=25.7").get_json()
    assert d == {"name": "", "country_code": ""}
    assert client.get("/api/reverse-geocode?lat=x").status_code == 400


def test_new_geofence_form_leaves_the_country_to_the_suggestion(client):
    html = client.get("/admin").get_data(as_text=True)
    feld = html.split('id="clCountryCode"', 1)[1].split(">", 1)[0]
    # A new geofence (no id) starts empty; an existing one keeps its stored code.
    assert "(loc.id ? 'DE' : '')" in feld
    assert "(loc.country_code || 'DE')" not in feld
    # The suggestion writes into the empty field and marks what it wrote, so a
    # moved marker may replace it, a typed code never.
    assert "function clSetCountry" in html
    assert "ccEl.dataset.auto = ccEl.value" in html
    # Every way of placing a new geofence asks for the country: map click,
    # marker drag, polygon close, and opening the editor from a charge.
    assert html.count("suggestChargeLocCountry(") >= 5


def test_country_suggestion_never_overwrites_a_typed_code(client):
    """Read the JavaScript as text: the guard must compare against data-auto."""
    html = client.get("/admin").get_data(as_text=True)
    fn = html.split("function clSetCountry", 1)[1].split("async function suggestChargeLocCountry", 1)[0]
    assert "if (cur && cur !== (ccEl.dataset.auto || '')) return;" in fn
