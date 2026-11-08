"""Range per direction with the wind forecast, and the road area built from it.

1. direction_ranges(): for 16 bearings the car "drives out" in STEP_KM road
   steps. At every step the wind of the nearest forecast raster point at the
   expected arrival time is split into head/tail wind along the bearing; the
   air speed replaces the driving speed in the drag term of the own
   consumption model (bms_learn.speed_model), the forecast temperature goes
   into the cold term. Where the energy runs out is the range of that bearing.
2. compose(): Valhalla returns up to 4 road contours (distances); every
   bearing wedge takes the contour closest to its own range. The pieces are
   clipped to their wedges (Sutherland-Hodgman, local flat projection); the
   fill keeps all pieces, the outline leaves out the radial cut edges, so the
   map shows one boundary, not a pie.
"""

import json
import math

import bms_learn as bl

N_DIRS = 16
STEP_KM = 10.0
DETOUR = 1.25            # road km per straight-line km
WIND_AT_CAR = 0.7        # wind at car height vs. the 10 m forecast
MAX_LEVELS = 4           # Valhalla's default max contours per request
G = 9.81
DRIVE_EFF = 0.9          # battery → wheel when climbing
REGEN_EFF = 0.6          # share of the potential energy won back downhill
KY = 110.57              # km per degree latitude
KX0 = 111.32             # km per degree longitude at the equator


def bearings():
    return [i * 360.0 / N_DIRS for i in range(N_DIRS)]


def _xy(lat0, lon0, lat, lon):
    return ((lon - lon0) * KX0 * math.cos(math.radians(lat0)), (lat - lat0) * KY)


def _ll(lat0, lon0, x, y):
    return (lat0 + y / KY, lon0 + x / (KX0 * math.cos(math.radians(lat0))))


def wind_points(rows):
    """[{lat, lon, data}] from the wind_points table (data as stored)."""
    out = []
    for r in rows:
        try:
            out.append({"lat": r["lat"], "lon": r["lon"], "data": json.loads(r["data"])})
        except (ValueError, TypeError, KeyError):
            pass
    return out


def direction_ranges(remaining_kwh, model, temp_now, v_kmh, lat0, lon0, points, now, mass_kg=None):
    """Road km per bearing (list of N_DIRS), or None without model/energy.
    Topography: the terrain height of the raster points (Open-Meteo) gives
    the climb along each bearing — uphill costs m·g·Δh, downhill wins part
    of it back by recuperation."""
    import config
    import wind
    if not model or not remaining_kwh or remaining_kwh <= 0 or not v_kmh:
        return None
    mass = mass_kg or config.VEHICLE_MASS_KG
    pts = [(*_xy(lat0, lon0, p["lat"], p["lon"]), p["data"]) for p in points]

    def elev_at(x, y):
        if not pts:
            return None
        return min(pts, key=lambda q: (q[0] - x) ** 2 + (q[1] - y) ** 2)[2].get("elev")

    h_start = elev_at(0.0, 0.0)
    out = []
    for th in bearings():
        ux, uy = math.sin(math.radians(th)), math.cos(math.radians(th))
        left, road, h_prev = remaining_kwh, 0.0, h_start
        while left > 0 and road < 1500:
            mid = road + STEP_KM / 2
            x, y = ux * mid / DETOUR, uy * mid / DETOUR
            t = now + mid / v_kmh * 3600
            along, temp = 0.0, temp_now
            if pts:
                px, py, data = min(pts, key=lambda q: (q[0] - x) ** 2 + (q[1] - y) ** 2)
                w = wind.at(data, t)
                if w and w.get("kmh") is not None and w.get("dir") is not None:
                    to = (w["dir"] + 180.0) % 360.0          # forecast gives "from"
                    along = w["kmh"] * WIND_AT_CAR * math.cos(math.radians(to - th))
                    if w.get("temp") is not None:
                        temp = w["temp"]
            v_air = max(0.0, v_kmh - along)                  # tail wind: less air speed
            cold = max(0.0, bl.COLD_C - temp) if temp is not None else 0.0
            cons = model["a"] + model["b"] * (v_air / 100) ** 2 + model["c"] * cold
            need = max(cons, 5.0) * STEP_KM / 100
            h = data.get("elev") if pts else None
            if h is not None and h_prev is not None and h != h_prev:
                pot = mass * G * (h - h_prev) / 3.6e6           # kWh
                need += pot / DRIVE_EFF if pot > 0 else pot * REGEN_EFF
                need = max(need, 0.0)
            if h is not None:
                h_prev = h
            if need >= left:
                road += STEP_KM * left / need
                left = 0
            else:
                road += STEP_KM
                left -= need
        out.append(round(road))
    return out


def levels(dirs):
    """Up to MAX_LEVELS contour distances (km, rounded to 5) covering dirs."""
    lo, hi = min(dirs), max(dirs)
    if hi - lo < 10:
        return [int(round((lo + hi) / 2 / 5.0) * 5)]
    n = MAX_LEVELS
    return sorted({int(round((lo + (hi - lo) * i / (n - 1)) / 5.0) * 5) for i in range(n)})


# ── geometry ────────────────────────────────────────────────────────────────

