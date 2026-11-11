"""Range area from an energy field (1.2.315).

Before, each of 32 routes ("rays") set the range of its whole bearing wedge,
and the wedge was cut from one of at most four distance contours: a star with
fingers and steps. A route to a point far out says little about the land
between two rays, and the further out, the wider the gaps.

Now every route is walked in 10 km steps and leaves a sample at each step:
"this much energy is left here" — positive on the way out, negative past the
point where the battery is empty. Twice as many rays (the second half on the
bearings in between) give some thousand samples. They are interpolated to a
field over the map; the area is where the field is not negative, cut to the
land Valhalla can reach by road (one distance contour as the mask: coast,
islands, places without roads).

    rays()     routes → {"dirs", "samples", "dmax", "used"}
    compose()  isochrone mask + samples → GeoJSON (fill + outline)

Pure logic apart from the Valhalla route calls (route_fn can be passed in).
"""

import logging
import math
import time

import wind_range as wr

log = logging.getLogger("triplog.range_field")

AIM = 1.8                # route target: this × the guessed range as road km (÷ DETOUR as the crow flies);
                         # on slow roads the car gets up to ~1.7 × the 130 km/h figure
GAP_AIM = 1.3            # the rays in between: this × what their neighbours reached
MIN_RAYS = 8             # fewer routes than this: no field, the old way
EXTRA_BUDGET_S = 75      # second half of the rays only when all routes fit into this
BUCKET_KM = 15.0
NEAREST = 6              # samples per interpolated value
COARSE = 4               # field on every 4th cell of the outline grid, bilinear in between


def _target(lat0, lon0, th, km):
    lat = lat0 + km * math.cos(math.radians(th)) / wr.KY
    lon = lon0 + km * math.sin(math.radians(th)) / (wr.KX0 * math.cos(math.radians(lat0)))
    return lat, lon


def _ray(remaining_kwh, model, temp_now, lat0, lon0, th, guess, points, now, v_fixed, route_fn):
    """One route towards bearing th → (range km or None, samples)."""
    lat1, lon1 = _target(lat0, lon0, th, max(guess, 30) * AIM / wr.DETOUR)
    try:
        prof = wr.route_profile(route_fn(lat0, lon0, lat1, lon1))
    except Exception:
        prof = None
    if not prof or prof["km"] < 5:
        return None, []
    limit = None
    for c in prof.get("crossings") or []:
        if c["km_to"] - c["km_from"] > wr.MAX_FERRY_KM:
            limit = c["km_from"]           # a long sea crossing: the route counts up to the terminal
            break
    if limit is not None and limit < 5:
        return None, []
    # Samples only where the route really runs: up to its end, or up to the
    # terminal of a long ferry. (Carried on straight past a route that ends
    # at the coast they would lie in the sea and pull the shore down.)
    end = min(prof["km"], limit) if limit is not None else prof["km"]
    trace = []
    km = wr.range_along_route(remaining_kwh, model, temp_now, prof, points, now, v_fixed=v_fixed,
                              trace=trace, trace_km=end)
    trace = [s for s in trace if s[3] <= end]
    if limit is not None and km > limit:
        km = None                          # reaches the terminal: no range from this ray
    return km, trace


def rays(remaining_kwh, model, temp_now, lat0, lon0, points, now, guess, v_fixed=None,
         route_fn=None, budget_s=EXTRA_BUDGET_S, clock=time.time):
    """Routes on the N_DIRS bearings and, when they are fast enough, on the
    bearings in between. guess: road km per bearing (straight-line walk).
    Returns {"dirs": road km per bearing (guess where there is no route),
    "samples": [(lat, lon, kWh left, road km)], "dmax": furthest road km
    still reached, "used": routes that counted, "rays": routes asked} —
    or None when too few routes came back."""
    route_fn = route_fn or wr._route
    bs = wr.bearings()
    half = 180.0 / wr.N_DIRS
    dirs, samples, used, asked = [], [], 0, 0
    t0 = clock()
    for th, g in zip(bs, guess):
        asked += 1
        km, tr = _ray(remaining_kwh, model, temp_now, lat0, lon0, th, g, points, now, v_fixed, route_fn)
        dirs.append(km if km is not None else g)
        if tr:
            used += 1
            samples += tr
    first = clock() - t0
    if used >= MIN_RAYS and first * 2 <= budget_s:
        for i, th in enumerate(bs):          # the gaps: bearings halfway between
            # aim a little beyond what the two neighbours reached
            g = max(dirs[i], dirs[(i + 1) % len(dirs)]) * GAP_AIM / AIM
            asked += 1
            km, tr = _ray(remaining_kwh, model, temp_now, lat0, lon0, th + half, g, points, now, v_fixed, route_fn)
            if tr:
                used += 1
                samples += tr
    if used < MIN_RAYS:
        return None
    reached = [s[3] for s in samples if s[2] >= 0]
    return {"dirs": dirs, "samples": samples, "used": used, "rays": asked,
            "dmax": max(reached) if reached else 0.0, "seconds": clock() - t0}


