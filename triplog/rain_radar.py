"""Rain on the map, for the range and for the read API (L-004, L-005, E-002).

Two radar sources, one data layer:

1. Deutscher Wetterdienst (DWD), WMS layer ``dwd:Niederschlagsradar``: the
   German radar composite in mm/h, every 5 minutes, with analysis and a
   nowcast up to two hours ahead. The WMS only serves coloured pictures (no
   own styles), so the colours of its legend are turned back into mm/h.
   Where the composite has no data (grey) or the place lies outside its grid,
   the second source steps in.
2. RainViewer, the free weather-maps API: radar mosaics of the last two hours
   in 10-minute steps, at most zoom 7, one colour scheme ("Universal Blue",
   colour → dBZ table below, dBZ → mm/h by Marshall-Palmer). No nowcast.

Both are folded into the same fixed raster: cells of CELL_KM, anchored on the
globe like the wind raster (rows every CELL_KM in latitude, columns every
CELL_KM along each row), each with a grade 1 light (< 1 mm/h), 2 moderate
(1–5) or 3 heavy (> 5); dry cells are left out. The map, the range and the
read API only ever use this raster, so it makes no difference which source
a cell came from.

Area: the same as the wind arrows (the stored range area's centre and radius
plus one wind raster cell). The services are only asked for fixed web map
tiles (zoom TILE_Z) that cover that area, never for the car's own position.

Frames every FRAME_MIN minutes, on the quarter hour, from now (PAST_MIN = 0) to
AHEAD_MIN ahead (DWD; the RainViewer cells only for now: it has no forecast).

Load on the services (their terms: DWD open data under GeoNutzV with the
source named, fair use; RainViewer's free API for personal, non-commercial
use, tiles to be cached, attribution "RainViewer"):
- the DWD is asked for one picture of its whole composite per frame time –
  the same number of requests as for a small area, but every place in its
  reach is covered at once, wherever the car is or goes;
- the measured picture of now is fetched once per quarter hour. While rain was
  asked for in the last KEEP_DAYS days, a keeper fetches it (96 a day), so
  the map shows the rain of now at once; it comes back by itself after a
  restart (last use kept in the settings);
- the eight forecast pictures only while someone looked in the last two
  hours (then the keeper has the whole raster ready) or when asked for, at
  most once per quarter hour;
- RainViewer only for cells outside the DWD composite, its tiles kept 3 h;
  its frame list at most every 5 min; the coverage mask once a day from a
  picture fetched anyway;
- after an error the source rests FAIL_S.
"""

import io
import json
import logging
import math
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import config
from functools import lru_cache

log = logging.getLogger("triplog.rain")

CELL_KM = 5.0
LAT_STEP = CELL_KM / 111.2
TILE_Z = 7                     # RainViewer serves no finer zoom for free; DWD asked on the same tiles
FRAME_MIN = 15
PAST_MIN = 0                   # the map shows now and what comes, not what was (18.11.2026)
AHEAD_MIN = 120
STEP_MIN = 5                   # DWD composite cadence
LAG_MIN = 10                   # the newest DWD picture is a few minutes old
MAX_TILES = 64
LEVELS_MMH = (0.1, 1.0, 5.0)
LEVEL_TEXT = {"1": "light, 0.1–1 mm/h", "2": "moderate, 1–5 mm/h", "3": "heavy, over 5 mm/h"}
FAIL_S = 10 * 60
UA = {"User-Agent": "IDmate"}

DWD_LAYER = "dwd:Niederschlagsradar"
# Outer box of the composite grid (EPSG:4326, from the capabilities)
DWD_BOX = (45.68, 1.46, 56.22, 18.74)
# Legend of the layer: colour → lower edge in mm/h ("Keine Daten" separately)
DWD_COLOURS = [
    ((0x33, 0xFF, 0xFF), 0.1), ((0x1A, 0xCC, 0x9A), 0.2), ((0x01, 0x99, 0x34), 0.4),
    ((0x4D, 0xB3, 0x1B), 1.0), ((0x99, 0xCC, 0x01), 2.0), ((0xCC, 0xE6, 0x01), 3.0),
    ((0xFF, 0xFF, 0x01), 5.0), ((0xFF, 0xC4, 0x01), 7.5), ((0xFF, 0x89, 0x01), 10.0),
    ((0xFF, 0x45, 0x01), 15.0), ((0xFE, 0x00, 0x00), 30.0), ((0xE5, 0x00, 0x4C), 45.0),
    ((0xCC, 0x00, 0x98), 75.0), ((0x66, 0x00, 0xCB), 100.0), ((0x00, 0x00, 0xFE), 150.0)]
