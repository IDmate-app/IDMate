"""The Roundtrip world map as IDmate's base map: with ROUNDTRIP_URL set, every
map gets /tiles/{z}/{x}/{y}.mvt, and Flask relays those tiles from Roundtrip
(same origin for the browser, no Roundtrip login, upstream host hidden)."""

import io
import urllib.error
import urllib.request

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


class _Upstream:
    """Stand-in for urllib.request.urlopen: records the URL, answers as told."""
    def __init__(self, status=200, body=b"\x1a\x00tile", error=None):
        self.status, self.body, self.error, self.calls = status, body, error, []

    def __call__(self, req, timeout=None):
        self.calls.append(req.full_url if hasattr(req, "full_url") else req)
        if self.error:
            raise self.error
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def _http_error(code):
    return urllib.error.HTTPError("http://rt/x", code, "err", {}, io.BytesIO(b""))


def test_without_roundtrip_nothing_changes(client, app_module, monkeypatch):
    import config
    monkeypatch.setattr(config, "ROUNDTRIP_URL", "")
    with app_module.app.test_request_context("/"):
        assert app_module.get_map_tile_url() == app_module.MAP_STYLES["osm_de"]
    assert client.get("/tiles/1/1/1.mvt").status_code == 404
    profile = client.get("/profile").get_data(as_text=True)
    assert "disabled" not in profile.split('id="prefMapStyle"', 1)[1].split(">", 1)[0]


def test_roundtrip_map_replaces_every_style_and_is_relayed(client, app_module, monkeypatch):
    import config
    monkeypatch.setattr(config, "ROUNDTRIP_URL", "http://rt:8580")
    with app_module.app.test_request_context("/"):
        assert app_module.get_map_tile_url() == "/tiles/{z}/{x}/{y}.mvt"

    up = _Upstream()
    monkeypatch.setattr(urllib.request, "urlopen", up)
    r = client.get("/tiles/5/16/10.mvt")
    assert r.status_code == 200
    assert r.mimetype == "application/vnd.mapbox-vector-tile"
    assert r.data == b"\x1a\x00tile"
    assert "max-age=604800" in r.headers["Cache-Control"]
    assert up.calls == ["http://rt:8580/tiles/5/16/10.mvt"]

    # empty tile (sea): 204 passes through unchanged
    monkeypatch.setattr(urllib.request, "urlopen", _Upstream(status=204, body=b""))
    assert client.get("/tiles/5/16/10.mvt").status_code == 204

    # upstream verdicts: 404 stays 404, anything else is a bad gateway
    monkeypatch.setattr(urllib.request, "urlopen", _Upstream(error=_http_error(404)))
    assert client.get("/tiles/5/16/10.mvt").status_code == 404
    monkeypatch.setattr(urllib.request, "urlopen", _Upstream(error=_http_error(500)))
    assert client.get("/tiles/5/16/10.mvt").status_code == 502
    monkeypatch.setattr(urllib.request, "urlopen", _Upstream(error=urllib.error.URLError("down")))
    assert client.get("/tiles/5/16/10.mvt").status_code == 502


def test_tiles_outside_the_planet_never_reach_roundtrip(client, monkeypatch):
    import config
    monkeypatch.setattr(config, "ROUNDTRIP_URL", "http://rt:8580")
    up = _Upstream()
    monkeypatch.setattr(urllib.request, "urlopen", up)
    assert client.get("/tiles/16/0/0.mvt").status_code == 404      # deeper than the planet
    assert client.get("/tiles/3/9/0.mvt").status_code == 404       # x beyond 2**z
    assert up.calls == []


def test_tiles_need_a_login(app_module, db, monkeypatch):
    import config
    monkeypatch.setattr(config, "ROUNDTRIP_URL", "http://rt:8580")
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as anon:
        r = anon.get("/tiles/5/16/10.mvt")
    assert r.status_code in (302, 401)


def test_pages_wire_the_roundtrip_map(client, db, monkeypatch):
    import config
    monkeypatch.setattr(config, "ROUNDTRIP_URL", "http://rt:8580")
    jid = client.post("/api/journeys", json={"title": "Finnland", "date_from": "2026-07-15",
                                             "date_to": "2026-07-20", "excluded_ids": []}).get_json()["id"]
    html = client.get(f"/journeys/{jid}").get_data(as_text=True)
    assert "IDmateMap.baseLayer('/tiles/{z}/{x}/{y}.mvt'" in html
    for lib in ("vendor/leaflet.js", "vendor/protomaps-leaflet.js", "mapstyle.js", "maprules.js", "basemap.js"):
        assert lib in html
    assert "unpkg.com" not in html
    # the style pickers say why they are inactive
    profile = client.get("/profile").get_data(as_text=True)
    assert "disabled" in profile.split('id="prefMapStyle"', 1)[1].split(">", 1)[0]
    assert "Roundtrip" in profile


def test_broken_roundtrip_url_is_named_at_startup(app_module, monkeypatch, caplog):
    """192.168188.5 (dot missing) is not an IP — Python resolves it as a host
    name and every tile fails with a DNS error that hides the typo. The
    startup check names it."""
    import logging
    import config
    monkeypatch.setattr(config, "ROUNDTRIP_URL", "http://192.168188.5:8580")
    with caplog.at_level(logging.ERROR, logger="triplog.app"):
        app_module._check_roundtrip_url()
    assert "dot missing" in caplog.text
    caplog.clear()
    monkeypatch.setattr(config, "ROUNDTRIP_URL", "http://192.168.188.5:8580")
    with caplog.at_level(logging.ERROR, logger="triplog.app"):
        app_module._check_roundtrip_url()
    assert caplog.text == ""


def test_unreachable_roundtrip_logs_once_a_minute(client, monkeypatch, caplog):
    import logging
    import config
    monkeypatch.setattr(config, "ROUNDTRIP_URL", "http://rt:8580")
    monkeypatch.setattr(urllib.request, "urlopen", _Upstream(error=urllib.error.URLError("down")))
    import app as app_module
    app_module._roundtrip_warned_at = 0.0
    with caplog.at_level(logging.WARNING, logger="triplog.app"):
        for _ in range(5):
            assert client.get("/tiles/5/16/10.mvt").status_code == 502
    assert caplog.text.count("unreachable") == 1
