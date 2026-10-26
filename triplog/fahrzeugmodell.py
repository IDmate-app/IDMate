"""Vehicle model: the one place for battery and charging figures.

Step 1 of docs/fahrzeugmodell.md:
- battery segments: a vehicle can get a new battery; capacity is fitted per
  battery, never across a swap
- capacity trend per segment (Theil-Sen), level or nominal value when a
  segment has too few points yet
- detection of a capacity jump that looks like an unrecorded swap
- own measurement from the car's energy counter (kw): capacity and charging
  efficiency (AC/DC) without the BMS capacity estimate, which is only shown
  for comparison

Pure functions plus small DB helpers; app.py keeps the point collection.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Fit a slope only with enough points over enough time; below that a level.
MIN_FIT_POINTS = 6
MIN_FIT_DAYS = 60
MIN_LEVEL_POINTS = 3
PLAUSI_MIN_KWH = 40.0
PLAUSI_MAX_KWH = 110.0
NOMINAL_TOLERANCE = 1.05
# Swap detection: a lasting upward step of at least this share, seen in at
# least STEP_MIN_SIDE points on both sides.
STEP_MIN_JUMP = 0.08
STEP_MIN_SIDE = 5
STEP_WINDOW = 15
# Plausible charging efficiency (battery ÷ metered kWh)
EFF_MIN, EFF_MAX = 0.75, 1.0


def median(vals):
    vals = sorted(vals)
    if not vals:
        return None
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2.0


def mad(vals):
    m = median(vals)
    return median([abs(v - m) for v in vals]) if vals else None


THEIL_SEN_MAX = 300   # points for the slope: pairs grow with n², 733 → 270,000


def theil_sen(xy):
    """Robust line fit: median of all pairwise slopes. xy = [(x, y), ...].
    Above THEIL_SEN_MAX points the slope comes from that many spread evenly
    over the series (the median of 45,000 pairs is as robust as of 270,000,
    and a request on a NAS no longer stalls the others); the intercept
    always uses all points."""
    n = len(xy)
    if n < 2:
        return None
    sub = xy
    if n > THEIL_SEN_MAX:
        step = (n - 1) / (THEIL_SEN_MAX - 1)
        sub = [xy[round(i * step)] for i in range(THEIL_SEN_MAX)]
    k = len(sub)
    slopes = [(sub[j][1] - sub[i][1]) / (sub[j][0] - sub[i][0])
              for i in range(k) for j in range(i + 1, k) if sub[j][0] != sub[i][0]]
    if not slopes:
        return None
    m = median(slopes)
    return m, median([y - m * x for x, y in xy])


# ── Battery segments ───────────────────────────────────────────────────────

def batteries(db, plate):
    """Battery segments of a vehicle, oldest first:
    [{id, from (date or None), nominal, note}]. The first one is the
    original battery (nominal = vehicles.battery_capacity_kwh)."""
    v = db.execute("SELECT id, battery_capacity_kwh FROM vehicles WHERE plate = ?", (plate,)).fetchone()
    if not v:
        return [{"id": None, "from": None, "nominal": None, "note": None}]
    segs = [{"id": None, "from": None,
             "nominal": float(v["battery_capacity_kwh"]) if v["battery_capacity_kwh"] else None,
             "note": None}]
    for r in db.execute("SELECT id, installed_at, nominal_kwh, note FROM vehicle_batteries "
                        "WHERE vehicle_id = ? ORDER BY installed_at", (v["id"],)):
        segs.append({"id": r["id"], "from": r["installed_at"][:10],
                     "nominal": float(r["nominal_kwh"]) if r["nominal_kwh"] else segs[-1]["nominal"],
                     "note": r["note"]})
    return segs


def _day(ts):
    """ISO string or unix seconds → 'YYYY-MM-DD' (local)."""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    return str(ts)[:10]


def segment_index(segs, ts):
    day = _day(ts)
    idx = 0
    for i, s in enumerate(segs):
        if s["from"] is None or s["from"] <= day:
            idx = i
    return idx


def bounds(nominal):
    """Plausible capacity range for a battery of this nominal size."""
    if not nominal:
        return PLAUSI_MIN_KWH, PLAUSI_MAX_KWH
    return min(PLAUSI_MIN_KWH, 0.6 * nominal), min(PLAUSI_MAX_KWH, nominal * NOMINAL_TOLERANCE)


# ── Capacity per segment ───────────────────────────────────────────────────

def fit_segment(points, nominal, now_ts):
    """points: [{ts, cap}] of one battery. Returns a dict with kind
    ('trend' | 'level' | 'nominal' | 'none'), cap_at(ts), cap_now, n,
    slope_per_year (trend over ≥180 days only), span_days."""
    lo, hi = bounds(nominal)

    def clamp(v):
        return max(lo, min(hi, v))

    pts = sorted(points, key=lambda p: p["ts"])
    n = len(pts)
    span = (pts[-1]["ts"] - pts[0]["ts"]) / 86400.0 if n > 1 else 0
    if n >= MIN_FIT_POINTS and span >= MIN_FIT_DAYS:
        m, b = theil_sen([(p["ts"] / 86400.0, p["cap"]) for p in pts])

        def cap_at(ts):
            return clamp(m * (ts / 86400.0) + b)
        return {"kind": "trend", "n": n, "span_days": round(span), "cap_at": cap_at,
                "cap_now": round(cap_at(now_ts), 2),
                "slope_per_year": round(m * 365.0, 2) if span >= 180 else None}
    if n >= MIN_LEVEL_POINTS or (n and not nominal):
        level = clamp(median([p["cap"] for p in pts]))
        kind = "level"
    elif nominal:
        level, kind = nominal, "nominal"
    else:
        return {"kind": "none", "n": n, "span_days": round(span), "cap_at": lambda ts: None,
                "cap_now": None, "slope_per_year": None}
    return {"kind": kind, "n": n, "span_days": round(span), "cap_at": lambda ts, _l=level: _l,
            "cap_now": round(level, 2), "slope_per_year": None}


def capacity_model(points, segs, now_ts):
    """Per-segment fits over all capacity points; cap_at dispatches by date.
    Returns {segments: [...], current: fit of the newest battery, cap_at}."""
    fits = []
    for i, s in enumerate(segs):
        own = [p for p in points if segment_index(segs, p["ts"]) == i]
        f = fit_segment(own, s["nominal"], now_ts)
        f.update({"from": s["from"], "nominal": s["nominal"], "id": s["id"], "note": s["note"],
                  "points": own})
        fits.append(f)

    def cap_at(ts):
        return fits[segment_index(segs, ts)]["cap_at"](ts)
    return {"segments": fits, "current": fits[-1], "cap_at": cap_at}


# ── SoC profile: kWh per % is not the same over the whole SoC range ────────

SOC_BANDS = ((0, 20), (20, 40), (40, 60), (60, 80), (80, 100))
PROFILE_MIN_POINTS = 20
PROFILE_PRIOR = 3.0        # pseudo charges pulling every band to the mean
PROFILE_CLIP = (0.7, 1.3)


def _band_overlap(s0, s1):
    s0, s1 = max(0.0, float(s0)), min(100.0, float(s1))
    return [max(0.0, min(s1, b) - max(s0, a)) for a, b in SOC_BANDS]


def _solve(rows, ys, ws):
    """Weighted least squares for a few unknowns (normal equations)."""
    n = len(rows[0])
    m = [[sum(w * r[i] * r[j] for r, w in zip(rows, ws)) for j in range(n)] for i in range(n)]
    v = [sum(w * r[i] * y for r, y, w in zip(rows, ys, ws)) for i in range(n)]
    for i in range(n):
        p = max(range(i, n), key=lambda k: abs(m[k][i]))
        m[i], m[p], v[i], v[p] = m[p], m[i], v[p], v[i]
        if abs(m[i][i]) < 1e-12:
            return None
        for k in range(i + 1, n):
            f = m[k][i] / m[i][i]
            for j in range(i, n):
                m[k][j] -= f * m[i][j]
            v[k] -= f * v[i]
    x = [0.0] * n
    for i in reversed(range(n)):
        x[i] = (v[i] - sum(m[i][j] * x[j] for j in range(i + 1, n))) / m[i][i]
    return x


def soc_profile(points, cap_at):
    """How much energy one % of SoC holds in each band, relative to the mean
    (1.0). The displayed SoC is not linear in energy: the ID.7 stores about
    0.81 kWh per % at 20-40 % and 0.92 at 80-100 %, so a charge 20→80 reads
    a lower capacity than 60→100. points: [{ts, kwh, s0, s1}], cap_at(ts):
    the capacity at that time (the first, uncorrected fit). Robust (Huber),
    bands with few charges stay near 1. None with too few points."""
    use = [p for p in points if p.get("s0") is not None and p.get("s1") is not None and p.get("kwh")]
    if len(use) < PROFILE_MIN_POINTS:
        return None
    rows, ys = [], []
    for p in use:
        cap = cap_at(p["ts"])
        if not cap:
            continue
        rows.append(_band_overlap(p["s0"], p["s1"]))
        ys.append(p["kwh"] / cap * 100)            # SoC span the energy is worth
    if len(rows) < PROFILE_MIN_POINTS:
        return None
    width = [b - a for a, b in SOC_BANDS]
    for i, w in enumerate(width):                  # prior: every band = 1.0
        rows.append([w if j == i else 0.0 for j in range(len(width))])
        ys.append(float(w))
    n = len(rows) - len(width)
    ws = [1.0] * n + [PROFILE_PRIOR] * len(width)
    x = None
    for _ in range(6):
        x = _solve(rows, ys, ws)
        if x is None:
            return None
        res = [y - sum(a * b for a, b in zip(r, x)) for r, y in zip(rows[:n], ys[:n])]
        scale = (median([abs(r) for r in res]) or 1e-9) * 1.4826
        ws = [1.0 / max(1.0, abs(r) / (2 * scale)) for r in res] + [PROFILE_PRIOR] * len(width)
    mean = sum(x) / len(x)
    if mean <= 0:
        return None
    lo, hi = PROFILE_CLIP
    return [round(min(hi, max(lo, v / mean)), 3) for v in x]


def apply_soc_profile(points, shape):
    """Capacity per point with the SoC span weighted by the profile:
    kWh ÷ Σ (span in band × weight). Keeps the raw value as cap_raw."""
    out = []
    for p in points:
        q = dict(p)
        if shape and p.get("s0") is not None and p.get("s1") is not None and p.get("kwh"):
            span = sum(o * w for o, w in zip(_band_overlap(p["s0"], p["s1"]), shape))
            if span > 0:
                q["cap_raw"], q["cap"] = p["cap"], p["kwh"] / span * 100
        out.append(q)
    return out


# DC and AC points: the counter (and a DC receipt) sees the energy at the
# battery terminals; at 300+ A a part of it turns into heat in the cells
# instead of being stored, at 11 kW hardly any. DC points therefore read a
# little more capacity. Learned per vehicle, only when it is clear.
DC_OFFSET_MIN_N = 10
DC_OFFSET_MAX = 0.06


def dc_offset(points, cap_at):
    """Relative level of DC points over AC points around the fitted line:
    median(cap / cap_at − 1) of DC minus that of AC. None unless both kinds
    have DC_OFFSET_MIN_N points and the difference exceeds twice its
    standard error (and stays below DC_OFFSET_MAX)."""
    res = {True: [], False: []}
    for p in points:
        c = cap_at(p["ts"])
        if c and p.get("dc") is not None:
            res[bool(p["dc"])].append(p["cap"] / c - 1)
    dc, ac = res[True], res[False]
    if len(dc) < DC_OFFSET_MIN_N or len(ac) < DC_OFFSET_MIN_N:
        return None
    off = median(dc) - median(ac)

    def spread(v):
        m = sum(v) / len(v)
        return (sum((x - m) ** 2 for x in v) / (len(v) - 1)) ** 0.5
    se = 1.2533 * (spread(dc) ** 2 / len(dc) + spread(ac) ** 2 / len(ac)) ** 0.5
    if abs(off) < 2 * se or abs(off) > DC_OFFSET_MAX:
        return None
    return round(off, 4)


def apply_dc_offset(points, off):
    """DC points on the AC level: energy (and capacity) ÷ (1 + off)."""
    out = []
    for p in points:
        q = dict(p)
        if off and p.get("dc"):
            q["cap"] = p["cap"] / (1 + off)
            if p.get("kwh"):
                q["kwh"] = p["kwh"] / (1 + off)
        out.append(q)
    return out


def rolling_median(points, segs, n=10):
    """Median of the last n capacity points (within one battery) at every
    point, for a continuous line through the scatter. [{ts, cap}]"""
    out, by_seg = [], {}
    for p in sorted(points, key=lambda p: p["ts"]):
        win = by_seg.setdefault(segment_index(segs, p["ts"]), [])
        win.append(p["cap"])
        out.append({"ts": p["ts"], "cap": round(median(win[-n:]), 2) if len(win) >= min(n, 3) else None,
                    "seg": segment_index(segs, p["ts"])})
    return out


def detect_battery_change(points, min_jump=STEP_MIN_JUMP, side=STEP_MIN_SIDE, window=STEP_WINDOW):
    """A lasting upward capacity step inside one segment, which is what a
    new battery looks like. points sorted or not: [{ts, cap}].
    Returns {date, before, after, n_after} for the strongest step, or None."""
    pts = sorted(points, key=lambda p: p["ts"])
    caps = [p["cap"] for p in pts]
    cands = []
    for i in range(side, len(pts) - side + 1):
        before, after = caps[max(0, i - window):i], caps[i:i + window]
        mb, ma = median(before), median(after)
        jump = ma - mb
        if jump < min_jump * mb:
            continue            # the cheap test first: most positions end here
        noise = (mad(before) or 0) + (mad(after) or 0)
        # the rest of the series must stay up, not only the window
        if jump >= 1.5 * noise and median(caps[i:]) - mb >= min_jump * mb:
            cands.append({"i": i, "jump": jump, "before": round(mb, 1), "after": round(ma, 1)})
    if not cands:
        return None
    # Few points right after a swap blur the step over several positions:
    # take the earliest one that is nearly as strong as the strongest.
    top = max(c["jump"] for c in cands)
    best = next(c for c in cands if c["jump"] >= 0.8 * top)
    p = pts[best["i"]]
    return {"date": _day(p["ts"]), "before": best["before"], "after": best["after"],
            "n_after": len(pts) - best["i"]}


# ── Charging efficiency ────────────────────────────────────────────────────

# ── Energy book ────────────────────────────────────────────────────────────

# VW energy counter "kw": energy into the battery (charging + recuperation),
# sent as u16 / 10 → wraps at 6553.6 kWh.
KW_WRAP = 6553.6


def kw_delta(a, b):
    """Increase of the kw counter from a to b, across a wrap."""
    if a is None or b is None:
        return None
    d = b - a
    if d < -KW_WRAP / 2:
        d += KW_WRAP
    return d


# Charging sessions faster than this are DC (same rule as the charge stats).
DC_MIN_KW = 22.0


def is_dc(session):
    """DC charge: average power above an AC wallbox's 22 kW."""
    kw = session.get("avg_kw") if hasattr(session, "get") else session["avg_kw"]
    return kw is not None and kw > DC_MIN_KW


