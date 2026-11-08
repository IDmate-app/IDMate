"""Long ferries on request (1.2.298): car ferries from OpenStreetMap
(Overpass), joined where split, only > MAX_FERRY_KM; terminal reachable with
energy left (Valhalla route); range area around the port of arrival."""

import ferries
import wind_range as wr
from test_wind_range import MODEL, _enc

CRUISE = {"left": 50.0, "model": MODEL, "temp": 10, "v_eval": 110, "km": 230}


def _way(pts, **tags):
    return {"type": "way", "tags": dict(route="ferry", motor_vehicle="yes", **tags),
            "geometry": [{"lat": la, "lon": lo} for la, lo in pts]}


def _line(a, b, n=20):
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]


# Travemünde-like terminal ~70 km from the car, Trelleborg-like port across
TERMINAL, MID, PORT = (53.96, 10.87), (54.6, 11.9), (55.37, 13.15)
OSM = {"elements": [
    _way(_line(TERMINAL, MID), name="Travemünde - Trelleborg", duration="08:30"),
    _way(_line(MID, PORT)),                                     # same ferry, second way
    _way(_line((54.50, 11.22), (54.66, 11.36)), name="Puttgarden - Rødby"),   # short: ignored
]}


def _road(lat0, lon0, lat1, lon1, n=50):
    """A straight 'road' from the car to the terminal at 100 km/h."""
    pts = [(lat0 + (lat1 - lat0) * i / n, lon0 + (lon1 - lon0) * i / n) for i in range(n + 1)]
    km = ferries._km((lat0, lon0), (lat1, lon1)) * 1.2
    return {"trip": {"legs": [{"shape": _enc(pts), "maneuvers": [
        {"length": km, "time": km / 100 * 3600, "begin_shape_index": 0, "end_shape_index": n}]}]}}


def test_bbox_is_coarse():
    s, w, n, e = ferries.bbox(53.81, 10.37, 230)
    assert all(float(x).is_integer() for x in (s, w, n, e))
    assert s < 53.81 - 2 and n > 53.81 + 2


def test_crossings_joins_split_ways_and_drops_short_ones():
    cs = ferries.crossings(OSM)
    assert len(cs) == 1
    c = cs[0]
    assert c["name"] == "Travemünde - Trelleborg" and c["h"] == 8.5
    assert 150 < c["km"] < 230
    ends = {(round(c["pts"][0][0], 2), round(c["pts"][0][1], 2)), (round(c["pts"][-1][0], 2), round(c["pts"][-1][1], 2))}
    assert ends == {(53.96, 10.87), (55.37, 13.15)}


def test_run_finds_ferry_with_range_after_arrival():
    asked = []
    def area(lat, lon, kms):
        asked.append((round(lat, 2), round(lon, 2), kms))
        return None
    ferries._run("devF", 53.81, 10.37, CRUISE, [], route_fn=_road, area_fn=area, osm_fn=lambda box: OSM)
    st = ferries.status("devF")
    assert st["status"] == "ok" and len(st["ferries"]) == 1
    f = st["ferries"][0]
    assert f["from"] == [53.96, 10.87] and f["to"] == [55.37, 13.15]   # terminal = near end
    assert 0 < f["kwh_left"] < 50 and f["km_after"] > 100
    assert asked == [(55.37, 13.15, [f["km_after"]])]


def test_terminal_out_of_reach_is_skipped():
    ferries._run("devG", 53.81, 10.37, dict(CRUISE, left=3.0), [], route_fn=_road,
                 area_fn=lambda *a: None, osm_fn=lambda box: OSM)
    assert ferries.status("devG")["ferries"] == []


def test_overpass_failure_reports_error():
    def boom(box):
        raise OSError("overpass down")
    ferries._run("devH", 53.81, 10.37, CRUISE, [], route_fn=_road, osm_fn=boom)
    st = ferries.status("devH")
    assert st["status"] == "error" and "overpass" in st["error"]


def test_endpoints_off_and_no_model(app_module, db, monkeypatch):
    import config
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'dev1')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) "
               "VALUES (1, 'admin', 'x', 1, 1)")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        monkeypatch.setattr(config, "FERRY_FILE", "")
        monkeypatch.setattr(config, "VALHALLA_URL", "")
        assert c.post("/api/ferries/start").get_json() == {"status": "off"}
        monkeypatch.setattr(config, "VALHALLA_URL", "http://valhalla:8002")
        monkeypatch.setattr(config, "FERRY_URL", "")
        assert c.post("/api/ferries/start").get_json() == {"status": "off"}
        monkeypatch.setattr(config, "FERRY_URL", "https://overpass.example/api")
        monkeypatch.setattr(app_module, "_vehicle_state", lambda dev: {"lat": 53.8, "lon": 10.4, "calc_range": 200})
        assert c.post("/api/ferries/start").get_json()["error"] == "no_model"
        assert "lat" not in c.get("/api/ferries").get_json()


