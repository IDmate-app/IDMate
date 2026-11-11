"""Range area from routes that end at the edge of the range (1.2.316).

Whether a place can be reached is only known from a route that ends there.
So for every bearing the target is put where the range is expected to end;
the route there says how much energy is left on arrival (speed of every road
section, elevation, wind, short ferries free). Too much left: the next
target lies further out, not enough: closer in. After two or three routes
the edge of that bearing is pinned between a place that is reached and one
that is not.

    probe()    routes → {"rim": [(bearing°, km as the crow flies, road km)], …}
    compose()  Valhalla contour (the land reachable by road: coast, islands)
               cut to the rim → GeoJSON (fill + outline)

History: 1.2.290–1.2.314 let each of 32 routes to a point far beyond the
range decide for its whole wedge; 1.2.315 interpolated samples along 64 such
routes. Both only know the trunk roads those routes take — the land between
two motorways got no route at all and was guessed (the gap at Uelzen).

Pure logic apart from the Valhalla route calls (route_fn can be passed in).
"""

import logging
import math
import time

import wind_range as wr

log = logging.getLogger("triplog.range_field")

MAX_ROUTES = 3           # per bearing
MIN_BEARINGS = 8         # fewer with a result: no rim, the old way
EXTRA_BUDGET_S = 90      # the bearings in between only when everything fits into this
STEP_OUT = 1.6           # never more than this × further out in one step
TABLE = 720              # rim lookup: half-degree steps


def _target(lat0, lon0, th, km):
    lat = lat0 + km * math.cos(math.radians(th)) / wr.KY
    lon = lon0 + km * math.sin(math.radians(th)) / (wr.KX0 * math.cos(math.radians(lat0)))
    return lat, lon


