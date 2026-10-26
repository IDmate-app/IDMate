"""Mini map of a journey for the journeys list, rendered on the server.

The picture is a square: the route fills it (fractional zoom, small margin),
drawn over a map background. With the Roundtrip world map configured the
background comes from its vector tiles, decoded here (a small Mapbox Vector
Tile reader, no protobuf dependency) and painted in a dark palette without
any labels — no country or town names. Without Roundtrip there is no map
behind the route: public raster tiles carry labels, and none is wanted.

Everything is plain Pillow. Lines and polygons are drawn at double size and
scaled down once, which gives smooth edges.
"""

import gzip
import math

from PIL import Image, ImageDraw

GROESSE = 256          # the picture, square
RAND = 12              # the route keeps this far from the edge
FAKTOR = 4             # supersampling
ZOOM_MAX_DATEN = 15    # the planet holds zoom 0..15; deeper is overzoomed
ROUTE = (88, 166, 255)                 # --accent
HINTERGRUND = (13, 17, 23)             # the app's dark background (no data)
PALETTE = {
    "wasser": (36, 62, 90),
    "land": (58, 63, 72),
    "wald": (52, 68, 58),
    "highway": (104, 110, 118),
    "major": (86, 92, 100),
    "medium": (74, 80, 88),
    "minor": (66, 72, 80),
}
ROUTE_BREITE = 3.15    # px in the picture
WALD_ARTEN = {"wood", "forest", "park", "nature_reserve", "national_park"}


# --- Mapbox Vector Tile reader ----------------------------------------------

def _varint(buf, i):
    wert, schritt = 0, 0
    while True:
        b = buf[i]
        i += 1
        wert |= (b & 0x7F) << schritt
        if not b & 0x80:
            return wert, i
        schritt += 7


def _felder(buf):
    """(field number, wire type, value) for every field of a message; length
    delimited values come as bytes, varints as int, fixed as raw bytes."""
    i, n = 0, len(buf)
    while i < n:
        schluessel, i = _varint(buf, i)
        feld, art = schluessel >> 3, schluessel & 7
        if art == 0:
            wert, i = _varint(buf, i)
        elif art == 2:
            laenge, i = _varint(buf, i)
            wert = buf[i:i + laenge]
            i += laenge
        elif art == 1:
            wert = buf[i:i + 8]
            i += 8
        elif art == 5:
            wert = buf[i:i + 4]
            i += 4
        else:
            raise ValueError(f"unknown wire type {art}")
        yield feld, art, wert


def _gepackt(buf):
    i, aus = 0, []
    while i < len(buf):
        v, i = _varint(buf, i)
        aus.append(v)
    return aus


def _zickzack(n):
    return (n >> 1) ^ -(n & 1)


def _wert(buf):
    import struct
    for feld, art, v in _felder(buf):
        if feld == 1:
            return v.decode("utf-8", "replace")
        if feld == 2:
            return struct.unpack("<f", v)[0]
        if feld == 3:
            return struct.unpack("<d", v)[0]
        if feld in (4, 5):
            return v
        if feld == 6:
            return _zickzack(v)
        if feld == 7:
            return bool(v)
    return None


def _geometrie(befehle):
    """Rings/lines as lists of (x, y) in tile units."""
    teile, aktuell = [], []
    x = y = 0
    i = 0
    while i < len(befehle):
        cmd, anzahl = befehle[i] & 7, befehle[i] >> 3
        i += 1
        if cmd == 1:                      # MoveTo
            for _ in range(anzahl):
                x += _zickzack(befehle[i])
                y += _zickzack(befehle[i + 1])
                i += 2
                if aktuell:
                    teile.append(aktuell)
                aktuell = [(x, y)]
        elif cmd == 2:                    # LineTo
            for _ in range(anzahl):
                x += _zickzack(befehle[i])
                y += _zickzack(befehle[i + 1])
                i += 2
                aktuell.append((x, y))
        elif cmd == 7:                    # ClosePath
            if aktuell:
                aktuell.append(aktuell[0])
        else:
            break
    if aktuell:
        teile.append(aktuell)
    return teile