def test_overpass_falls_back_to_next_server(monkeypatch):
    import io
    import urllib.error
    import config
    monkeypatch.setattr(config, "FERRY_URL", "https://a.example/api, https://b.example/api")
    asked = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout):
        asked.append(req.full_url)
        if "a.example" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, None)
        return Resp(b'{"elements": []}')
    assert ferries._overpass((53, 10, 55, 12), opener=opener) == {"elements": []}
    assert asked == ["https://a.example/api", "https://b.example/api"]


def test_overpass_all_busy_reports_busy(monkeypatch):
    import urllib.error
    import config
    monkeypatch.setattr(config, "FERRY_URL", "https://a.example/api,https://b.example/api")

    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 504, "Gateway Timeout", {}, None)
    try:
        ferries._overpass((53, 10, 55, 12), opener=opener)
        assert False
    except OSError as e:
        assert "overpass busy" in str(e) and "a.example: HTTP 504" in str(e)


def _geojson(osm):
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": el["tags"],
         "geometry": {"type": "LineString", "coordinates": [[p["lon"], p["lat"]] for p in el["geometry"]]}}
        for el in osm["elements"]]}


def test_file_chains_from_osmium_export(tmp_path, monkeypatch):
    import json
    import config
    data = _geojson(OSM)
    data["features"].append({"type": "Feature", "properties": {"route": "ferry", "foot": "yes"},   # no cars
                             "geometry": {"type": "LineString", "coordinates": [[10.0, 54.0], [12.0, 56.0]]}})
    f = tmp_path / "ferries.geojson"
    f.write_text(json.dumps(data))
    cs = ferries.file_chains(str(f))
    assert len(cs) == 1 and cs[0]["name"] == "Travemünde - Trelleborg"
    seq = tmp_path / "ferries.geojsonseq"
    seq.write_text("\n".join("\x1e" + json.dumps(ft) for ft in data["features"]))
    assert len(ferries.file_chains(str(seq))) == 1
    assert ferries.file_chains(str(tmp_path / "missing.geojson")) is None
    # the search uses the file and never asks Overpass
    monkeypatch.setattr(config, "FERRY_FILE", str(f))
    monkeypatch.setattr(ferries, "_overpass", lambda box: (_ for _ in ()).throw(AssertionError("overpass")))
    assert ferries.enabled()
    ferries._run("devJ", 53.81, 10.37, CRUISE, [], route_fn=_road, area_fn=lambda *a: None)
    st = ferries.status("devJ")
    assert st["status"] == "ok" and len(st["ferries"]) == 1


def test_two_ferries_from_one_port_stay_apart():
    kiel, oslo, gbg = (54.32, 10.14), (59.90, 10.75), (57.70, 11.95)
    cs = ferries.crossings({"elements": [_way(_line(kiel, oslo), name="Kiel - Oslo"),
                                         _way(_line(kiel, gbg), name="Kiel - Göteborg")]})
    assert sorted(c["name"] for c in cs) == ["Kiel - Göteborg", "Kiel - Oslo"]


def test_both_directions_count_once():
    back = _way(_line(PORT, TERMINAL, 25), name="Trelleborg - Travemünde")     # other direction, own way
    osm = {"elements": OSM["elements"] + [back]}
    ferries._run("devK", 53.81, 10.37, CRUISE, [], route_fn=_road, area_fn=lambda *a: None, osm_fn=lambda box: osm)
    assert len(ferries.status("devK")["ferries"]) == 1


def test_ferry_to_port_reachable_by_road_is_skipped():
    # Travemünde–Rostock-like: the port across is close enough to drive there
    near = {"elements": [_way(_line(TERMINAL, (54.6, 11.9)) + _line((54.6, 11.9), (54.18, 12.09))[1:],
                              name="Travemünde - Rostock")]}
    ferries._run("devL", 53.81, 10.37, CRUISE, [], route_fn=_road, area_fn=lambda *a: None,
                 osm_fn=lambda box: near)
    assert ferries.status("devL")["ferries"] == []
    # the same ferry stays when the road to the port is blocked (no route)
    ferries._run("devM", 53.81, 10.37, CRUISE, [], route_fn=_road, area_fn=lambda *a: None,
                 osm_fn=lambda box: near, land_fn=lambda *a: {})
    assert len(ferries.status("devM")["ferries"]) == 1
