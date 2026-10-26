"""The journeys list shows one three-line row per journey (title / from – to /
trips · km) with a square mini map on the left: the route filling the square
on a map background, rendered ahead of time (after new trips, on edits, at
start-up), cached as a WebP file and served immutable."""

import io
import os
import struct

import pytest
from PIL import Image

import kartenbild


# --- a tiny Mapbox Vector Tile encoder for the tests ------------------------

def _varint(n):
    aus = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            aus.append(b | 0x80)
        else:
            aus.append(b)
            return bytes(aus)


def _feld(nr, art, wert):
    kopf = _varint((nr << 3) | art)
    if art == 0:
        return kopf + _varint(wert)
    return kopf + _varint(len(wert)) + wert


def _zz(n):
    return (n << 1) ^ (n >> 63) if n < 0 else n << 1


def _geom(teile, schliessen):
    aus, x, y = [], 0, 0
    for teil in teile:
        aus.append((1 << 3) | 1)
        dx, dy = teil[0][0] - x, teil[0][1] - y
        aus += [_zz(dx), _zz(dy)]
        x, y = teil[0]
        aus.append((len(teil) - 1) << 3 | 2)
        for px, py in teil[1:]:
            aus += [_zz(px - x), _zz(py - y)]
            x, y = px, py
        if schliessen:
            aus.append(7)
    return b"".join(_varint(v) for v in aus)


def mvt(lagen):
    """lagen: {name: [(typ, {k: v}, [ring/line, ...]), ...]}"""
    tile = b""
    for name, features in lagen.items():
        keys, values = [], []
        fbufs = []
        for typ, props, teile in features:
            tags = []
            for k, v in props.items():
                if k not in keys:
                    keys.append(k)
                if v not in values:
                    values.append(v)
                tags += [keys.index(k), values.index(v)]
            fb = _feld(3, 0, typ) + _feld(2, 2, b"".join(_varint(t) for t in tags)) \
                + _feld(4, 2, _geom(teile, typ == 3))
            fbufs.append(_feld(2, 2, fb))
        lage = _feld(15, 0, 2) + _feld(1, 2, name.encode()) + b"".join(fbufs)
        for k in keys:
            lage += _feld(3, 2, k.encode())
        for v in values:
            lage += _feld(4, 2, _feld(1, 2, str(v).encode()))
        lage += _feld(5, 0, 4096)
        tile += _feld(3, 2, lage)
    return tile


# earth everywhere, a lake in the middle, a highway across, a place label
# (which must never show)
KACHEL = mvt({
    "earth": [(3, {"kind": "earth"}, [[(0, 0), (4096, 0), (4096, 4096), (0, 4096)]])],
    "water": [(3, {"kind": "lake"}, [[(1500, 1500), (2600, 1500), (2600, 2600), (1500, 2600)]])],
    "roads": [(2, {"kind": "highway"}, [[(0, 3500), (4096, 600)]])],
    "places": [(1, {"kind": "locality", "name": "Sontheim"}, [[(2048, 2048)]])],
})


def test_mvt_reader_reads_layers_props_and_geometry():
    lagen = {l["name"]: l for l in kartenbild.mvt_dekodieren(KACHEL)}
    assert set(lagen) == {"earth", "water", "roads", "places"}
    see = lagen["water"]["features"][0]
    assert see["typ"] == 3 and see["props"] == {"kind": "lake"}
    assert see["geom"] == [[(1500, 1500), (2600, 1500), (2600, 2600), (1500, 2600), (1500, 1500)]]
    strasse = lagen["roads"]["features"][0]
    assert strasse["typ"] == 2 and strasse["geom"] == [[(0, 3500), (4096, 600)]]
    assert lagen["places"]["features"][0]["props"]["name"] == "Sontheim"


@pytest.fixture
def bp(app_module):
    from blueprints import journeys
    return journeys


@pytest.fixture
def renders(bp, monkeypatch):
    """Counts the renders (no tiles: without Roundtrip the route is drawn on
    the plain background)."""
    import config
    monkeypatch.setattr(config, "ROUNDTRIP_URL", "")
    aufrufe = []
    echt = bp._thumb_bild

    def zaehlen(segments):
        aufrufe.append(1)
        return echt(segments)
    monkeypatch.setattr(bp, "_thumb_bild", zaehlen)
    return aufrufe


@pytest.fixture
def roundtrip(bp, monkeypatch):
    """The Roundtrip world map: every tile is KACHEL."""
    import config
    monkeypatch.setattr(config, "ROUNDTRIP_URL", "http://rt:8580")
    aufrufe = []

    def laden(z, x, y):
        aufrufe.append((z, x, y))
        return KACHEL
    monkeypatch.setattr(bp, "_vektorkachel_laden", laden)
    return aufrufe