def _polar(lat0, lon0, lat, lon):
    x, y = wr._xy(lat0, lon0, lat, lon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360, math.hypot(x, y)


def _left_at(trace, km):
    """Energy left at road km, from the samples of a walk (linear between
    them, carried on beyond the last)."""
    if not trace:
        return None
    if len(trace) == 1:
        return trace[0][2]
    for a, b in zip(trace, trace[1:]):
        if km <= b[3]:
            break
    span = (b[3] - a[3]) or 1e-9
    return a[2] + (b[2] - a[2]) * (km - a[3]) / span


def _arrive(remaining_kwh, model, temp_now, lat0, lon0, th, r_km, points, now, v_fixed, route_fn):
    """Route to the place r_km out on bearing th → dict or None (no route):
    {"r": km as the crow flies of where the route really ends, "th": its
    bearing, "road": road km, "left": kWh on arrival (negative: short by
    that much), "ferry": True when a long ferry lies on the way — then r,
    road and left are those at its terminal}."""
    lat1, lon1 = _target(lat0, lon0, th, r_km)
    try:
        prof = wr.route_profile(route_fn(lat0, lon0, lat1, lon1))
    except Exception:
        prof = None
    if not prof or prof["km"] < 1:
        return None
    end, ferry = prof["km"], False
    for c in prof.get("crossings") or []:
        if c["km_to"] - c["km_from"] > wr.MAX_FERRY_KM:
            end, ferry = c["km_from"], True        # a long sea crossing: only up to the terminal
            break
    if end < 1:
        return None
    trace = [(lat0, lon0, remaining_kwh, 0.0)]
    wr.range_along_route(remaining_kwh, model, temp_now, prof, points, now, v_fixed=v_fixed,
                         trace=trace, trace_km=end)
    left = _left_at(trace, end)
    if left is None:
        return None
    la, lo, _, _ = wr._at_km(prof, end)
    th1, r1 = _polar(lat0, lon0, la, lo)
    return {"r": r1, "th": th1, "road": end, "left": left, "ferry": ferry, "asked": r_km}


def _edge(remaining_kwh, model, temp_now, lat0, lon0, th, r0, points, now, v_fixed, route_fn):
    """The edge of the range on one bearing → (rim point or None, routes asked).
    Rim point: (bearing°, km as the crow flies, road km)."""
    lo = hi = None                       # furthest place reached / nearest place not reached
    r, asked = max(r0, 5.0), 0
    for _ in range(MAX_ROUTES):
        asked += 1
        a = _arrive(remaining_kwh, model, temp_now, lat0, lon0, th, r, points, now, v_fixed, route_fn)
        if a is None:
            if lo or hi:
                break
            r *= 0.6                      # no road out there (sea): try closer in
            continue
        if a["ferry"]:
            if a["left"] >= 0:            # the terminal is reached: the range ends at the quay
                return (a["th"], a["r"] + 3.0, a["road"]), asked
            hi = a if hi is None or a["r"] < hi["r"] else hi
        elif a["left"] >= 0:
            ended_short = a["r"] < a["asked"] - 15
            lo = a if lo is None or a["r"] > lo["r"] else lo
            if ended_short:
                break                     # the route ends well before the target: the land ends here
        else:
            hi = a if hi is None or a["r"] < hi["r"] else hi
        if lo and hi and hi["r"] - lo["r"] <= 25:
            break
        if lo and hi:
            # between the two: where the energy left crosses zero
            r = lo["r"] + (hi["r"] - lo["r"]) * lo["left"] / (lo["left"] - hi["left"])
        else:
            used = remaining_kwh - a["left"]          # energy this route needs
            scale = remaining_kwh / used if used > 0 else STEP_OUT
            r = a["r"] * min(STEP_OUT, max(0.4, scale * (1.04 if a["left"] >= 0 else 0.96)))
    if lo and hi and hi["r"] > lo["r"]:
        f = lo["left"] / (lo["left"] - hi["left"]) if lo["left"] != hi["left"] else 0.5
        return (th, lo["r"] + (hi["r"] - lo["r"]) * f, lo["road"] + (hi["road"] - lo["road"]) * f), asked
    one = lo or hi
    if not one:
        return None, asked
    used = remaining_kwh - one["left"]
    scale = min(STEP_OUT, max(0.3, remaining_kwh / used)) if used > 0 else 1.0
    # only one side known: the same consumption carried on (the contour cuts
    # the sea where the land ended before the energy did)
    return (th, one["r"] * scale, one["road"] * scale), asked


def probe(remaining_kwh, model, temp_now, lat0, lon0, points, now, guess, v_fixed=None,
          route_fn=None, budget_s=EXTRA_BUDGET_S, clock=time.time):
    """The edge of the range on the N_DIRS bearings and, when the routes are
    fast enough, on the bearings in between. guess: road km per bearing (the
    straight-line walk).
    Returns {"rim": [(bearing°, km as the crow flies, road km)] sorted by
    bearing, "dirs": road km per bearing (guess where nothing was found),
    "dmax": largest road km on the rim, "used": bearings with a result,
    "routes": routes asked, "seconds"} — or None with too few results."""
    route_fn = route_fn or wr._route
    bs = wr.bearings()
    half = 180.0 / wr.N_DIRS
    rim, dirs, routes = [], [], 0
    first = [None] * len(bs)
    t0 = clock()
    for i, (th, g) in enumerate(zip(bs, guess)):
        p, n = _edge(remaining_kwh, model, temp_now, lat0, lon0, th, max(g, 10) / wr.DETOUR, points, now,
                     v_fixed, route_fn)
        routes += n
        first[i] = p
        dirs.append(int(round(p[2])) if p else g)
        if p:
            rim.append(p)
    took = clock() - t0
    if len(rim) >= MIN_BEARINGS and took * 2 <= budget_s:
        for i, th in enumerate(bs):          # the bearings halfway between
            a, b = first[i], first[(i + 1) % len(bs)]
            near = [p[1] for p in (a, b) if p]
            r0 = sum(near) / len(near) if near else max(guess[i], 10) / wr.DETOUR
            p, n = _edge(remaining_kwh, model, temp_now, lat0, lon0, th + half, r0, points, now, v_fixed, route_fn)
            routes += n
            if p:
                rim.append(p)
    if len(rim) < MIN_BEARINGS:
        return None
    rim.sort()
    return {"rim": rim, "dirs": dirs, "used": len(rim), "routes": routes,
            "dmax": max(p[2] for p in rim), "seconds": clock() - t0}


def mask_km(field):
    """Distance of the contour that the rim is cut from: a little beyond the
    furthest road km on the rim, rounded up to 5 km."""
    return int(math.ceil((field["dmax"] + wr.STEP_KM) / 5.0) * 5)


def rim_table(rim):
    """Reach as the crow flies for every half degree: linear between the
    rim points around the circle, lightly smoothed."""
    pts = sorted((th % 360.0, r) for th, r, _ in rim)
    if not pts:
        return None
    n = len(pts)
    raw = []
    j = 0
    for k in range(TABLE):
        th = k * 360.0 / TABLE
        while j < n and pts[j][0] <= th:
            j += 1
        a, b = pts[(j - 1) % n], pts[j % n]
        span = (b[0] - a[0]) % 360.0 or 360.0
        f = ((th - a[0]) % 360.0) / span if n > 1 else 0.0
        raw.append(a[1] + (b[1] - a[1]) * f)
    w = TABLE // 180                       # ±2°: round the corners at the rim points
    return [sum(raw[(k + d) % TABLE] for d in range(-w, w + 1)) / (2 * w + 1) for k in range(TABLE)]


def compose(isochrone, rim, lat0, lon0):
    """The largest contour of a Valhalla isochrone (land reachable by road)
    cut to the rim → GeoJSON like wind_range.compose: one fill feature
    (MultiPolygon) and one outline feature (closed loops)."""
    feats = [f for f in (isochrone or {}).get("features") or []
             if (f.get("properties") or {}).get("contour") is not None]
    table = rim_table(rim or [])
    if not feats or not table:
        return None
    top = max(feats, key=lambda f: float(f["properties"]["contour"]))
    rings = [[wr._xy(lat0, lon0, la, lo) for lo, la in ring[:-1]] for ring in wr._outer_rings(top)]
    rings = [r for r in rings if len(r) >= 3]
    if not rings:
        return None
    grid, x0, y0, cell = wr._rasterize(rings)
    k = TABLE / 360.0
    for j, row in enumerate(grid):
        if not any(row):
            continue
        y = y0 + (j + 0.5) * cell
        for i in range(len(row)):
            if row[i]:
                x = x0 + (i + 0.5) * cell
                th = math.degrees(math.atan2(x, y)) % 360.0
                if x * x + y * y > table[int(th * k) % TABLE] ** 2:
                    row[i] = 0
    loops = []
    for loop in wr._outline(grid):
        pts = [(x0 + (gx / 2 + 0.5) * cell, y0 + (gy / 2 + 0.5) * cell) for gx, gy in wr._simplify(loop)]
        if wr._area(pts) >= wr.MIN_LOOP_KM2:
            loops.append(pts)
    if not loops:
        return None

    def ll(pts):
        c = [[round(lo, 5), round(la, 5)] for la, lo in (wr._ll(lat0, lon0, x, y) for x, y in pts)]
        return c + [c[0]]

    out = [ll(p) for p in loops]
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"kind": "fill", "levels": [float(top["properties"]["contour"])]},
         "geometry": {"type": "MultiPolygon", "coordinates": [[r] for r in out]}},
        {"type": "Feature", "properties": {"kind": "line"},
         "geometry": {"type": "MultiLineString", "coordinates": out}},
    ]}