def mvt_dekodieren(daten):
    """Layers of a tile: [{"name", "extent", "features": [{"typ", "props", "geom"}]}].
    typ 1 point, 2 line, 3 polygon; geom a list of coordinate lists."""
    if daten[:2] == b"\x1f\x8b":
        daten = gzip.decompress(daten)
    lagen = []
    for feld, art, lage in _felder(daten):
        if feld != 3:
            continue
        name, extent, keys, values, roh = "", 4096, [], [], []
        for f, a, v in _felder(lage):
            if f == 1:
                name = v.decode("utf-8", "replace")
            elif f == 2:
                roh.append(v)
            elif f == 3:
                keys.append(v.decode("utf-8", "replace"))
            elif f == 4:
                values.append(_wert(v))
            elif f == 5:
                extent = v
        features = []
        for fbuf in roh:
            typ, tags, geom = 0, [], []
            for f, a, v in _felder(fbuf):
                if f == 2:
                    tags = _gepackt(v)
                elif f == 3:
                    typ = v
                elif f == 4:
                    geom = _gepackt(v)
            props = {}
            for k, vi in zip(tags[::2], tags[1::2]):
                if k < len(keys) and vi < len(values):
                    props[keys[k]] = values[vi]
            features.append({"typ": typ, "props": props, "geom": _geometrie(geom)})
        lagen.append({"name": name, "extent": extent, "features": features})
    return lagen


# --- projection ---------------------------------------------------------------

def merc(lat, lon):
    """Web Mercator as fractions of the world (0..1)."""
    lat = max(-85.0511, min(85.0511, float(lat)))
    x = (float(lon) + 180.0) / 360.0
    y = (1.0 - math.log(math.tan(math.radians(lat)) + 1.0 / math.cos(math.radians(lat))) / math.pi) / 2.0
    return x, y


def _flaeche(ring):
    a = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        a += x1 * y2 - x2 * y1
    return a / 2.0


class _Fenster:
    """The square's place in the world: scale in world pixels and the
    top-left corner, from the route's bounding box."""

    def __init__(self, pts):
        xs = [x for seg in pts for x, _ in seg]
        ys = [y for seg in pts for _, y in seg]
        minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
        span = max(maxx - minx, maxy - miny)
        innen = GROESSE - 2 * RAND
        self.skala = innen / span if span > 0 else 256.0 * 2 ** 12       # world width in px
        self.skala = min(self.skala, 256.0 * 2 ** 17)
        self.zoom = math.log2(self.skala / 256.0)
        cx, cy = (minx + maxx) / 2 * self.skala, (miny + maxy) / 2 * self.skala
        self.x0, self.y0 = cx - GROESSE / 2, cy - GROESSE / 2
        self.zt = max(0, min(ZOOM_MAX_DATEN, math.ceil(self.zoom)))    # tile zoom
        self.kachel_px = self.skala / 2 ** self.zt                       # one tile in output px

    def px(self, p):
        return ((p[0] * self.skala - self.x0) * FAKTOR, (p[1] * self.skala - self.y0) * FAKTOR)

    def kacheln(self):
        n = 2 ** self.zt
        for tx in range(int(math.floor(self.x0 / self.kachel_px)),
                        int(math.floor((self.x0 + GROESSE - 1e-9) / self.kachel_px)) + 1):
            for ty in range(int(math.floor(self.y0 / self.kachel_px)),
                            int(math.floor((self.y0 + GROESSE - 1e-9) / self.kachel_px)) + 1):
                if 0 <= ty < n:
                    yield tx, ty, tx % n, ty

    def kachel_punkt(self, tx, ty, extent, gx, gy):
        return ((tx * self.kachel_px + gx / extent * self.kachel_px - self.x0) * FAKTOR,
                (ty * self.kachel_px + gy / extent * self.kachel_px - self.y0) * FAKTOR)


# --- painting -----------------------------------------------------------------

def _strassenbreite(kind, zoom):
    if kind == "highway":
        return 2.2 if zoom >= 8 else 1.6
    if kind == "major_road":
        return 1.5 if zoom >= 9 else 1.0
    if kind == "medium_road":
        return 1.1 if zoom >= 11 else 0
    if kind == "minor_road":
        return 0.9 if zoom >= 12.5 else 0
    return 0