# ── Consumption: one rule ─────────────────────────────────────────────────

# A trip's consumption and every average over trips count trips from this
# distance on. Below it the whole-percent SoC steps dominate, and trips
# with a 0 % step carry no energy at all — short trips would bias upwards.
MIN_CONS_KM = 10


def trip_consumption(energy, km):
    """kWh/100 km of one trip, None below MIN_CONS_KM or without energy."""
    return round(energy / km * 100, 1) if energy and km and km >= MIN_CONS_KM else None


def avg_consumption(trips, trim=0.0):
    """km-weighted battery-side consumption Σ kWh ÷ Σ km × 100 over trips
    [{energy_kwh, distance_km}] with energy and ≥ MIN_CONS_KM. trim drops
    that share of the highest and lowest trips (the dashboard's range
    window). → {cons, km, kwh, n} or None."""
    rows = [(float(t["energy_kwh"]), float(t["distance_km"])) for t in trips
            if t.get("energy_kwh") and t["energy_kwh"] > 0
            and t.get("distance_km") and t["distance_km"] >= MIN_CONS_KM]
    if trim and len(rows) >= 5:
        k = max(1, int(len(rows) * trim))
        rows = sorted(rows, key=lambda r: r[0] / r[1])[k:len(rows) - k]
    kwh, km = sum(r[0] for r in rows), sum(r[1] for r in rows)
    if not km:
        return None
    return {"cons": round(kwh / km * 100, 1), "km": round(km, 1), "kwh": round(kwh, 2), "n": len(rows)}


