"""Range per direction with the wind forecast, and the road area built from it.

1. direction_ranges(): for N_DIRS bearings the car "drives out" in STEP_KM road
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

N_DIRS = 32              # bearings; wedge 11.25°, ~40 km wide at 200 km
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


def _outer_rings(feature):
    g = feature.get("geometry") or {}
    if g.get("type") == "Polygon":
        return [g["coordinates"][0]]
    if g.get("type") == "MultiPolygon":
        return [poly[0] for poly in g["coordinates"]]
    return []


CELL_KM_MIN = 1.0
MAX_CELLS = 450
MIN_LOOP_KM2 = 30.0


def _rasterize(polys):
    """Union of polygons (local km) on a grid of cells → (grid rows, x0, y0, cell)."""
    xs = [p[0] for poly in polys for p in poly]
    ys = [p[1] for poly in polys for p in poly]
    x0, y0 = min(xs), min(ys)
    cell = max(CELL_KM_MIN, max(max(xs) - x0, max(ys) - y0) / MAX_CELLS)
    x0, y0 = x0 - 2 * cell, y0 - 2 * cell
    nx = int((max(xs) - x0) / cell) + 3
    ny = int((max(ys) - y0) / cell) + 3
    grid = [bytearray(nx) for _ in range(ny)]
    for poly in polys:
        rows = {}
        n = len(poly)
        for i in range(n):
            (ax, ay), (bx, by) = poly[i], poly[(i + 1) % n]
            if ay == by:
                continue
            if ay > by:
                ax, ay, bx, by = bx, by, ax, ay
            j0 = math.ceil((ay - y0) / cell - 0.5)
            j1 = math.ceil((by - y0) / cell - 0.5) - 1
            for j in range(max(j0, 0), min(j1, ny - 1) + 1):
                yc = y0 + (j + 0.5) * cell
                rows.setdefault(j, []).append(ax + (yc - ay) * (bx - ax) / (by - ay))
        for j, cross in rows.items():
            cross.sort()
            for k in range(0, len(cross) - 1, 2):
                i0 = max(0, math.ceil((cross[k] - x0) / cell - 0.5))
                i1 = min(nx - 1, math.ceil((cross[k + 1] - x0) / cell - 0.5) - 1)
                if i1 >= i0:
                    grid[j][i0:i1 + 1] = b"\x01" * (i1 - i0 + 1)
    return grid, x0, y0, cell


# marching squares: corners a (i,j) b (i+1,j) c (i+1,j+1) d (i,j+1), y up;
# edge midpoints on a doubled integer grid; saddles stay apart.
_MS = {1: [("L", "B")], 2: [("B", "R")], 3: [("L", "R")], 4: [("R", "T")],
       5: [("L", "B"), ("R", "T")], 6: [("B", "T")], 7: [("L", "T")], 8: [("T", "L")],
       9: [("B", "T")], 10: [("B", "R"), ("T", "L")], 11: [("R", "T")], 12: [("R", "L")],
       13: [("B", "R")], 14: [("L", "B")]}


_EDGE_CORNERS = {"B": ("a", "b"), "R": ("b", "c"), "T": ("c", "d"), "L": ("d", "a")}


def _outline(grid):
    """Closed boundary loops of the filled cells, inside on the left (outer
    loops counter-clockwise), in doubled grid coordinates."""
    ny, nx = len(grid), len(grid[0])
    nxt = {}
    for j in range(ny - 1):
        r0, r1 = grid[j], grid[j + 1]
        for i in range(nx - 1):
            a, b, c, d = r0[i], r0[i + 1], r1[i + 1], r1[i]
            case = a | b << 1 | c << 2 | d << 3
            if case in (0, 15):
                continue
            mid = {"B": (2 * i + 1, 2 * j), "R": (2 * i + 2, 2 * j + 1),
                   "T": (2 * i + 1, 2 * j + 2), "L": (2 * i, 2 * j + 1)}
            corner_pt = {"a": (2 * i, 2 * j), "b": (2 * i + 2, 2 * j), "c": (2 * i + 2, 2 * j + 2), "d": (2 * i, 2 * j + 2)}
            corners = {corner_pt["a"]: a, corner_pt["b"]: b, corner_pt["c"]: c, corner_pt["d"]: d}
            for e1, e2 in _MS[case]:
                p, q = mid[e1], mid[e2]
                # reference corner: the one both edges share, or for opposite
                # edges a fixed one on one side; inside must end up on the left
                shared = set(_EDGE_CORNERS[e1]) & set(_EDGE_CORNERS[e2])
                ref = next(iter(shared)) if shared else ("a" if {e1, e2} in ({"L", "R"}, {"B", "T"}) else None)
                k = corner_pt[ref]
                cross = (q[0] - p[0]) * (k[1] - p[1]) - (q[1] - p[1]) * (k[0] - p[0])
                inside = corners[k]
                if (cross > 0) != bool(inside):
                    p, q = q, p
                nxt[p] = q
    loops = []
    while nxt:
        start, cur = next(iter(nxt.items()))
        loop = [start]
        del nxt[start]
        while cur != start and cur in nxt:
            loop.append(cur)
            cur = nxt.pop(cur)
        if cur == start and len(loop) > 3:
            loops.append(loop)
    return loops


def _area(pts):
    return sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
               for i in range(len(pts))) / 2


def _simplify(pts):
    """Drop points on straight runs (the grid staircase keeps its corners)."""
    out = []
    n = len(pts)
    for i in range(n):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
        if (b[0] - a[0]) * (c[1] - b[1]) != (b[1] - a[1]) * (c[0] - b[0]):
            out.append(b)
    return out or pts


def compose(isochrone, dirs, lat0, lon0):
    """Valhalla isochrone (contours = levels(dirs)) → GeoJSON with one fill
    feature (MultiPolygon) and one outline feature (MultiLineString of
    closed loops). Every bearing wedge takes the contour closest to its own
    range; the pieces are joined on a grid (~1–1.5 km) and the outline is
    traced from it: one clean boundary, no cut edges, no holes, no slivers.
    dirs=None: the single contour as it is, cleaned the same way."""
    feats = (isochrone or {}).get("features") or []
    by_level = {}
    for f in feats:
        c = (f.get("properties") or {}).get("contour")
        if c is not None:
            by_level[float(c)] = [[_xy(lat0, lon0, la, lo) for lo, la in ring[:-1]] for ring in _outer_rings(f)]
    if not by_level:
        return None
    lv = sorted(by_level)
    pieces = []
    if not dirs or len(lv) == 1:
        pieces = [r for ring_set in (by_level[max(lv)],) for r in ring_set if len(r) >= 3]
    else:
        r = max(lv) * 2.0                    # wedge reaches beyond every contour
        half = 180.0 / N_DIRS
        for th, want in zip(bearings(), dirs):
            level = min(lv, key=lambda c: abs(c - want))
            wedge = _wedge(th, half, r)
            for ring in by_level[level]:
                piece = ring
                for i in range(len(wedge)):
                    if len(piece) < 3:
                        break
                    piece = _clip(piece, wedge[i], wedge[(i + 1) % len(wedge)])
                if len(piece) >= 3:
                    pieces.append(piece)
    if not pieces:
        return None
    grid, x0, y0, cell = _rasterize(pieces)
    loops = []
    for loop in _outline(grid):
        pts = [(x0 + (gx / 2 + 0.5) * cell, y0 + (gy / 2 + 0.5) * cell) for gx, gy in _simplify(loop)]
        if _area(pts) >= MIN_LOOP_KM2:      # outer boundaries only (holes run clockwise)
            loops.append(pts)
    if not loops:
        return None

    def ll(pts):
        c = [[round(lo, 5), round(la, 5)] for la, lo in (_ll(lat0, lon0, x, y) for x, y in pts)]
        return c + [c[0]]

    rings = [ll(p) for p in loops]
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"kind": "fill", "levels": lv},
         "geometry": {"type": "MultiPolygon", "coordinates": [[r] for r in rings]}},
        {"type": "Feature", "properties": {"kind": "line"},
         "geometry": {"type": "MultiLineString", "coordinates": rings}},
    ]}


# ── along real routes: speed limits and elevation from Valhalla ─────────────
# Per bearing a Valhalla route to a point beyond the range. Walking it in
# STEP_KM steps: the speed of the road section (speed limits, road class,
# top_speed 130) instead of one flat speed, the heading of the road for the
# wind, the route's elevation profile (Valhalla elevation tiles) for the climb.
# Without a route (sea, no road) or without elevation the straight-line walk
# and the raster height above stay in place for that bearing.

TOP_SPEED = 130
MAX_FERRY_KM = 40        # short ferries (Fehmarnbelt ~19 km) count, long ones
                         # (Travemünde–Trelleborg/Helsinki) make the route unusable
ELEV_INTERVAL_M = 500
ROUTE_TIMEOUT_S = 30


def decode_polyline6(s):
    """Valhalla's encoded shape (precision 6) → [(lat, lon)]."""
    out, i, lat, lon = [], 0, 0, 0
    while i < len(s):
        vals = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(result >> 1) if result & 1 else result >> 1)
        lat += vals[0]
        lon += vals[1]
        out.append((lat / 1e6, lon / 1e6))
    return out