def _vektor_hintergrund(zeichnen, fenster, kacheln_holen):
    """Paint the map from vector tiles. Returns False when a tile could not
    be fetched (the picture is then not worth keeping)."""
    komplett = True
    for tx, ty, kx, ky in fenster.kacheln():
        daten = kacheln_holen(fenster.zt, kx, ky)
        if daten is None:
            komplett = False
            continue
        if not daten:
            continue                                  # empty tile: open sea
        try:
            lagen = {l["name"]: l for l in mvt_dekodieren(daten)}
        except Exception:
            komplett = False
            continue

        def punkte(lage, ring):
            e = lage["extent"]
            return [fenster.kachel_punkt(tx, ty, e, gx, gy) for gx, gy in ring]

        def flaechen(lage, farbe, hinter, filt=None):
            for f in lage["features"]:
                if f["typ"] != 3 or (filt and not filt(f["props"])):
                    continue
                aussen = None
                for ring in f["geom"]:
                    if len(ring) < 4:
                        continue
                    a = _flaeche(ring)
                    if aussen is None:
                        aussen = a > 0
                    loch = (a > 0) != aussen
                    zeichnen.polygon(punkte(lage, ring), fill=hinter if loch else farbe)

        if "earth" in lagen:
            flaechen(lagen["earth"], PALETTE["land"], PALETTE["wasser"])
        for name in ("landuse", "natural"):
            if name in lagen:
                flaechen(lagen[name], PALETTE["wald"], PALETTE["land"],
                         lambda p: p.get("kind") in WALD_ARTEN)
        if "water" in lagen:
            flaechen(lagen["water"], PALETTE["wasser"], PALETTE["land"])
        if "roads" in lagen:
            for stufe in ("minor_road", "medium_road", "major_road", "highway"):
                breite = _strassenbreite(stufe, fenster.zoom)
                if not breite:
                    continue
                farbe = PALETTE[{"highway": "highway", "major_road": "major",
                                 "medium_road": "medium", "minor_road": "minor"}[stufe]]
                for f in lagen["roads"]["features"]:
                    if f["typ"] != 2 or f["props"].get("kind") != stufe:
                        continue
                    for linie in f["geom"]:
                        if len(linie) > 1:
                            zeichnen.line(punkte(lagen["roads"], linie), fill=farbe,
                                          width=max(1, round(breite * FAKTOR)), joint="curve")
    return komplett


def _route(zeichnen, fenster, pts):
    farbe = ROUTE + (255,)
    halo = HINTERGRUND + (170,)
    erster = letzter = None
    for seg in pts:
        raster = [fenster.px(p) for p in seg]
        if len(raster) > 1:
            zeichnen.line(raster, fill=halo, width=round(7 * FAKTOR), joint="curve")
            zeichnen.line(raster, fill=farbe, width=round(ROUTE_BREITE * FAKTOR), joint="curve")
        if erster is None:
            erster = raster[0]
        letzter = raster[-1]
    r = 4 * FAKTOR
    zeichnen.ellipse((erster[0] - r, erster[1] - r, erster[0] + r, erster[1] + r),
                     fill=(255, 255, 255, 255), outline=farbe, width=2 * FAKTOR)
    zeichnen.ellipse((letzter[0] - r, letzter[1] - r, letzter[0] + r, letzter[1] + r), fill=farbe)


def bild(segments, vektor_kacheln=None):
    """(image, complete). ``vektor_kacheln(z, x, y)`` returns the bytes of a
    Roundtrip tile (b"" for an empty one, None when unavailable); without it
    the route is drawn on the plain dark background."""
    pts = [[merc(p[0], p[1]) for p in seg if p and len(p) >= 2] for seg in (segments or [])]
    pts = [seg for seg in pts if seg]
    if not pts:
        leer = Image.new("RGB", (GROESSE, GROESSE), HINTERGRUND)
        d = ImageDraw.Draw(leer)
        m = GROESSE // 2
        d.ellipse((m - 8, m - 8, m + 8, m + 8), outline=tuple(int(c * 0.45) for c in ROUTE), width=2)
        return leer, True

    fenster = _Fenster(pts)
    gross = GROESSE * FAKTOR
    if vektor_kacheln is not None:
        lage = Image.new("RGB", (gross, gross), PALETTE["wasser"])
        komplett = _vektor_hintergrund(ImageDraw.Draw(lage), fenster, vektor_kacheln)
    else:
        lage = Image.new("RGB", (gross, gross), HINTERGRUND)
        komplett = True
    lage = lage.convert("RGBA")
    oben = Image.new("RGBA", (gross, gross), (0, 0, 0, 0))
    _route(ImageDraw.Draw(oben), fenster, pts)
    lage = Image.alpha_composite(lage, oben)
    return lage.resize((GROESSE, GROESSE), Image.LANCZOS).convert("RGB"), komplett