# ── Energy book: grid and battery side of every charge ────────────────────

import re as _re
_TM_USED = _re.compile(r"kwh_used=([0-9.]+)")


def session_energy(s, eff_ac, eff_dc=None, cap=None):
    """Both sides of one charge: {"grid", "batt", "source", "measured"}.
    charge_sessions.total_kwh holds different things by source:
      wallbox    (is_external = 0)        grid, metered
      receipt    (external with a price)  grid, billed
      teslamate  ("TM-…")                 battery (charge_energy_added); the
                                          grid side from the import note
                                          (kwh_used) when it is there
      estimate   (external, no price)     battery, ΔSoC × capacity — with
                                          cap the SoC span is used again
    The other side follows with the efficiency (DC: eff_dc, else eff_ac).
    None for a charge without energy."""
    tk = s.get("total_kwh")
    eff = (eff_dc or eff_ac) if (s.get("is_external") and is_dc(s)) else eff_ac
    num = str(s.get("session_number") or "")
    if num.startswith("TM-"):
        if not tk:
            return None
        m = _TM_USED.search(str(s.get("note") or ""))
        used = float(m.group(1)) if m else None
        grid = used if used and tk * 0.8 <= used <= tk * 1.5 else (tk / eff if eff else tk)
        return {"grid": grid, "batt": tk, "source": "teslamate", "measured": True}
    if not s.get("is_external") or (s.get("cost_total") or 0) > 0:
        if not tk:
            return None
        return {"grid": tk, "batt": tk * eff if eff else tk,
                "source": "receipt" if s.get("is_external") else "wallbox", "measured": True}
    a, b = s.get("soc_start"), s.get("soc_end")
    batt = (b - a) / 100.0 * cap if cap and a is not None and b is not None and b > a else tk
    if not batt:
        return None
    return {"grid": batt / eff if eff else batt, "batt": batt, "source": "estimate", "measured": False}


