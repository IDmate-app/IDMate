"""Visits view of the trip log: when we were away from home, and when on site.

An *outing* runs from the trip that leaves home to the trip that comes back
home. Between two trips of an outing the car stood somewhere (a *stop*).
Consecutive stops within SITE_RADIUS_M of the first stop of the group are one
*on-site* block: from arriving at the first stop to leaving the last one. The
short hops inside (e.g. between gates of a port) belong to the time on site.
A hop that drove more than SITE_RADIUS_M is travel even when the straight line
is shorter (12 km by road, 9.6 km as the crow flies).

A stop is "home" when its position lies within HOME_RADIUS_M of a home point
(the "Zuhause" geofence, home charge locations) or its address is a home name.
A trip without position whose NEXT trip starts at home is taken to end at home
(missing telemetry on the way back).

Charging stops are travel: a charge more than CHARGE_SITE_M away from the
site never joins its block, and an on-site block made only of stops with a
charge and shorter than CHARGE_STOP_MAX_MIN is dropped (the return trip from
work with a stop at a charger is still the return trip).

Pure logic: the caller passes trips, charges and home points.
"""

import math
from datetime import datetime

HOME_RADIUS_M = 1000
SITE_RADIUS_M = 10000
CHARGE_STOP_MAX_MIN = 60
CHARGE_SITE_M = 1000
HOME_NAMES = {"zuhause", "home", "daheim", "zu hause"}
# Visit reason of a trip that only goes back from a site (its destination
# names the place left, not the place reached).
RETURN_WORD = "rückfahrt"


def _dist_m(a, b):
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 6371000.0 * 2 * math.asin(math.sqrt(h))


def _ts(v):
    if not v:
        return None
    s = str(v).replace(" ", "T")[:19]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _pos(lat, lon):
    return (lat, lon) if lat is not None and lon is not None else None


def _minutes(a, b):
    if not a or not b:
        return None
    return int(round((b - a).total_seconds() / 60))


class _Home:
    def __init__(self, points, names):
        self.points = [p for p in points if p and p[0] is not None and p[1] is not None]
        self.names = {n.strip().lower() for n in names if n} | HOME_NAMES

    def at(self, pos, addr):
        if addr and addr.strip().lower() in self.names:
            return True
        return bool(pos) and any(_dist_m(pos, p) <= HOME_RADIUS_M for p in self.points)


def _most_common(values):
    counts = {}
    for v in values:
        if v:
            counts[v] = counts.get(v, 0) + 1
    return max(counts, key=lambda k: (counts[k], k)) if counts else ""


def _unique(values):
    out = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


def _addresses(values):
    """Unique addresses; a bare street is dropped when the same street also
    appears with a house number ("... Hafenstraße" next to "... Hafenstraße 8")."""
    vals = _unique(values)
    return [a for a in vals if not any(b != a and b.startswith(a + " ") for b in vals)]


def build_outings(trips, charges, home_points, home_names=()):
    """trips: dict rows (start/end time, lat/lon, address, purpose,
    destination, distance_km, is_manual, manual_fields), any order.
    charges: dict rows with start_time/end_time.
    Returns outings, newest first:
      {start, end, minutes, addresses, purpose, complete, flagged, sites: [
         {start, end, minutes, destinations, addresses, purpose}]}"""
    home = _Home(home_points, home_names)
    ts = sorted((t for t in trips if _ts(t.get("start_time"))),
                key=lambda t: (_ts(t["start_time"]), t.get("id") or 0))
    ch = [(_ts(c.get("start_time")), _ts(c.get("end_time"))) for c in charges]
    ch = [(a, b or a) for a, b in ch if a]

    raw = []
    for t in ts:
        s_pos = _pos(t.get("start_lat"), t.get("start_lon"))
        e_pos = _pos(t.get("end_lat"), t.get("end_lon"))
        raw.append((home.at(s_pos, t.get("start_address")), home.at(e_pos, t.get("end_address"))))

    outings, cur = [], None
    for i, t in enumerate(ts):
        s_home = raw[i][0] or (i > 0 and raw[i - 1][1])
        e_home = raw[i][1] or (i + 1 < len(ts) and raw[i + 1][0])
        if s_home and cur:              # left home again without coming back
            outings.append(cur)
            cur = None
        if cur is None:
            cur = {"trips": [], "starts_home": bool(s_home)}
        cur["trips"].append(t)
        if e_home:
            cur["ends_home"] = True
            outings.append(cur)
            cur = None
    if cur:
        outings.append(cur)

    result = [_outing(o, home, ch) for o in outings]
    result.sort(key=lambda o: o["start"] or datetime.min, reverse=True)
    return result


