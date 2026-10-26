"""Aenderungen kommen auf dem Handy an (1.2.155-dev).

Der Service Worker hielt idmate.css und idmate.js cache-first unter dem
festen Namen idmate-v1 – ein geaendertes Stylesheet erreichte das Handy
nie. Jetzt: Versionsstempel an den eigenen Dateien, Cache-Name mit der
App-Version (app.py setzt sie in sw.js ein), Assets netzwerk-zuerst."""

import config
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


def test_service_worker_carries_the_version_and_is_never_cached(client):
    r = client.get("/static/sw.js")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-cache"
    js = r.get_data(as_text=True)
    assert f"const CACHE = 'idmate-{config.VERSION}'" in js and "__VERSION__" not in js
    assert "NETWORK-FIRST for the small set" in js and "ignoreSearch: true" in js


def test_own_assets_carry_a_version_stamp(client):
    html = client.get("/").get_data(as_text=True)
    for name in ("idmate.css", "idmate.js", "basemap.js", "kartenauto.js", "spline.js"):
        assert f"/static/{name}?v={config.VERSION}" in html, name
    assert f"/static/beleg.js?v={config.VERSION}" in client.get("/charges").get_data(as_text=True)