# ── The shared wallbox meter ───────────────────────────────────────────────

METER_EPS = 1e-6  # meter values carry 2-3 decimals; a 10 Wh reading is real


def covered_readings(readings):
    """Readings whose meter span is already fully covered by readings stored
    before them (smaller id) — energy counted twice. The wallbox meter is
    shared by all vehicles and only grows, so a span can belong to one
    reading only. readings: [{id, meter_start, meter_end}] (any order).
    Returns the ids of the covered ones."""
    import bisect
    starts, ends = [], []          # merged, disjoint intervals sorted by start
    out = []
    for r in sorted(readings, key=lambda r: r["id"]):
        a, b = r.get("meter_start"), r.get("meter_end")
        if a is None or b is None or b <= a:
            continue
        i = bisect.bisect_right(starts, a + METER_EPS) - 1
        if i >= 0 and ends[i] >= b - METER_EPS:
            out.append(r["id"])
            continue
        # merge [a, b] into the union
        lo = bisect.bisect_left(ends, a - METER_EPS)
        hi = bisect.bisect_right(starts, b + METER_EPS)
        if lo < hi:
            a, b = min(a, starts[lo]), max(b, ends[hi - 1])
        starts[lo:hi] = [a]
        ends[lo:hi] = [b]
    return out


DEFAULT_BAT_KWH = 86.5     # kWh when neither vehicle nor setting has a capacity