def _clip(poly, a, b):
    """Keep the part of poly left of the directed line a→b (one SH pass)."""
    def inside(p):
        return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) >= 0

    def cut(p, q):
        dx1, dy1 = q[0] - p[0], q[1] - p[1]
        dx2, dy2 = b[0] - a[0], b[1] - a[1]
        den = dx1 * dy2 - dy1 * dx2
        if abs(den) < 1e-12:
            return q
        s = ((a[0] - p[0]) * dy2 - (a[1] - p[1]) * dx2) / den
        return (p[0] + s * dx1, p[1] + s * dy1)

    out = []
    for i, cur in enumerate(poly):
        prev = poly[i - 1]
        if inside(cur):
            if not inside(prev):
                out.append(cut(prev, cur))
            out.append(cur)
        elif inside(prev):
            out.append(cut(prev, cur))
    return out


def _wedge(th, half, r):
    def u(deg):
        return (math.sin(math.radians(deg)) * r, math.cos(math.radians(deg)) * r)
    # counter-clockwise in x/y (east/north): origin, right edge, middle, left edge
    return [(0.0, 0.0), u(th + half), u(th), u(th - half)]


def _on_ray(p, deg, eps=1e-6):
    ux, uy = math.sin(math.radians(deg)), math.cos(math.radians(deg))
    return abs(p[0] * uy - p[1] * ux) < eps and (p[0] * ux + p[1] * uy) >= -eps


def _outer_rings(feature):
    g = feature.get("geometry") or {}
    if g.get("type") == "Polygon":
        return [g["coordinates"][0]]
    if g.get("type") == "MultiPolygon":
        return [poly[0] for poly in g["coordinates"]]
    return []


def _bearing(p):
    return (math.degrees(math.atan2(p[0], p[1])) + 360.0) % 360.0


def _join(chains):
    """Link the outline pieces of neighbouring wedges: where one ends on a
    wedge ray and the next starts on the same ray (other contour), connect
    them, so the outline runs through with a small step instead of a gap."""
    def delta(a, b):
        return (b - a + 540.0) % 360.0 - 180.0
    arcs, rest = [], []
    for c in chains:
        if len(c) < 2:
            continue
        if delta(_bearing(c[0]), _bearing(c[-1])) < 0:
            c = c[::-1]                       # all arcs run clockwise
        (arcs if abs(delta(_bearing(c[0]), _bearing(c[-1]))) > 1.0 else rest).append(c)
    arcs.sort(key=lambda c: _bearing(c[0]))
    out = []
    for c in arcs:
        if out and abs(delta(_bearing(out[-1][-1]), _bearing(c[0]))) < 0.5:
            out[-1] = out[-1] + c
        else:
            out.append(list(c))
    if len(out) > 1 and abs(delta(_bearing(out[-1][-1]), _bearing(out[0][0]))) < 0.5:
        out[0] = out.pop() + out[0]           # across north
    if len(out) == 1 and abs(delta(_bearing(out[0][-1]), _bearing(out[0][0]))) < 0.5:
        out[0] = out[0] + [out[0][0]]         # closed loop
    return out + rest


def compose(isochrone, dirs, lat0, lon0):
    """Valhalla isochrone (contours = levels(dirs)) → GeoJSON with one fill
    feature (MultiPolygon) and one outline feature (MultiLineString)."""
    feats = (isochrone or {}).get("features") or []
    by_level = {}
    for f in feats:
        c = (f.get("properties") or {}).get("contour")
        if c is not None:
            by_level[float(c)] = [[_xy(lat0, lon0, la, lo) for lo, la in ring[:-1]] for ring in _outer_rings(f)]
    if not by_level:
        return None
    lv = sorted(by_level)
    r = max(lv) * 2.0                    # wedge reaches beyond every contour
    half = 180.0 / N_DIRS
    fills, lines = [], []
    for th, want in zip(bearings(), dirs):
        level = min(lv, key=lambda c: abs(c - want))
        wedge = _wedge(th, half, r)
        for ring in by_level[level]:
            piece = ring
            for i in range(len(wedge)):
                if len(piece) < 3:
                    break
                piece = _clip(piece, wedge[i], wedge[(i + 1) % len(wedge)])
            if len(piece) < 3:
                continue
            fills.append(piece)
            # outline: the ring without the edges lying on the wedge's rays
            chain = []
            n = len(piece)
            for i in range(n):
                p, q = piece[i], piece[(i + 1) % n]
                cut_edge = any(_on_ray(p, d) and _on_ray(q, d) for d in (th - half, th + half))
                if cut_edge:
                    if len(chain) > 1:
                        lines.append(chain)
                    chain = []
                else:
                    if not chain:
                        chain = [p]
                    chain.append(q)
            if len(chain) > 1:
                lines.append(chain)

    lines = _join(lines)

    def ll(pts, close=False):
        c = [[round(lo, 5), round(la, 5)] for la, lo in (_ll(lat0, lon0, x, y) for x, y in pts)]
        return c + [c[0]] if close else c

    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"kind": "fill", "levels": lv},
         "geometry": {"type": "MultiPolygon", "coordinates": [[ll(p, True)] for p in fills]}},
        {"type": "Feature", "properties": {"kind": "line"},
         "geometry": {"type": "MultiLineString", "coordinates": [ll(c) for c in lines]}},
    ]}
