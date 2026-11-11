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
MAX_ROUTES_WIDE = 6      # … up to this many while reached and not reached lie far apart
GAP_KM = 25              # close enough: the edge is taken between the two
MIN_BEARINGS = 8         # fewer with a result: no rim, the old way
EXTRA_BUDGET_S = 90      # a finer pass only while twice the time so far fits into this
CLOSE = 0.04             # a route that arrives within this share of the energy of empty needs no second one
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


def arrival(remaining_kwh, model, temp_now, lat0, lon0, lat1, lon1, points, now, v_fixed=None, route_fn=None):
    """One route from the car to a place → dict or None (no route):
    {"lat", "lon": where the route really ends (the nearest road), "road":
    road km, "left": kWh on arrival (negative: short by that much),
    "ferry": True when a long ferry lies on the way — then lat/lon, road
    and left are those at its terminal —, "ferry_km": km on short ferries
    (free), "ferry_name": the long ferry, "empty_km": road km where the
    energy runs out (None when it lasts)}."""
    route_fn = route_fn or wr._route
    try:
        prof = wr.route_profile(route_fn(lat0, lon0, lat1, lon1))
    except Exception:
        prof = None
    if not prof or prof["km"] < 1:
        return None
    end, ferry, name = prof["km"], False, ""
    short = 0.0
    for c in prof.get("crossings") or []:
        if c["km_to"] - c["km_from"] > wr.MAX_FERRY_KM:
            end, ferry, name = c["km_from"], True, c.get("name") or ""   # a long sea crossing: only up to the terminal
            break
        short += c["km_to"] - c["km_from"]
    if end < 1:
        return None
    trace = [(lat0, lon0, remaining_kwh, 0.0)]
    km = wr.range_along_route(remaining_kwh, model, temp_now, prof, points, now, v_fixed=v_fixed,
                              trace=trace, trace_km=end)
    left = _left_at(trace, end)
    if left is None:
        return None
    la, lo, _, _ = wr._at_km(prof, end)
    return {"lat": la, "lon": lo, "road": end, "left": left, "ferry": ferry, "ferry_km": short,
            "ferry_name": name, "empty_km": km if left < 0 else None}


def _arrive(remaining_kwh, model, temp_now, lat0, lon0, th, r_km, points, now, v_fixed, route_fn):
    """arrival() at the place r_km out on bearing th, plus "r"/"th": km as
    the crow flies and bearing of where the route really ends."""
    lat1, lon1 = _target(lat0, lon0, th, r_km)
    a = arrival(remaining_kwh, model, temp_now, lat0, lon0, lat1, lon1, points, now, v_fixed, route_fn)
    if a is None:
        return None
    a["th"], a["r"] = _polar(lat0, lon0, a["lat"], a["lon"])
    a["asked"] = r_km
    return a


def _edge(remaining_kwh, model, temp_now, lat0, lon0, th, r0, points, now, v_fixed, route_fn, seen=None):
    """The edge of the range on one bearing → (rim point or None, routes
    asked, energy). Rim point: (bearing°, km as the crow flies, road km);
    energy: True when the road km are a measured range (a place reached and
    one not reached pin the edge), False at the quay of a long ferry or
    where the land ends before the energy does."""
    lo = hi = None                       # furthest place reached / nearest place not reached
    r, asked = max(r0, 5.0), 0
    for n in range(MAX_ROUTES_WIDE):
        if n >= MAX_ROUTES and not (lo and hi and hi["r"] - lo["r"] > GAP_KM):
            break                         # more than MAX_ROUTES only to close a wide gap
        asked += 1
        a = _arrive(remaining_kwh, model, temp_now, lat0, lon0, th, r, points, now, v_fixed, route_fn)
        if a is None:
            if lo or hi:
                break
            r *= 0.6                      # no road out there (sea): try closer in
            continue
        if seen is not None:
            seen.append(a)                # every place a route ended at, reached or not
        if a["ferry"]:
            if a["left"] >= 0:            # the terminal is reached: the range ends at the quay
                return (a["th"], a["r"] + 3.0, a["road"]), asked, False
            hi = a if hi is None or a["r"] < hi["r"] else hi
        elif a["left"] >= 0:
            ended_short = a["r"] < a["asked"] - 15
            lo = a if lo is None or a["r"] > lo["r"] else lo
            if ended_short:
                break                     # the route ends well before the target: the land ends here
        else:
            hi = a if hi is None or a["r"] < hi["r"] else hi
        if lo and hi and hi["r"] - lo["r"] <= GAP_KM:
            break
        if not (lo and hi) and abs(a["left"]) <= CLOSE * remaining_kwh and not a["ferry"]:
            break                         # arrives (almost) exactly empty: that is the edge
        if lo and hi:
            # Between the two: where the energy left crosses zero. But a place
            # behind a detour (across a bay) is short by far more than its
            # distance says, and that estimate then sits right at the reached
            # place again: keep to the middle part of the gap.
            f = lo["left"] / (lo["left"] - hi["left"])
            r = lo["r"] + (hi["r"] - lo["r"]) * min(0.7, max(0.3, f))
        else:
            used = remaining_kwh - a["left"]          # energy this route needs
            scale = remaining_kwh / used if used > 0 else STEP_OUT
            r = a["r"] * min(STEP_OUT, max(0.4, scale * (1.04 if a["left"] >= 0 else 0.96)))
    if lo and hi and hi["r"] > lo["r"]:
        f = lo["left"] / (lo["left"] - hi["left"]) if lo["left"] != hi["left"] else 0.5
        return (th, lo["r"] + (hi["r"] - lo["r"]) * f, lo["road"] + (hi["road"] - lo["road"]) * f), asked, True
    one = lo or hi
    if not one:
        return None, asked, False
    used = remaining_kwh - one["left"]
    scale = min(STEP_OUT, max(0.3, remaining_kwh / used)) if used > 0 else 1.0
    # only one side known: the same consumption carried on (the contour cuts
    # the sea where the land ended before the energy did). Its road km are a
    # measured range only when the route arrived about empty — at a coast
    # with most of the energy left they are a capped guess.
    return (th, one["r"] * scale, one["road"] * scale), asked, abs(one["left"]) <= CLOSE * remaining_kwh