# ── Buffer watch: rest voltage at a displayed SoC over the months ──────────

# If the car opens its hidden buffer, the energy between displayed 0 and 100 %
# stays the same — the window slides into the buffer, and a displayed SoC then
# sits at a higher (top) or lower (bottom) cell voltage than before. Energy and
# SoC cannot show that; the rest voltage at a displayed SoC can.
BUFFER_BANDS = {"top": (70.0, 90.0, 80.0), "bottom": (20.0, 40.0, 30.0)}
BUFFER_REF_C = 20.0
BUFFER_MIN_MONTH = 5        # rest samples a month needs to count
BUFFER_FIT_MAX = 300        # samples for the robust slope fits (O(n²))
BUFFER_SHIFT_PCT = 1.0      # a shift of this many % SoC is a hint


def _thin(rows, n):
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


def buffer_watch(samples):
    """samples: rest moments [{ts, s, u, bt}] (parked, current about 0).
    Per band: the voltage at the band's SoC (center) at BUFFER_REF_C, as a
    monthly median, and how far it moved from the first to the last months,
    in volts and in % SoC (volts ÷ the slope of the voltage curve there).
    → {band: {center, slope_v_per_pct, temp_v_per_c, months: [{month, u, n}],
              shift_v, shift_pct, hint}} — hint: "released" (moved outwards
    by ≥ BUFFER_SHIFT_PCT), "none" (< half of it over ≥ 3 months) or None."""
    out = {}
    for band, (lo, hi, center) in BUFFER_BANDS.items():
        rows = [r for r in samples if r.get("u") and r.get("s") is not None and lo <= r["s"] <= hi]
        if len(rows) < 2 * BUFFER_MIN_MONTH:
            continue
        rows.sort(key=lambda r: r["ts"])
        fit = theil_sen([(r["s"], r["u"]) for r in _thin(rows, BUFFER_FIT_MAX)])
        if not fit or fit[0] <= 0:
            continue
        slope = fit[0]
        resid = [(r["bt"], r["u"] - slope * (r["s"] - center)) for r in rows if r.get("bt") is not None]
        tfit = theil_sen(_thin(resid, BUFFER_FIT_MAX)) if len(resid) >= 10 else None
        tc = tfit[0] if tfit and abs(tfit[0]) < 2 else 0.0
        by = {}
        for r in rows:
            u = r["u"] - slope * (r["s"] - center)
            if r.get("bt") is not None:
                u -= tc * (r["bt"] - BUFFER_REF_C)
            by.setdefault(datetime.fromtimestamp(r["ts"]).strftime("%Y-%m"), []).append(u)
        months = [{"month": m, "u": round(median(v), 2), "n": len(v)}
                  for m, v in sorted(by.items()) if len(v) >= BUFFER_MIN_MONTH]
        res = {"center": center, "slope_v_per_pct": round(slope, 3), "temp_v_per_c": round(tc, 3),
               "months": months, "shift_v": None, "shift_pct": None, "hint": None}
        if len(months) >= 2:
            k = 2 if len(months) >= 4 else 1
            first = median([m["u"] for m in months[:k]])
            last = median([m["u"] for m in months[-k:]])
            shift = last - first
            pct = shift / slope
            res["shift_v"], res["shift_pct"] = round(shift, 2), round(pct, 2)
            outward = pct if band == "top" else -pct      # top up / bottom down = buffer used
            if outward >= BUFFER_SHIFT_PCT:
                res["hint"] = "released"
            elif len(months) >= 3 and abs(pct) < BUFFER_SHIFT_PCT / 2:
                res["hint"] = "none"
        out[band] = res
    return out


def bms_resets(daily, min_jump=0.3):
    """The BMS capacity estimate drifts down and jumps back to its ceiling
    after a recalibration. daily: {date: kWh}. → {ceiling, resets: [dates]}"""
    if not daily:
        return None
    days = sorted(daily)
    ceiling = max(daily.values())
    resets = [b for a, b in zip(days, days[1:])
              if daily[b] - daily[a] >= min_jump and daily[b] >= ceiling - 0.15]
    return {"ceiling": round(ceiling, 2), "resets": resets}


# ── Flat rate for home charges ────────────────────────────────────────────

DEFAULT_FLAT_RATE = 0.34   # EUR/kWh when no tariff is entered


def flat_rates(tariffs):
    """rate(ts) → EUR/kWh of the tariff valid on that day. tariffs: rows with
    valid_from and pauschale_kwh. Before the first tariff the first one, no
    day the newest, no tariff at all DEFAULT_FLAT_RATE."""
    rows = sorted(((str(t["valid_from"]), t["pauschale_kwh"]) for t in tariffs), key=lambda t: t[0])

    def rate(ts=None):
        if not rows:
            return DEFAULT_FLAT_RATE
        if not ts:
            return rows[-1][1]
        day = str(ts)[:10]
        r = rows[0][1]
        for valid_from, v in rows:
            if valid_from <= day:
                r = v
        return r
    return rate