def _outing(o, home, charges):
    trips = o["trips"]
    start, end = _ts(trips[0]["start_time"]), _ts(trips[-1]["end_time"])
    stops = []
    for a, b in zip(trips, trips[1:]):
        arrive, leave = _ts(a["end_time"]), _ts(b["start_time"])
        pos = _pos(a.get("end_lat"), a.get("end_lon")) or _pos(b.get("start_lat"), b.get("start_lon"))
        addrs = _unique([a.get("end_address"), b.get("start_address")])
        charged = bool(arrive and leave) and any(cs < leave and ce > arrive for cs, ce in charges)
        # The trip arriving here names the place in "destination" — unless it
        # is the way back from somewhere else ("Rückfahrt").
        back = RETURN_WORD in (a.get("visit_reason") or "").lower()
        stops.append({"arrive": arrive, "leave": leave, "pos": pos, "addrs": addrs,
                      "dest": a.get("destination") or "", "purpose": a.get("purpose") or "",
                      "back": back, "charge": charged, "hop_km": a.get("distance_km")})

    # Trips whose times overlap (filled in by hand with one shared window)
    # leave no stop between them: no time on site can be told there.
    overlap = any(s["arrive"] and s["leave"] and s["leave"] < s["arrive"] for s in stops)
    stops = [s for s in stops if not (s["arrive"] and s["leave"] and s["leave"] < s["arrive"])]

    groups = []
    for s in stops:
        g = groups[-1] if groups else None
        drove_away = (s["hop_km"] or 0) * 1000 > SITE_RADIUS_M
        d = _dist_m(g["anchor"], s["pos"]) if g and s["pos"] and g["anchor"] else None
        if g and d is not None and d <= SITE_RADIUS_M and not drove_away \
                and not (s["charge"] and d > CHARGE_SITE_M):
            g["stops"].append(s)
        elif g and not s["pos"] and not g["anchor"] and not drove_away \
                and s["dest"] and s["dest"] == g["stops"][-1]["dest"]:
            g["stops"].append(s)
        else:
            groups.append({"anchor": s["pos"], "stops": [s]})

    sites = []
    for g in groups:
        st = g["stops"]
        s0, s1 = st[0]["arrive"], st[-1]["leave"]
        mins = _minutes(s0, s1)
        if all(x["charge"] for x in st) and mins is not None and mins < CHARGE_STOP_MAX_MIN:
            continue                    # charging on the way: travel time
        named = [x for x in st if x["dest"] and x["dest"].strip().lower() not in home.names]
        main = [x for x in named if not x["back"]] or named
        sites.append({
            "start": s0, "end": s1, "minutes": mins,
            "destinations": _unique(x["dest"] for x in main),
            "addresses": _addresses(a for x in st for a in x["addrs"]),
            "purpose": _most_common(x["purpose"] for x in (main or st)),
        })

    # Colour of the outing: the purpose with the most time on site.
    weight = {}
    for x in sites:
        if x["purpose"]:
            weight[x["purpose"]] = weight.get(x["purpose"], 0) + (x["minutes"] or 0)
    purpose = (max(weight, key=lambda k: (weight[k], k)) if weight
               else _most_common(t.get("purpose") for t in trips))
    return {
        "start": start, "end": end, "minutes": _minutes(start, end),
        "addresses": _addresses(a for s in sites for a in s["addresses"]),
        "overlap": overlap,
        "purpose": purpose,
        "complete": bool(o.get("starts_home") and o.get("ends_home")),
        "flagged": any(t.get("is_manual") or t.get("manual_fields") for t in trips),
        "km": round(sum(t.get("distance_km") or 0 for t in trips), 1),
        "sites": sites,
    }


def home_points(db):
    """Home positions: the "Zuhause" geofence(s) and home charge locations."""
    pts, names = [], set()
    for r in db.execute("SELECT name, lat, lon FROM charge_locations WHERE is_home = 1"):
        pts.append((r["lat"], r["lon"]))
        names.add(r["name"])
    for r in db.execute("SELECT name, lat, lon FROM locations"):
        if (r["name"] or "").strip().lower() in HOME_NAMES:
            pts.append((r["lat"], r["lon"]))
            names.add(r["name"])
    return pts, names