DWD_NODATA = (0x7E, 0x7E, 0x7E)

# RainViewer "Universal Blue", rain, from dBZ -10 up to 75 (rainviewer_api_colors_table.csv)
RV_DBZ0 = -10
RV_COLOURS = """
63615914 66635a19 69665c1e 6c685d24 6f6b5f29 726e612e 75706234 78736439 7c75653e 7f786744
827b6949 857d6a4e 88806c54 8b826d59 8e856f5e 92887164 9e93756e aa9e7978 b6a97e82 c2b4828c
cec08796 d2c48ba0 d6c88faa dacc93b4 ded097be 88ddeeff 6cd1ebff 51c5e8ff 36bae5ff 1baee2ff
00a3e0ff 009ad5ff 0091caff 0088bfff 007fb4ff 0077aaff 0070a3ff 00699cff 006295ff 005b8eff
005588ff 005180ff 004e78ff 004a70ff 004768ff ffee00ff ffe000ff ffd200ff ffc500ff ffb700ff
ffaa00ff ff9f00ff ff9500ff ff8b00ff ff8100ff ff4400ff f23600ff e62800ff d91b00ff cd0d00ff
c10000ff a80000ff 8f0000ff 760000ff 5d0000ff ffaaffff ff9fffff ff95ffff ff8bffff ff81ffff
ff77ffff ff6cffff ff62ffff ff58ffff ff4effff
""".split()
RV_TABLE = [(tuple(int(h[i:i + 2], 16) for i in (0, 2, 4, 6)), RV_DBZ0 + k) for k, h in enumerate(RV_COLOURS)]

_lock = threading.Lock()
_cache = {}                    # key → result
_last = None                   # newest result (for the range)
_mask = {"t": 0.0, "img": None}
_down_until = {"dwd": 0.0, "rainviewer": 0.0}
_rv_meta = {"t": 0.0, "data": None}
_tiles = {}                    # RainViewer tile cache: url → (time, image); the pictures never change
_dwd_pics = {}                 # whole-composite DWD pictures: time (measured) or (time, now) → image
_pics_lock = threading.Lock()
_used = {"t": 0.0, "keeper": False}   # last time someone asked for rain; the keeper thread runs


def sources():
    return [s for s in (x.strip() for x in config.RAIN_SOURCE.split(",")) if s in ("dwd", "rainviewer")]


def enabled():
    return bool(sources())


# ── grades ──────────────────────────────────────────────────────────────────

def level_mmh(mmh):
    """Grade 0–3 for a rate in mm/h."""
    if mmh is None:
        return None
    return sum(1 for edge in LEVELS_MMH if mmh >= edge)


def dbz_to_mmh(dbz):
    """Marshall-Palmer, Z = 200 · R^1.6."""
    return (10 ** (dbz / 10.0) / 200.0) ** (1 / 1.6)


@lru_cache(maxsize=8192)
def dwd_pixel(p):
    """DWD pixel (r, g, b, a) → mm/h (0.0 dry), None for "no data"."""
    r, g, b, a = p
    if a == 0:
        return 0.0
    if abs(r - DWD_NODATA[0]) < 8 and abs(g - DWD_NODATA[1]) < 8 and abs(b - DWD_NODATA[2]) < 8:
        return None
    if a < 200:
        return None                    # blended edge: leave it to the other source
    best = min(DWD_COLOURS, key=lambda c: (c[0][0] - r) ** 2 + (c[0][1] - g) ** 2 + (c[0][2] - b) ** 2)
    return best[1]


@lru_cache(maxsize=8192)
def rv_pixel(p):
    """RainViewer pixel (r, g, b, a) → mm/h (0.0 dry)."""
    if p[3] == 0:
        return 0.0
    best = min(RV_TABLE, key=lambda c: sum((c[0][i] - p[i]) ** 2 for i in range(4)))
    return dbz_to_mmh(best[1])