def flat_rate(db, ts=None):
    """EUR/kWh of the tariff valid at ts (see flat_rates)."""
    return flat_rates(db.execute("SELECT valid_from, pauschale_kwh FROM charge_tariffs").fetchall())(ts)


import config as _config
LOCAL_TZ = _config.LOCAL_TZ   # reading timestamps: local, no offset


def _parse_local(ts):
    try:
        return datetime.fromisoformat(str(ts).replace(" ", "T")[:19])
    except (TypeError, ValueError):
        return None


def _ambiguous(dt):
    return (dt.replace(tzinfo=LOCAL_TZ, fold=0).utcoffset()
            != dt.replace(tzinfo=LOCAL_TZ, fold=1).utcoffset())


def order_readings(readings):
    """Charge readings in true time order, as dicts with "_utc" (epoch s).
    Timestamps are local time without offset, and the night the clocks go
    back (last Sunday of October) 02:00-02:59 happens twice: sorted by the
    text, the second pass lands before the first and a session's first meter
    comes out wrong (+8 kWh over- and -8 kWh undercount, 25.10.2026). The
    shared meter only grows, so inside that hour the meter gives the order
    and the second pass starts where the wall clock jumps back. Readings
    there without a meter take the pass their created_at (UTC) is closer to."""
    rows = [dict(r) for r in readings]
    for d in rows:
        d["_dt"] = _parse_local(d.get("timestamp"))
    def by_created(d):
        c = _parse_local(d.get("created_at"))
        if c is None:
            return 0
        c = c.replace(tzinfo=timezone.utc).timestamp()
        return min((0, 1), key=lambda f: abs(d["_dt"].replace(tzinfo=LOCAL_TZ, fold=f).timestamp() - c))

    fold = {}                                   # id(d) -> 0/1 in the double hour
    amb = [d for d in rows if d["_dt"] is not None and _ambiguous(d["_dt"])]
    for day in {d["_dt"].date() for d in amb}:
        block = [d for d in amb if d["_dt"].date() == day]
        metered = sorted((d for d in block if d.get("meter_start") is not None),
                         key=lambda d: (float(d["meter_start"]), d.get("id") or 0))
        f, prev = (by_created(metered[0]) if metered else 0), None
        for d in metered:
            if prev is not None and d["_dt"] < prev:
                f = 1
            fold[id(d)], prev = f, d["_dt"]
        for d in block:
            if id(d) not in fold:
                fold[id(d)] = by_created(d)
    for d in rows:
        dt = d.pop("_dt")
        d["_utc"] = (dt.replace(tzinfo=LOCAL_TZ, fold=fold.get(id(d), 0)).timestamp()
                     if dt is not None else None)
    rows.sort(key=lambda d: (d["_utc"] is None, d["_utc"] or 0,
                             float(d["meter_start"]) if d.get("meter_start") is not None else 0,
                             d.get("id") or 0))
    return rows


def session_meters(readings):
    """{session_id: (first meter_start, last meter_end)} in true time order."""
    out = {}
    for r in order_readings(readings):
        sid = r.get("session_id")
        if sid is None:
            continue
        a, b = out.get(sid, (None, None))
        if a is None and r.get("meter_start") is not None:
            a = r["meter_start"]
        if r.get("meter_end") is not None:
            b = r["meter_end"]
        out[sid] = (a, b)
    return out


def meter_gaps(sessions, min_kwh=0.05):
    """Gaps on the shared meter: sessions [{first_meter, last_meter, ...}]
    sorted by start time. A session's first meter is compared with the
    highest meter reached by ANY earlier session — a session of the other
    car that started before and ended after must not open a phantom gap.
    Returns [(before_session, session, kwh_missing)]."""
    out = []
    top = None                      # session holding the highest meter so far
    for s in sessions:
        if s["first_meter"] is not None and top is not None:
            delta = float(s["first_meter"]) - float(top["last_meter"])
            if delta >= min_kwh:
                out.append((top, s, round(delta, 3)))
        if s["last_meter"] is not None and (top is None or float(s["last_meter"]) > float(top["last_meter"])):
            top = s
    return out


# ── Own measurement from the energy counter (no BMS estimate) ─────────────

COUNTER_MIN_KWH = 5.0
COUNTER_MIN_DSOC = 20.0


def counter_charges(trips):
    """Charges seen by the car's energy counter: between two consecutive
    trips (car parked) the counter grows by the energy put into the battery.
    trips: [{start_time, end_time, kw_start, kw_end, soc_start, soc_end}]
    sorted by start. Yields dicts {after (end of trip before), before (start
    of trip after), kwh (battery side), dsoc}."""
    out = []
    for a, b in zip(trips, trips[1:]):
        dk = kw_delta(a.get("kw_end"), b.get("kw_start"))
        if dk is None or dk < COUNTER_MIN_KWH:
            continue
        dsoc = None
        if a.get("soc_end") is not None and b.get("soc_start") is not None:
            dsoc = b["soc_start"] - a["soc_end"]
        try:
            hours = (datetime.fromisoformat(str(b["start_time"]).replace(" ", "T")[:19])
                     - datetime.fromisoformat(str(a["end_time"]).replace(" ", "T")[:19])).total_seconds() / 3600
        except (TypeError, ValueError):
            hours = None
        # parked so briefly that only a fast charger can have delivered it
        dc = bool(hours and hours > 0 and dk / hours > DC_MIN_KW)
        out.append({"after": a["end_time"], "before": b["start_time"], "kwh": dk, "dsoc": dsoc,
                    "s0": a.get("soc_end"), "s1": b.get("soc_start"), "dc": dc})
    return out