def probe(remaining_kwh, model, temp_now, lat0, lon0, points, now, guess, v_fixed=None,
          route_fn=None, budget_s=EXTRA_BUDGET_S, clock=time.time):
    """The edge of the range on the N_DIRS bearings and, while the routes are
    fast enough, on the bearings in between (twice, up to 4 × N_DIRS).
    guess: road km per bearing (the straight-line walk).
    Returns {"rim": [(bearing°, km as the crow flies, road km)] sorted by
    bearing, "dirs": road km per bearing (guess where nothing was found),
    "dmax": largest road km on the rim, "safe": smallest road km at which
    the battery ran out on any bearing (None when unknown), "used": bearings
    with a result, "routes": routes asked, "seconds"} — or None with too few
    results."""
    route_fn = route_fn or wr._route
    bs = wr.bearings()
    half = 180.0 / wr.N_DIRS
    rim, dirs, routes, roads, seen = [], [], 0, [], []
    t0 = clock()
    for i, (th, g) in enumerate(zip(bs, guess)):
        p, n, by_energy = _edge(remaining_kwh, model, temp_now, lat0, lon0, th, max(g, 10) / wr.DETOUR, points, now,
                                v_fixed, route_fn, seen)
        routes += n
        dirs.append(int(round(p[2])) if p else g)
        if p:
            rim.append(p)
            if by_energy:
                roads.append(p[2])
    # Finer while the routes are fast: the bearings halfway between, then
    # the quarters (64 and 128 bearings). Each pass starts where the rim so
    # far says the edge is, so most bearings need one or two routes.
    for offsets in ((half,), (half / 2, half * 1.5)):
        if len(rim) < MIN_BEARINGS or (clock() - t0) * 2 > budget_s:
            break
        table = rim_table(rim)
        for th in bs:
            for off in offsets:
                b = (th + off) % 360.0
                p, n, by_energy = _edge(remaining_kwh, model, temp_now, lat0, lon0, b,
                                        table[int(b * TABLE / 360.0) % TABLE], points, now, v_fixed, route_fn, seen)
                routes += n
                if p:
                    rim.append(p)
                    if by_energy:
                        roads.append(p[2])
    if len(rim) < MIN_BEARINGS:
        return None
    rim.sort()
    # the places themselves, in local km: (x, y, kWh left on arrival) — plus the car
    places = [(0.0, 0.0, float(remaining_kwh))]
    for a in seen:
        t = math.radians(a["th"])
        places.append((a["r"] * math.sin(t), a["r"] * math.cos(t), a["left"]))
    return {"rim": rim, "places": places, "dirs": dirs, "used": len(rim), "routes": routes,
            "dmax": max(p[2] for p in rim), "safe": min(roads) if roads else None, "seconds": clock() - t0}


def mask_km(field):
    """Distance of the contour that the rim is cut from: a little beyond the
    furthest road km on the rim, rounded up to 5 km."""
    return int(math.ceil((field["dmax"] + wr.STEP_KM) / 5.0) * 5)


def safe_km(field):
    """Road distance every place within which counts as reached, whatever
    its bearing: the shortest road distance at which the battery ran out
    (fastest roads, head wind), rounded down to 5 km. None when it is not
    clearly inside the mask contour."""
    s = field.get("safe")
    if not s:
        return None
    s = int(s // 5 * 5)
    return s if 10 <= s <= mask_km(field) - 10 else None


def rim_table(rim):
    """Reach as the crow flies for every half degree: linear between the
    rim points around the circle, the corners barely rounded."""
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
    w = TABLE // 360                       # ±1°: only take the sharpest tip off the rim points
    return [sum(raw[(k + d) % TABLE] for d in range(-w, w + 1)) / (2 * w + 1) for k in range(TABLE)]


def compose(isochrone, rim, lat0, lon0, safe=None):
    """The largest contour of a Valhalla isochrone (land reachable by road)
    cut to the rim → GeoJSON like wind_range.compose: one fill feature
    (MultiPolygon) and one outline feature (closed loops).

    safe: km of a smaller contour in the same isochrone — everything inside
    it stays, also beyond the rim. One edge per bearing cannot tell that a
    peninsula or an island behind a bay is still in reach by its own road
    (the edge of that bearing may lie in the water before it); the road
    distance can."""
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
    near = None
    if safe:
        inner = [f for f in feats if f is not top and abs(float(f["properties"]["contour"]) - safe) < 0.5]
        srings = [[wr._xy(lat0, lon0, la, lo) for lo, la in ring[:-1]] for f in inner for ring in wr._outer_rings(f)]
        srings = [r for r in srings if len(r) >= 3]
        if srings:
            near = wr._rasterize(srings, frame=(x0, y0, cell, len(grid[0]), len(grid)))[0]
    k = TABLE / 360.0
    for j, row in enumerate(grid):
        if not any(row):
            continue
        y = y0 + (j + 0.5) * cell
        keep = near[j] if near else None
        for i in range(len(row)):
            if row[i] and not (keep and keep[i]):
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