# ── raster and tiles ────────────────────────────────────────────────────────

def _dist_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, a)))


def _lon_step(lat):
    return CELL_KM / (111.2 * max(0.01, math.cos(math.radians(lat))))


def cell_of(lat, lon):
    """Index (row, column) of the raster cell that holds a point."""
    i = round(lat / LAT_STEP)
    la = i * LAT_STEP
    return i, round(lon / _lon_step(la))


def cell_centre(i, j):
    la = i * LAT_STEP
    return round(la, 4), round(((j * _lon_step(la) + 180) % 360) - 180, 4)


def cells(lat, lon, radius_km):
    """Raster cells (i, j, lat, lon) within radius_km of a position."""
    out = []
    i0 = math.floor((lat - radius_km / 111.2) / LAT_STEP)
    i1 = math.ceil((lat + radius_km / 111.2) / LAT_STEP)
    for i in range(i0, i1 + 1):
        la = i * LAT_STEP
        if abs(la) > 85:
            continue
        step = _lon_step(la)
        k = 111.2 * math.cos(math.radians(la))
        j0 = math.floor((lon - radius_km / k) / step)
        j1 = math.ceil((lon + radius_km / k) / step)
        for j in range(j0, j1 + 1):
            c = cell_centre(i, j)
            if _dist_km(lat, lon, c[0], c[1]) <= radius_km:
                out.append((i, j, c[0], c[1]))
    return out