def counter_capacity_points(trips, segs):
    """Capacity = counter energy ÷ SoC gain, per charge with ≥ 20 % gain;
    plausible for the battery fitted then. [{ts, cap, date, kwh, dsoc}]"""
    pts = []
    for c in counter_charges(trips):
        if c["dsoc"] is None or c["dsoc"] < COUNTER_MIN_DSOC:
            continue
        cap = c["kwh"] / (c["dsoc"] / 100.0)
        lo, hi = bounds(segs[segment_index(segs, c["before"])]["nominal"])
        if not (lo <= cap <= hi):
            continue
        try:
            ts = datetime.fromisoformat(str(c["before"]).replace(" ", "T")[:19]).timestamp()
        except ValueError:
            continue
        pts.append({"ts": ts, "cap": cap, "date": c["before"], "kwh": round(c["kwh"], 2),
                    "dsoc": round(c["dsoc"], 1), "s0": c["s0"], "s1": c["s1"], "dc": c["dc"]})
    return pts


def counter_efficiency(trips, sessions):
    """Charging efficiency without any capacity estimate: energy the counter
    saw going into the battery ÷ kWh metered (wallbox) or billed (receipt),
    for parking stops with exactly one recorded charge.
    sessions: [{start_time, total_kwh, is_external, avg_kw, session_number}].
    Returns {"ac": result|None, "dc": result|None}, result = {eff, n, p25, p75}."""
    sess = sorted((s for s in sessions if s.get("total_kwh") and s["total_kwh"] >= COUNTER_MIN_KWH
                   and not str(s.get("session_number") or "").startswith("TM-")),
                  key=lambda s: str(s["start_time"]).replace(" ", "T"))
    starts = [str(s["start_time"]).replace(" ", "T")[:16] for s in sess]
    vals = {"ac": [], "dc": []}
    import bisect
    for c in counter_charges(trips):
        a, b = str(c["after"])[:16], str(c["before"])[:16]
        i, j = bisect.bisect_left(starts, a), bisect.bisect_left(starts, b)
        if j - i != 1:
            continue
        s = sess[i]
        e = c["kwh"] / s["total_kwh"]
        kind = "dc" if s.get("is_external") and is_dc(s) else ("ac" if not s.get("is_external") else None)
        if kind and EFF_MIN <= e <= 1.02:
            vals[kind].append(min(e, 1.0))
    out = {}
    for k, v in vals.items():
        if len(v) < 3:
            out[k] = None
            continue
        v.sort()
        out[k] = {"eff": round(median(v), 3), "n": len(v),
                  "p25": round(v[len(v) // 4], 3), "p75": round(v[(3 * len(v)) // 4], 3)}
    return out


# ── Vehicle report (analysis tab "Fahrzeug") ───────────────────────────────

SEASONS = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
           6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}


def percentile(vals, q):
    vals = sorted(vals)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(round(q * (len(vals) - 1))))]


def season_consumption(trips):
    """Battery-side kWh/100 km per season over trips with energy (≥ 10 km).
    trips: [{start_time, distance_km, energy_kwh}] → {season: {cons, km, n}}"""
    acc = {}
    for t in trips:
        km, e = t.get("distance_km"), t.get("energy_kwh")
        if not km or km < MIN_CONS_KM or not e or not t.get("start_time"):
            continue
        s = SEASONS[int(str(t["start_time"])[5:7])]
        a = acc.setdefault(s, [0.0, 0.0, 0])
        a[0] += e
        a[1] += km
        a[2] += 1
    return {s: {"cons": round(a[0] / a[1] * 100, 1), "km": round(a[1]), "n": a[2]}
            for s, a in acc.items() if a[1] >= 100}


def recuperation(trips):
    """Energy the counter saw going into the battery while driving
    (kw_end - kw_start): kWh/100 km and share of the gross consumption.
    None without counter data."""
    rec = km = net = 0.0
    n = 0
    for t in trips:
        d = kw_delta(t.get("kw_start"), t.get("kw_end"))
        dist, e = t.get("distance_km"), t.get("energy_kwh")
        if d is None or not (0 <= d < 50) or not dist or dist < MIN_CONS_KM or e is None:
            continue
        rec += d
        km += dist
        net += e
        n += 1
    if n < 5 or not km:
        return None
    return {"kwh_100km": round(rec / km * 100, 1), "share_pct": round(rec / (net + rec) * 100, 1),
            "n": n, "km": round(km)}