def mask_km(field):
    """Distance of the one contour that masks the field: a little beyond
    the furthest point still reached, rounded up to 5 km."""
    return int(math.ceil((field["dmax"] + wr.STEP_KM) / 5.0) * 5)


class _Field:
    """Inverse-distance interpolation of the samples (local km)."""

    def __init__(self, pts):
        self.b = {}
        for x, y, f in pts:
            self.b.setdefault((int(x // BUCKET_KM), int(y // BUCKET_KM)), []).append((x, y, f))
        self.any = bool(pts)

    def at(self, x, y):
        if not self.any:
            return None
        bx, by = int(x // BUCKET_KM), int(y // BUCKET_KM)
        found, r, done = [], 1, False
        while r <= 40:
            for i in range(bx - r, bx + r + 1):
                for j in range(by - r, by + r + 1):
                    if max(abs(i - bx), abs(j - by)) == r or r == 1:
                        found += self.b.get((i, j), ())
            if len(found) >= NEAREST and done:
                break                      # one ring more than needed: nothing nearer is missed
            done = len(found) >= NEAREST
            r += 1
        if not found:
            return None
        near = sorted(((px - x) ** 2 + (py - y) ** 2, f) for px, py, f in found)[:NEAREST]
        num = den = 0.0
        for d2, f in near:
            w = 1.0 / (d2 + 1.0)
            num += w * f
            den += w
        return num / den


def compose(isochrone, samples, lat0, lon0):
    """Mask (the outer rings of the largest contour of a Valhalla isochrone)
    cut by the energy field → GeoJSON like wind_range.compose: one fill
    feature (MultiPolygon) and one outline feature (closed loops)."""
    feats = [f for f in (isochrone or {}).get("features") or []
             if (f.get("properties") or {}).get("contour") is not None]
    if not feats or not samples:
        return None
    top = max(feats, key=lambda f: float(f["properties"]["contour"]))
    rings = [[wr._xy(lat0, lon0, la, lo) for lo, la in ring[:-1]] for ring in wr._outer_rings(top)]
    rings = [r for r in rings if len(r) >= 3]
    if not rings:
        return None
    grid, x0, y0, cell = wr._rasterize(rings)
    ny, nx = len(grid), len(grid[0])
    field = _Field([(*wr._xy(lat0, lon0, la, lo), f) for la, lo, f, _ in samples])
    # field on a coarse lattice, bilinear in between
    cx, cy = nx // COARSE + 2, ny // COARSE + 2
    step = cell * COARSE
    lat = [[None] * cx for _ in range(cy)]
    need = set()
    for j in range(ny):
        row = grid[j]
        if any(row):
            for i in range(nx):
                if row[i]:
                    need.add((i // COARSE, j // COARSE))
    for (a, b) in list(need):
        for da in (0, 1):
            for db in (0, 1):
                if lat[b + db][a + da] is None:
                    lat[b + db][a + da] = field.at(x0 + (a + da) * step + cell / 2, y0 + (b + db) * step + cell / 2)
    for j in range(ny):
        row = grid[j]
        if not any(row):
            continue
        b, v = j // COARSE, (j % COARSE) / COARSE
        for i in range(nx):
            if not row[i]:
                continue
            a, u = i // COARSE, (i % COARSE) / COARSE
            f00, f10, f01, f11 = lat[b][a], lat[b][a + 1], lat[b + 1][a], lat[b + 1][a + 1]
            if None in (f00, f10, f01, f11):
                vals = [f for f in (f00, f10, f01, f11) if f is not None]
                f = sum(vals) / len(vals) if vals else -1.0
            else:
                f = (f00 * (1 - u) + f10 * u) * (1 - v) + (f01 * (1 - u) + f11 * u) * v
            if f < 0:
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