@pytest.fixture
def client(app_module, bp, db, tmp_path, monkeypatch, renders):
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'dev1')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) "
               "VALUES (1, 'admin', 'x', 1, 1)")
    db.commit()
    monkeypatch.setattr(bp, "MEDIA_DIR", str(tmp_path / "media"))
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def _trip(db, start, km, s_lat, s_lon, e_lat, e_lon):
    cur = db.execute(
        "INSERT INTO trips (device, start_time, end_time, distance_km, start_lat, start_lon, end_lat, end_lon) "
        "VALUES ('dev1', ?, ?, ?, ?, ?, ?, ?)", (start, start, km, s_lat, s_lon, e_lat, e_lon))
    db.commit()
    return cur.lastrowid


def _journey(client, db):
    _trip(db, "2026-06-14T09:00:00", 100, 52.5, 13.4, 53.55, 10.0)
    _trip(db, "2026-06-15T09:00:00", 80, 53.55, 10.0, 54.3, 10.1)
    return client.post("/api/journeys", json={
        "title": "Ostsee", "date_from": "2026-06-14T08:00", "date_to": "2026-06-15T18:00",
        "excluded_ids": []}).get_json()["id"]


def _box(bild, farbe, toleranz=25):
    """Bounding box of the pixels near ``farbe``, or None."""
    xs, ys = [], []
    r0, g0, b0 = farbe
    for i, (r, g, b) in enumerate(bild.convert("RGB").getdata()):
        if abs(r - r0) < toleranz and abs(g - g0) < toleranz and abs(b - b0) < toleranz:
            xs.append(i % bild.width)
            ys.append(i // bild.width)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def test_route_fills_the_square(bp, roundtrip):
    bild, fertig = bp._thumb_bild([[[52.5, 13.4], [53.55, 10.0]], [[53.55, 10.0], [54.3, 10.1]]])
    assert fertig and bild.size == (kartenbild.GROESSE, kartenbild.GROESSE)
    x0, y0, x1, y1 = _box(bild, kartenbild.ROUTE)
    innen = kartenbild.GROESSE - 2 * kartenbild.RAND
    assert max(x1 - x0, y1 - y0) >= innen - 6          # the long side spans the square
    assert x0 >= kartenbild.RAND - 6 and x1 <= kartenbild.GROESSE - kartenbild.RAND + 6


def test_vector_map_paints_land_water_roads_and_no_labels(bp, roundtrip):
    bild, fertig = bp._thumb_bild([[[52.0, 13.0], [60.0, 13.0]]])
    assert fertig and roundtrip                                   # tiles were asked for
    assert _box(bild, kartenbild.PALETTE["land"], 12)             # earth
    assert _box(bild, kartenbild.PALETTE["wasser"], 12)           # the lake
    assert _box(bild, kartenbild.PALETTE["highway"], 20)          # the road
    # every pixel is one of ours: land, lake, road, route or halo — no text colour
    eigene = list(kartenbild.PALETTE.values()) + [kartenbild.ROUTE, kartenbild.HINTERGRUND, (255, 255, 255)]
    fremd = 0
    for px in bild.convert("RGB").getdata():
        if not any(sum(abs(a - b) for a, b in zip(px, f)) < 90 for f in eigene):
            fremd += 1
    assert fremd < 0.03 * kartenbild.GROESSE ** 2                 # anti-aliasing seams only


def test_vector_map_without_a_tile_is_not_final(bp, roundtrip, monkeypatch):
    monkeypatch.setattr(bp, "_vektorkachel_laden", lambda z, x, y: None)
    bild, fertig = bp._thumb_bild([[[52.5, 13.4], [53.55, 10.0]]])
    assert not fertig and _box(bild, kartenbild.ROUTE)
    monkeypatch.setattr(bp, "_vektorkachel_laden", lambda z, x, y: b"")   # empty tile: open sea
    bild, fertig = bp._thumb_bild([[[52.5, 13.4], [53.55, 10.0]]])
    assert fertig and bild.getpixel((2, 2)) == kartenbild.PALETTE["wasser"]


def test_without_roundtrip_the_route_sits_on_the_plain_background(bp, renders):
    bild, fertig = bp._thumb_bild([[[52.5, 13.4], [53.55, 10.0]], [[53.55, 10.0], [54.3, 10.1]]])
    assert fertig and bild.size == (kartenbild.GROESSE, kartenbild.GROESSE)
    assert bild.getpixel((2, 2)) == kartenbild.HINTERGRUND          # no tiles, no labels
    box = _box(bild, kartenbild.ROUTE)
    assert box and box[0] >= kartenbild.RAND - 6 and box[2] <= kartenbild.GROESSE - kartenbild.RAND + 6


def test_thumb_keeps_the_route_shape(bp, renders):
    bild, _ = bp._thumb_bild([[[52.0, 13.0], [60.0, 13.0]]])         # north-south: tall, narrow
    x0, y0, x1, y1 = _box(bild, kartenbild.ROUTE)
    assert (y1 - y0) > 3 * (x1 - x0)
    bild, _ = bp._thumb_bild([[[52.0, 8.0], [52.0, 20.0]]])          # east-west: wide, flat
    x0, y0, x1, y1 = _box(bild, kartenbild.ROUTE)
    assert (x1 - x0) > 3 * (y1 - y0)


def test_thumb_without_route_is_a_placeholder(bp, renders):
    bild, fertig = bp._thumb_bild([])
    assert fertig and bild.size == (kartenbild.GROESSE, kartenbild.GROESSE)
    assert _box(bild, kartenbild.ROUTE) is None


def test_list_shows_three_line_rows_with_thumb(client, db):
    jid = _journey(client, db)
    html = client.get("/journeys").get_data(as_text=True)
    assert "<table" not in html
    assert 'class="journey-row"' in html
    assert "Ostsee" in html
    assert "14.06.2026 – 15.06.2026" in html
    assert "2 Fahrten · 180 km" in html
    assert f'src="/journeys/{jid}/thumb.webp?k=6-2-2-180"' in html
    assert 'width="76" height="76"' in html


def test_thumb_is_rendered_once_and_cached(client, bp, db, renders):
    jid = _journey(client, db)
    res = client.get(f"/journeys/{jid}/thumb.webp")
    assert res.status_code == 200
    assert res.mimetype == "image/webp"
    assert "immutable" in res.headers["Cache-Control"]
    bild = Image.open(io.BytesIO(res.data))
    assert bild.format == "WEBP" and bild.size == (kartenbild.GROESSE, kartenbild.GROESSE)
    assert len(res.data) < 40_000
    datei = os.path.join(bp._thumb_dir(), f"{jid}-6-2-2-180.webp")
    assert os.path.exists(datei)
    n = len(renders)
    with open(datei, "wb") as f:
        f.write(b"RIFFcached")
    assert client.get(f"/journeys/{jid}/thumb.webp").data == b"RIFFcached"
    assert len(renders) == n
    _trip(db, "2026-06-15T12:00:00", 20, 54.3, 10.1, 54.5, 9.9)
    res = client.get(f"/journeys/{jid}/thumb.webp")
    assert res.status_code == 200 and len(renders) > n
    assert not os.path.exists(datei)
    assert os.path.exists(os.path.join(bp._thumb_dir(), f"{jid}-6-3-3-200.webp"))
    assert client.get("/journeys/999/thumb.webp").status_code == 404


def test_thumb_with_a_missing_tile_is_not_cached(client, bp, db, monkeypatch):
    import config
    jid = _journey(client, db)
    monkeypatch.setattr(config, "ROUNDTRIP_URL", "http://rt:8580")
    monkeypatch.setattr(bp, "_vektorkachel_laden", lambda z, x, y: None)
    res = client.get(f"/journeys/{jid}/thumb.webp")
    assert res.status_code == 200 and res.headers["Cache-Control"] == "no-cache"
    assert not os.path.exists(os.path.join(bp._thumb_dir(), f"{jid}-6-2-2-180.webp"))


def test_pictures_are_rendered_ahead(client, bp, db, renders, monkeypatch):
    """New trips and journey edits queue a render; the list then serves the
    file without rendering."""
    from journey_sync import sync_journey_trips
    angestossen = []
    monkeypatch.setattr(bp, "thumbs_im_hintergrund", lambda ids: angestossen.append(list(ids)))
    jid = _journey(client, db)
    assert [jid] in angestossen                                   # creating the journey
    angestossen.clear()
    _trip(db, "2026-06-15T12:00:00", 20, 54.3, 10.1, 54.5, 9.9)   # detected later
    assert sync_journey_trips(db, device="dev1") == 1
    assert angestossen == [[jid]]
    assert sync_journey_trips(db, device="dev1") == 0 and angestossen == [[jid]]   # nothing new: no render
    # the pre-render itself writes the file; the request then only reads it
    bp.thumb_vorrendern(jid)
    datei = os.path.join(bp._thumb_dir(), f"{jid}-6-3-3-200.webp")
    assert os.path.exists(datei)
    n = len(renders)
    assert client.get(f"/journeys/{jid}/thumb.webp").status_code == 200
    assert len(renders) == n
    bp.thumb_vorrendern(jid)                                      # already there: no second render
    assert len(renders) == n
    # in tests the background start is a no-op (no thread, no real tiles)
    monkeypatch.undo()
    bp.thumbs_im_hintergrund([jid])


def test_route_api_unchanged_by_the_thumb_refactor(client, db):
    jid = _journey(client, db)
    data = client.get(f"/api/journeys/{jid}/route").get_json()
    assert data["source"] == "coords" and len(data["segments"]) == 2
    assert client.get("/api/journeys/999/route").get_json()["segments"] == []