def charging_summary(sessions, home_names=(), eff_ac=None, eff_dc=None):
    """Charges of one vehicle: count, energy home/external, AC/DC, SoC
    window, cost per kWh, energy on the grid side (session_energy; without
    eff_ac the stored kWh). sessions: charge_sessions rows as dicts. A charge
    is at home when it came from the wallbox or its location is a home
    location (home_names) — TeslaMate imports at home are stored as external."""
    out = {"n": 0, "kwh": 0.0, "kwh_home": 0.0, "kwh_ext": 0.0, "kwh_ac": 0.0, "kwh_dc": 0.0,
           "n_dc": 0, "n_full": 0}
    starts, ends, cost, cost_kwh = [], [], 0.0, 0.0
    for s in sessions:
        e = session_energy(s, eff_ac, eff_dc)
        kwh = e["grid"] if e and eff_ac else s.get("total_kwh")   # grid side, one unit
        if not kwh or kwh <= 0:
            continue
        out["n"] += 1
        out["kwh"] += kwh
        ext = bool(s.get("is_external"))
        home = not ext or s.get("location_name") in home_names
        out["kwh_home" if home else "kwh_ext"] += kwh
        dc = not home and is_dc(s)
        out["kwh_dc" if dc else "kwh_ac"] += kwh
        out["n_dc"] += dc
        a, b = s.get("soc_start"), s.get("soc_end")
        if a is not None and b is not None and b > a:
            starts.append(a)
            ends.append(b)
            out["n_full"] += b >= 95
        c = s.get("cost_total") if ext else (s.get("cost_pauschale") or s.get("cost_tibber"))
        if c and c > 0:
            cost += c
            cost_kwh += kwh
    for k in ("kwh", "kwh_home", "kwh_ext", "kwh_ac", "kwh_dc"):
        out[k] = round(out[k], 1)
    out["soc_start_median"] = round(median(starts), 0) if starts else None
    out["soc_end_median"] = round(median(ends), 0) if ends else None
    out["full_pct"] = round(out["n_full"] / len(ends) * 100) if ends else None
    out["ct_per_kwh"] = round(cost / cost_kwh * 100, 1) if cost_kwh else None
    return out


def monthly_consumption(trips, min_km=200):
    """Battery-side kWh/100 km per month over trips with energy (≥ 10 km).
    → [{month 'YYYY-MM', cons, km}] for months with at least min_km."""
    acc = {}
    for t in trips:
        km, e = t.get("distance_km"), t.get("energy_kwh")
        if not km or km < MIN_CONS_KM or not e or not t.get("start_time"):
            continue
        a = acc.setdefault(str(t["start_time"])[:7], [0.0, 0.0])
        a[0] += e
        a[1] += km
    return [{"month": m, "cons": round(a[0] / a[1] * 100, 1), "km": round(a[1])}
            for m, a in sorted(acc.items()) if a[1] >= min_km]


TEMP_REF_C = 20.0
TEMP_FIT_MIN_MONTHS = 6


def temperature_fit(months):
    """Consumption against outside temperature over months [{cons, km,
    temp}]: km-weighted line cons = a + b·T. Returns the consumption at
    TEMP_REF_C and the increase per 10 °C colder, or None with too few
    months or too little temperature spread."""
    pts = [(m["temp"], m["cons"], m["km"]) for m in months if m.get("temp") is not None]
    if len(pts) < TEMP_FIT_MIN_MONTHS:
        return None
    w = sum(p[2] for p in pts)
    mt = sum(p[0] * p[2] for p in pts) / w
    mc = sum(p[1] * p[2] for p in pts) / w
    var = sum(p[2] * (p[0] - mt) ** 2 for p in pts) / w
    if var < 9:                      # under ±3 °C spread the slope is noise
        return None
    b = sum(p[2] * (p[0] - mt) * (p[1] - mc) for p in pts) / w / var
    a = mc - b * mt
    return {"at_ref": round(a + b * TEMP_REF_C, 1), "ref_c": TEMP_REF_C,
            "per_10c_colder": round(-10 * b, 1), "months": len(pts)}


# ── Data accuracy (stamp of the vehicle report) ────────────────────────────

ACCURACY_WEIGHTS = {"energy": 0.4, "gaps": 0.4, "findings": 0.2}


def data_accuracy(energy_pct, gap_km, km, errors, warnings):
    """How continuous and consistent the data behind the report is, 0-100,
    from three parts (each 0-1):
      energy    share of trips with energy
      gaps      1 − open odometer gap km ÷ recorded km (×10: 10 % gaps → 0)
      findings  1 − 0.1 per diagnostics error − 0.02 per warning
    The spread of the SoH measurements is no part of it: that is how exact
    the method can measure, not whether data is missing; it stands as ± at
    the SoH."""
    parts = {}
    parts["energy"] = (energy_pct or 0) / 100
    parts["gaps"] = max(0.0, 1 - 10 * (gap_km or 0) / km) if km else 0.0
    parts["findings"] = max(0.0, 1 - 0.1 * (errors or 0) - 0.02 * (warnings or 0))
    score = round(100 * sum(ACCURACY_WEIGHTS[k] * v for k, v in parts.items()))
    return {"score": score, "parts": {k: round(v * 100) for k, v in parts.items()}}