def _route(lat0, lon0, lat1, lon1):
    import urllib.request
    import config
    body = {"locations": [{"lat": lat0, "lon": lon0}, {"lat": lat1, "lon": lon1}],
            "costing": "auto", "costing_options": {"auto": {"top_speed": TOP_SPEED}},
            "elevation_interval": ELEV_INTERVAL_M, "narrative": False,
            "directions_options": {"units": "kilometers"}}
    req = urllib.request.Request(config.VALHALLA_URL + "/route", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=ROUTE_TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8"))


def route_profile(resp):
    """Valhalla /route response → {"pts": [(lat, lon)], "cum": [km],
    "speed": [km/h per shape segment], "elev": [m every ELEV_INTERVAL_M] or
    None, "km": total} or None."""
    try:
        leg = resp["trip"]["legs"][0]
        pts = decode_polyline6(leg["shape"])
    except (KeyError, IndexError, TypeError):
        return None
    if len(pts) < 2:
        return None
    cum = [0.0]
    for (a, b), (c, d) in zip(pts, pts[1:]):
        x, y = _xy(a, b, c, d)
        cum.append(cum[-1] + math.hypot(x, y))
    speed = [None] * (len(pts) - 1)
    ferry = [False] * (len(pts) - 1)
    crossings = []
    for m in leg.get("maneuvers") or []:
        ln, tm = m.get("length"), m.get("time")
        b0, b1 = m.get("begin_shape_index"), m.get("end_shape_index")
        if b0 is None or b1 is None:
            continue
        # Valhalla maneuver type 28 = "take the ferry": the car rides along
        on_ferry = m.get("type") == 28 or m.get("travel_type") == "ferry"
        for k in range(b0, min(b1, len(speed))):
            ferry[k] = ferry[k] or on_ferry
        if on_ferry and b1 > b0:
            crossings.append({"b0": b0, "b1": min(b1, len(pts) - 1), "h": (tm or 0) / 3600,
                              "name": ", ".join((m.get("street_names") or [])[:1])})
        if ln and tm and tm > 0 and not on_ferry:
            v = ln / tm * 3600
            for k in range(b0, min(b1, len(speed))):
                speed[k] = v
    known = [v for v in speed if v]
    fill = sum(known) / len(known) if known else None
    speed = [v or fill for v in speed]
    elev = leg.get("elevation") or resp.get("trip", {}).get("elevation")
    ferry_km = sum(cum[k + 1] - cum[k] for k in range(len(ferry)) if ferry[k])
    for c in crossings:
        c["km_from"], c["km_to"] = cum[c["b0"]], cum[c["b1"]]
    return {"pts": pts, "cum": cum, "speed": speed, "ferry": ferry, "ferry_km": ferry_km,
            "crossings": crossings,
            "elev": elev if elev and all(e is not None for e in elev) else None, "km": cum[-1]}


def _at_km(prof, km):
    """(lat, lon, heading°, segment index) at road km along the profile."""
    cum, pts = prof["cum"], prof["pts"]
    k = max(0, min(len(cum) - 2, next((i for i in range(len(cum) - 1) if cum[i + 1] >= km), len(cum) - 2)))
    seg = cum[k + 1] - cum[k] or 1e-9
    f = max(0.0, min(1.0, (km - cum[k]) / seg))
    (a, b), (c, d) = pts[k], pts[k + 1]
    x, y = _xy(a, b, c, d)
    return a + (c - a) * f, b + (d - b) * f, (math.degrees(math.atan2(x, y)) + 360) % 360, k


def range_along_route(remaining_kwh, model, temp_now, prof, points, now, v_fixed=None,
                      speed_factor=None, mass_kg=None, until_km=None):
    """Road km until the energy runs out along one route profile; beyond the
    route's end it carries on with the last section's speed and heading.
    until_km: stop there and return the energy left (kWh, ≤ 0 = not reached)
    instead — e.g. at a ferry terminal."""
    import config
    import wind
    mass = mass_kg or config.VEHICLE_MASS_KG
    sf = speed_factor if speed_factor is not None else bl.CRUISE_AVG_KMH / bl.CRUISE_KMH
    lat0, lon0 = prof["pts"][0]
    pts = [(*_xy(lat0, lon0, p["lat"], p["lon"]), p["data"]) for p in points]

    def raster(lat, lon):
        if not pts:
            return None
        x, y = _xy(lat0, lon0, lat, lon)
        return min(pts, key=lambda q: (q[0] - x) ** 2 + (q[1] - y) ** 2)[2]

    def height(km, lat, lon):
        if prof["elev"]:
            i = min(len(prof["elev"]) - 1, int(km * 1000 / ELEV_INTERVAL_M))
            return prof["elev"][i]
        d = raster(lat, lon)
        return d.get("elev") if d else None

    left, road, t = remaining_kwh, 0.0, now
    h_prev = height(0.0, lat0, lon0)
    last_head, last_v = None, None
    while left > 0 and road < 1500 and (until_km is None or road < until_km):
        mid = road + STEP_KM / 2
        if mid <= prof["km"]:
            lat, lon, head, k = _at_km(prof, mid)
            if prof.get("ferry") and prof["ferry"][k]:
                road += STEP_KM            # on the ferry: distance without energy
                t += STEP_KM / 25.0 * 3600
                h_prev = None              # sea level jump, no climb energy
                continue
            v_road = prof["speed"][k]
            last_head, last_v = head, v_road
        else:                                   # beyond the route: straight on
            head, v_road = last_head or 0.0, last_v
            lat, lon, _, _ = _at_km(prof, prof["km"])
            extra = (mid - prof["km"]) / DETOUR
            lat += extra * math.cos(math.radians(head)) / KY
            lon += extra * math.sin(math.radians(head)) / (KX0 * math.cos(math.radians(lat)))
        v = v_fixed or ((v_road or bl.CRUISE_KMH) * sf)
        t += STEP_KM / max(v, 5.0) * 3600 / 2
        along, temp = 0.0, temp_now
        d = raster(lat, lon)
        w = wind.at(d, t) if d else None
        if w and w.get("kmh") is not None and w.get("dir") is not None:
            along = w["kmh"] * WIND_AT_CAR * math.cos(math.radians((w["dir"] + 180.0) % 360.0 - head))
            if w.get("temp") is not None:
                temp = w["temp"]
        t += STEP_KM / max(v, 5.0) * 3600 / 2
        cold = max(0.0, bl.COLD_C - temp) if temp is not None else 0.0
        cons = model["a"] + model["b"] * (max(0.0, v - along) / 100) ** 2 + model["c"] * cold
        need = max(cons, 5.0) * STEP_KM / 100
        h = height(min(mid + STEP_KM / 2, prof["km"]), lat, lon)
        if h is not None and h_prev is not None and h != h_prev:
            pot = mass * G * (h - h_prev) / 3.6e6
            need = max(0.0, need + (pot / DRIVE_EFF if pot > 0 else pot * REGEN_EFF))
        if h is not None:
            h_prev = h
        if need >= left:
            road += STEP_KM * left / need if need > 0 else STEP_KM
            left = 0
        else:
            road += STEP_KM
            left -= need
    if until_km is not None:
        return left
    return round(road)


def direction_ranges_routes(remaining_kwh, model, temp_now, lat0, lon0, points, now,
                            guess, v_fixed=None, route_fn=None):
    """Range per bearing along Valhalla routes; bearings without a route keep
    their value from guess (the straight-line walk)."""
    route_fn = route_fn or _route
    out, used = [], 0
    for th, g in zip(bearings(), guess):
        dist = max(g, 30) * 1.3 / DETOUR       # aim beyond the expected range
        lat1 = lat0 + dist * math.cos(math.radians(th)) / KY
        lon1 = lon0 + dist * math.sin(math.radians(th)) / (KX0 * math.cos(math.radians(lat0)))
        prof = None
        try:
            prof = route_profile(route_fn(lat0, lon0, lat1, lon1))
        except Exception:
            prof = None
        if not prof or prof["km"] < 5 or prof.get("ferry_km", 0) > MAX_FERRY_KM:
            out.append(g)              # no route, or only over a long sea crossing
            continue
        used += 1
        out.append(range_along_route(remaining_kwh, model, temp_now, prof, points, now, v_fixed=v_fixed))
    return out, used