def tile_xy(lat, lon, z=TILE_Z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    la = math.radians(max(-85.0, min(85.0, lat)))
    y = (1.0 - math.asinh(math.tan(la)) / math.pi) / 2.0 * n
    return x, y                        # fractional


def tiles_for(lat, lon, radius_km):
    """Fixed tile range (x0, y0, x1, y1) covering the circle."""
    dlat = radius_km / 111.2
    dlon = radius_km / (111.2 * max(0.05, math.cos(math.radians(min(85.0, abs(lat) + dlat)))))
    xa, ya = tile_xy(lat + dlat, lon - dlon)
    xb, yb = tile_xy(lat - dlat, lon + dlon)
    x0, y0, x1, y1 = int(xa), int(ya), int(xb), int(yb)
    while (x1 - x0 + 1) * (y1 - y0 + 1) > MAX_TILES:        # keep it bounded: trim the far edges
        if x1 - x0 >= y1 - y0:
            x0, x1 = x0 + 1, x1 - 1
        else:
            y0, y1 = y0 + 1, y1 - 1
    return x0, y0, x1, y1


def _merc(lat, lon):
    r = 6378137.0
    la = math.radians(max(-85.0, min(85.0, lat)))
    return r * math.radians(lon), r * math.log(math.tan(math.pi / 4 + la / 2))


def _tile_merc(x, y, z=TILE_Z):
    """EPSG:3857 bounds of tile corner (top left of tile x, y)."""
    o = 20037508.342789244
    size = 2 * o / 2 ** z
    return -o + x * size, o - y * size


# ── fetching ────────────────────────────────────────────────────────────────

def _get(url, data=None, timeout=30):
    import api_usage
    api_usage.count("dwd" if "dwd.de" in url or url.startswith(config.RAIN_DWD_URL) else "rainviewer")
    req = urllib.request.Request(url, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.headers.get("Content-Type", ""), r.read()


def _image(blob):
    from PIL import Image
    return Image.open(io.BytesIO(blob)).convert("RGBA")


def _dwd_map(bbox, width, height, when=None):
    """One coloured DWD picture of an EPSG:3857 box; None when the service
    has nothing for that time."""
    q = {"service": "WMS", "version": "1.3.0", "request": "GetMap", "layers": DWD_LAYER,
         "styles": "", "crs": "EPSG:3857", "bbox": ",".join(f"{v:.1f}" for v in bbox),
         "width": width, "height": height, "format": "image/png", "transparent": "true"}
    if when:
        q["time"] = when.strftime("%Y-%m-%dT%H:%M:00.000Z")
    ctype, blob = _get(config.RAIN_DWD_URL + "?" + urllib.parse.urlencode(q), timeout=40)
    if "image" not in ctype:
        return None                    # service exception (e.g. time out of range)
    return _image(blob)


DWD_PX_M = 2400.0               # metres (EPSG:3857) per pixel of the whole-composite picture ≈ 1.5 km at 50° N


def _dwd_frame_box():
    """The whole composite as one picture: EPSG:3857 box and size."""
    x0, y0 = _merc(DWD_BOX[0], DWD_BOX[1])
    x1, y1 = _merc(DWD_BOX[2], DWD_BOX[3])
    return (x0, y0, x1, y1), int((x1 - x0) / DWD_PX_M), int((y1 - y0) / DWD_PX_M)


def _mask_from(img, box, now):
    """Where the DWD composite has radar at all: every pixel not reachable
    from the edge of the whole-composite picture over empty pixels (the
    radar area is ringed by grey "no data"). Worked out on a half-size copy
    of a picture that is fetched anyway, once a day."""
    small = img.resize((img.width // 2, img.height // 2))
    w, h = small.size
    px = small.load()
    outside = bytearray(w * h)
    stack = [(x, y) for x in range(w) for y in (0, h - 1)] + [(x, y) for y in range(h) for x in (0, w - 1)]
    while stack:
        x, y = stack.pop()
        k = y * w + x
        if outside[k] or px[x, y][3] != 0:
            continue
        outside[k] = 1
        if x > 0: stack.append((x - 1, y))
        if x < w - 1: stack.append((x + 1, y))
        if y > 0: stack.append((x, y - 1))
        if y < h - 1: stack.append((x, y + 1))
    _mask.update({"t": now, "img": outside, "w": w, "h": h, "box": box})
    return _mask


def coverage(now=None, dwd_map=None):
    """The coverage mask (see _mask_from), renewed once a day from the newest
    measured picture."""
    now = now or time.time()
    if _mask["img"] is not None and now - _mask["t"] < 86400:
        return _mask
    base, _t = _frame_times(now)
    img = _dwd_frame(base, base, dwd_map or _dwd_map)
    if img is None:
        return _mask
    return _mask_from(img, _dwd_frame_box()[0], now)


def _covered(mask, lat, lon):
    if mask.get("img") is None:
        return False
    x0, y0, x1, y1 = mask["box"]
    mx, my = _merc(lat, lon)
    if not (x0 <= mx < x1 and y0 <= my < y1):
        return False
    px = min(mask["w"] - 1, int((mx - x0) / (x1 - x0) * mask["w"]))
    py = min(mask["h"] - 1, int((y1 - my) / (y1 - y0) * mask["h"]))
    return not mask["img"][py * mask["w"] + px]


def _dwd_frame(t, base, dwd_map):
    """Whole-composite picture of one frame time. Measured ones (t ≤ base)
    are fetched once and kept until the next quarter hour; a forecast once per
    quarter hour (it changes with every new analysis)."""
    key = t if t <= base else (t, base)
    with _pics_lock:
        if key in _dwd_pics:
            return _dwd_pics[key]
    box, w, h = _dwd_frame_box()
    try:
        img = dwd_map(box, w, h, datetime.fromtimestamp(t, timezone.utc))
    except Exception as e:
        log.warning("rain: DWD picture failed (%s)", e)
        return None
    if img is not None:
        with _pics_lock:
            _dwd_pics[key] = img
            for k in list(_dwd_pics):
                kt, kb = (k, None) if isinstance(k, int) else k
                if kt < base - PAST_MIN * 60 or (kb is not None and kb != base):
                    _dwd_pics.pop(k, None)
    return img


def _rv_frames(now):
    """RainViewer past frames [(unix time, url prefix)], refreshed every 5 min."""
    if _rv_meta["data"] is None or now - _rv_meta["t"] > 300:
        _ctype, blob = _get(config.RAIN_RV_URL, timeout=20)
        d = json.loads(blob.decode("utf-8"))
        host = d.get("host") or "https://tilecache.rainviewer.com"
        _rv_meta.update({"t": now, "data": [(f["time"], host + f["path"])
                                             for f in (d.get("radar") or {}).get("past") or []]})
    return _rv_meta["data"]


def _rv_tile(prefix, x, y, now):
    url = f"{prefix}/256/{TILE_Z}/{x}/{y}/2/0_0.png"
    hit = _tiles.get(url)
    if hit and now - hit[0] < 3 * 3600:
        return hit[1]
    _ctype, blob = _get(url, timeout=20)
    img = _image(blob)
    _tiles[url] = (now, img)
    for k in [k for k, v in _tiles.items() if now - v[0] > 3 * 3600]:
        _tiles.pop(k, None)
    return img


def _rv_tile_kept(prefix, x, y, now):
    hit = _tiles.get(f"{prefix}/256/{TILE_Z}/{x}/{y}/2/0_0.png")
    return hit[1] if hit else None


# ── the raster per frame ────────────────────────────────────────────────────

def _samples(lat, lon):
    """Centre and four points around it inside one cell."""
    d = LAT_STEP / 3
    e = _lon_step(lat) / 3
    return [(lat, lon), (lat + d, lon), (lat - d, lon), (lat, lon + e), (lat, lon - e)]


def _frame_times(now):
    """(now frame, all frame times): on the quarter hour, the newest quarter
    for which the DWD composite is surely there. A new set every 15 min."""
    slot = FRAME_MIN * 60
    base = (int(now) - LAG_MIN * 60) // slot * slot
    times = [base + k * slot for k in range(-PAST_MIN // FRAME_MIN, AHEAD_MIN // FRAME_MIN + 1)]
    return base, times


def compute(lat, lon, radius_km, now=None, dwd_map=None, rv_frames=None, rv_tile=None, mask=None,
            measured_only=False):
    """The rain raster for an area: {cell_km, radius, levels, frames: [{t,
    kind, src, cells: [[lat, lon, level], …]}]}. measured_only: only now (from pictures already kept, so it is there at once), without the
    forecast. Fetch functions can be handed in (tests)."""
    now = now or time.time()
    dwd_map = dwd_map or _dwd_map
    rv_frames = rv_frames or _rv_frames
    rv_tile = rv_tile or _rv_tile
    srcs = sources()
    radius = radius_km
    grid = cells(lat, lon, radius)
    (ox0, oy0, ox1, oy1), w, h = _dwd_frame_box()
    base, times = _frame_times(now)
    if measured_only:
        times = [t for t in times if t <= base]

    in_dwd = {}
    if "dwd" in srcs and now >= _down_until["dwd"]:
        try:
            m = mask if mask is not None else coverage(now, dwd_map)
            in_dwd = {(i, j): _covered(m, la, lo) for i, j, la, lo in grid}
        except Exception as e:
            _down_until["dwd"] = now + FAIL_S
            log.warning("rain: DWD coverage failed (%s)", e)

    rv_list = []
    if measured_only:
        # quick answer: RainViewer only from what is kept, no request
        rv_list = _rv_meta["data"] or []
        rv_tile = _rv_tile_kept
    elif "rainviewer" in srcs and now >= _down_until["rainviewer"] and not all(in_dwd.get((c[0], c[1])) for c in grid):
        try:
            rv_list = rv_frames(now)
        except Exception as e:
            _down_until["rainviewer"] = now + FAIL_S
            log.warning("rain: RainViewer list failed (%s)", e)

    # sample positions are the same for every frame: work them out once
    dwd_uv, rv_pt = {}, {}
    for i, j, la, lo in grid:
        uv, pt = [], []
        for sla, slo in _samples(la, lo):
            mx, my = _merc(sla, slo)
            u = int((mx - ox0) / (ox1 - ox0) * w)
            v = int((oy1 - my) / (oy1 - oy0) * h)
            if 0 <= u < w and 0 <= v < h:
                uv.append((u, v))
            fx, fy = tile_xy(sla, slo)
            tx, ty = int(fx), int(fy)
            pt.append((tx, ty, min(255, int((fx - tx) * 256)), min(255, int((fy - ty) * 256))))
        dwd_uv[(i, j)] = uv
        rv_pt[(i, j)] = pt

    # fetch what is not kept yet (a few workers): the DWD pictures of the
    # frames, the RainViewer tiles of the cells outside the DWD composite
    from concurrent.futures import ThreadPoolExecutor
    use_dwd = any(in_dwd.values())

    def near_rv(t):
        if not rv_list or t > base:
            return None
        f = min(rv_list, key=lambda f: abs(f[0] - t))
        return f if abs(f[0] - t) <= 8 * 60 else None

    def one_rv(key):
        try:
            return rv_tile(key[0], key[1], key[2], now)
        except Exception as e:
            log.warning("rain: RainViewer tile failed (%s)", e)
            return None
    rv_keys = set()
    outside = [c for c in grid if not in_dwd.get((c[0], c[1]))]
    for t in times:
        f = near_rv(t)
        if f:
            rv_keys |= {(f[1], tx, ty) for c in outside for tx, ty, _u, _v in rv_pt[(c[0], c[1])]}
    dwd_imgs, rv_imgs = {}, {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        if use_dwd and now >= _down_until["dwd"]:
            dwd_imgs = dict(zip(times, ex.map(lambda t: _dwd_frame(t, base, dwd_map), times)))
        rv_imgs = dict(zip(rv_keys, ex.map(one_rv, list(rv_keys))))

    frames = []
    for t in times:
        when = datetime.fromtimestamp(t, timezone.utc)
        kind = "past" if t < base else ("now" if t == base else "forecast")
        got, used = {}, []
        todo = list(grid)
        # 1. DWD
        img = dwd_imgs.get(t)
        if img is not None:
            px = img.load()
            rest = []
            for i, j, la, lo in todo:
                if not in_dwd.get((i, j)):
                    rest.append((i, j, la, lo))
                    continue
                best, ok = 0.0, bool(dwd_uv[(i, j)])
                for u, v in dwd_uv[(i, j)]:
                    val = dwd_pixel(tuple(px[u, v]))
                    if val is None:
                        ok = False
                        break
                    best = max(best, val)
                if ok:
                    got[(i, j)] = level_mmh(best)
                else:
                    rest.append((i, j, la, lo))
            todo = rest
            used.append("dwd")
        # 2. RainViewer for the rest (now only: it has no forecast)
        near = near_rv(t)
        if todo and near:
            imgs = {(k[1], k[2]): v for k, v in rv_imgs.items() if k[0] == near[1]}
            n_before = len(got)
            for i, j, la, lo in todo:
                best, seen = 0.0, False
                for tx, ty, pu, pv in rv_pt[(i, j)]:
                    if (tx, ty) not in imgs:
                        imgs[(tx, ty)] = one_rv((near[1], tx, ty))
                    img = imgs[(tx, ty)]
                    if img is None:
                        continue
                    seen = True
                    best = max(best, rv_pixel(tuple(img.getpixel((pu, pv)))))
                if seen:
                    got[(i, j)] = level_mmh(best)
            if len(got) > n_before:
                used.append("rainviewer")
        rain = [[*cell_centre(i, j), lv] for (i, j), lv in got.items() if lv]
        frames.append({"t": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": kind, "src": used,
                       "cells": sorted(rain), "_known": got, "_ts": t})
    return {"cell_km": CELL_KM, "radius": round(radius), "levels": LEVEL_TEXT, "frames": frames,
            "_center": (lat, lon), "_base": base, "_complete": not measured_only}


def public(res):
    """The result without the internal keys (map, read API). complete is
    false while the forecast is still being fetched (ask again shortly)."""
    if not res:
        return {"cell_km": CELL_KM, "radius": 0, "levels": LEVEL_TEXT, "frames": [], "complete": False}
    return {"cell_km": res["cell_km"], "radius": res["radius"], "levels": res["levels"],
            "complete": res.get("_complete", True),
            "frames": [{k: v for k, v in f.items() if not k.startswith("_")} for f in res["frames"]
                       if f["src"]]}


KEEP_DAYS = 3                  # the keeper fetches the picture of now while rain was asked for lately
ACTIVE_S = 2 * 3600            # … and the whole raster with the forecast while someone looked in the last 2 h
KEEP_EVERY_S = 60              # look once a minute whether a new quarter hour is there (asks only then)


def _used_store(t=None):
    """When rain was last asked for, kept in the settings table so the
    keeper comes back by itself after a restart (an update via Watchtower
    restarts the container). Written at most every 10 minutes."""
    try:
        import sqlite3
        db = sqlite3.connect(config.DB_PATH, timeout=5)
        try:
            if t is None:
                row = db.execute("SELECT value FROM settings WHERE key = 'rain_last_used'").fetchone()
                return float(row[0]) if row and row[0] else 0.0
            db.execute("INSERT INTO settings (key, value) VALUES ('rain_last_used', ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(int(t)),))
            db.commit()
        finally:
            db.close()
    except Exception as e:
        log.debug("rain: last use not stored: %s", e)
    return 0.0


def _keeper():
    """Keeps the rain ready, so the map shows it at once:
    - while rain was asked for in the last KEEP_DAYS days: the measured DWD
      picture of now, one per quarter hour (96 a day), and the coverage;
    - while someone looked in the last ACTIVE_S: also the whole raster of the
      last area with the forecast (8 more pictures per quarter hour), so even
      the forecast is there when the map opens.
    Looks once a minute; a picture already kept is not asked for again."""
    while True:
        now = time.time()
        if now - _used["t"] > KEEP_DAYS * 86400 or not enabled():
            _used["keeper"] = False
            return
        try:
            if "dwd" in sources() and now >= _down_until["dwd"]:
                base, _times = _frame_times(now)
                _dwd_frame(base, base, _dwd_map)
                coverage(now)
            args = _used.get("args")
            if args and now - _used["t"] < ACTIVE_S:
                area(*args, now=now, wait=True, touch=False)
        except Exception as e:
            log.warning("rain: keeper: %s", e)
        time.sleep(KEEP_EVERY_S)


def _start_keeper():
    with _lock:
        if _used["keeper"]:
            return
        _used["keeper"] = True
    threading.Thread(target=_keeper, name="rain-keeper", daemon=True).start()


def start(args=None):
    """At server start: the keeper comes back if rain was asked for in the
    last KEEP_DAYS days (args: the last area, if known)."""
    if not enabled():
        return
    t = _used_store()
    if t and time.time() - t < KEEP_DAYS * 86400:
        _used["t"] = t
        if args:
            _used["args"] = args
        _start_keeper()


_busy = {"on": False}


def area(lat, lon, radius_km, now=None, wait=False, touch=True):
    """The rain raster for the area around the range: same centre and radius
    as the wind arrows. Now comes at once from the picture the
    keeper holds (complete false); the forecast follows in the background
    within seconds (ask again). Recomputed with the next quarter hour or when
    the area needs other tiles."""
    import wind
    if not enabled():
        return None
    now = now or time.time()
    if touch:                          # someone asked (map, read API, range): remember for the keeper
        if now - _used.get("stored", 0) > 600:
            _used["stored"] = now
            _used_store(now)
        _used["t"] = max(_used["t"], now)
        _used["args"] = (lat, lon, radius_km)
        if not wait:
            _start_keeper()
    radius = radius_km + wind.GRID_KM
    base, _t = _frame_times(now)
    # same tiles, same quarter hour → same raster: a car driving on does not
    # cause new requests until its area needs other tiles
    key = (tiles_for(lat, lon, radius), base)

    def keep(res):
        global _last
        with _lock:
            if res.get("_complete") or key not in _cache:
                for k in [k for k in _cache if k != key]:
                    _cache.pop(k, None)
                _cache[key] = res
                _last = res

    def work():
        try:
            keep(compute(lat, lon, radius, now))
        except Exception as e:
            log.warning("rain: %s", e, exc_info=True)
        finally:
            _busy["on"] = False
    with _lock:
        hit = _cache.get(key)
        if hit is not None and (hit.get("_complete") or _busy["on"]):
            return hit
        start = not _busy["on"]
        if start:
            _busy["on"] = True
    if not start:
        return _cache.get(key)
    if wait:
        work()
        return _cache.get(key)
    try:
        quick = compute(lat, lon, radius, now, measured_only=True)
        keep(quick)
    except Exception as e:
        log.warning("rain: %s", e, exc_info=True)
        quick = None
    threading.Thread(target=work, name="rain-radar", daemon=True).start()
    return quick


def level_at(lat, lon, t):
    """Grade at a place and time from the newest raster (for the range):
    0–3, or None when the raster does not cover it (no data, too far ahead)."""
    res = _last
    if not res:
        return None
    best = None
    for f in res["frames"]:
        ft = f["_ts"]
        if abs(ft - t) <= FRAME_MIN * 30 and f["src"]:
            if best is None or abs(ft - t) < abs(best[0] - t):
                best = (ft, f)
    if not best:
        return None
    return best[1]["_known"].get(cell_of(lat, lon))
