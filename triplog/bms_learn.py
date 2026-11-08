"""BMS learning: what the car's display makes of the BMS.

The car shows a SoC, a range and an average consumption that differ from the
raw BMS SoC on the CAN bus (buffer at top and bottom, maybe temperature).
The user enters what the display shows; each entry is stored next to the CAN
values of that moment (bms_points). From those points:

  fit_display   display SoC = a + b·CAN SoC (+ c·battery °C + d·outside °C
                once there are enough points with enough temperature spread),
                robust (Huber), with leave-one-out error as the honest accuracy.
  range_offset  displayed range − CAN range; once stable, the range is taken
                from CAN and need not be typed in.
  fit_range     how VW turns that into km: range = display SoC × E ÷ the
                displayed average consumption. E (kWh per 100 % display) is
                learned, and which stretch of measured consumption (last 10,
                30, 50 or 100 km) the displayed average follows; and how far
                the range the stick reads from the car is off the display.
  real_range    an own range span: the energy left until the display reads 0 %
                divided by the consumption of past trips at a similar outside
                temperature (10th to 90th percentile).

Pure logic: the caller passes rows and series.
"""

import math

import fahrzeugmodell as fm

FRESH_S = 300                  # a CAN value older than this is not a point
WINDOWS_KM = (10, 30, 50, 100)
MIN_POINTS = 3                 # below: no display model
FULL_POINTS = 8                # from here temperature terms may join
TEMP_SPREAD_C = 2.0            # std. dev. a temperature needs to be a term
HUBER_DELTA = 0.75             # % SoC
RIDGE = 1.0                    # pulls the temperature terms to 0
BAT_REF_C, EXT_REF_C = 20.0, 10.0
TEMP_BAND_C = (4.0, 8.0)       # trips this close to today's temperature
MIN_BAND_KM = 150.0
RANGE_CAN_MIN_N = 5            # points with display AND CAN range before the
RANGE_CAN_SPREAD_KM = 3.0      # display range may come from CAN: offset stable


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# ── measured consumption over the last km ─────────────────────────────────

def window_consumption(series, cap_kwh, windows=WINDOWS_KM):
    """series: [(odometer km, CAN SoC %)] oldest first, up to now.
    kWh/100 km over the last N km from the SoC drop × capacity; None for a
    window the series does not reach or that holds a charge (SoC rise)."""
    pts = [(o, s) for o, s in series if o is not None and s is not None]
    out = {n: None for n in windows}
    if len(pts) < 2 or not cap_kwh:
        return out
    od_now, soc_now = pts[-1]
    for n in windows:
        peak = soc_now
        for o, s in reversed(pts):
            if s < peak - 1.0:         # going back in time SoC must not fall:
                break                  # that was a charge in between
            peak = max(peak, s)
            if od_now - o >= n:
                km = od_now - o
                if s > soc_now:
                    out[n] = round((s - soc_now) / 100.0 * cap_kwh / km * 100.0, 1)
                break
    return out


# ── display SoC ───────────────────────────────────────────────────────────

def _std(vals):
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))


def _terms(points):
    """Which regressors the points can carry: CAN SoC always, a temperature
    only with enough points and enough spread in it."""
    terms = ["can"]
    if len(points) >= FULL_POINTS:
        for key in ("bat_temp", "ext_temp"):
            vals = [_f(p.get(key)) for p in points]
            if all(v is not None for v in vals) and _std(vals) >= TEMP_SPREAD_C:
                terms.append(key)
    return terms


def _row(p, terms):
    r = [1.0]
    for t in terms:
        v = _f(p.get("can_soc")) if t == "can" else _f(p.get(t))
        ref = 0.0 if t == "can" else (BAT_REF_C if t == "bat_temp" else EXT_REF_C)
        r.append((v if v is not None else ref) - ref)
    return r


def _fit(points, terms):
    rows = [_row(p, terms) for p in points]
    ys = [_f(p["disp_soc"]) for p in points]
    k = len(rows[0])
    # Ridge rows: only the temperature coefficients are pulled to 0.
    ridge = [([0.0] * j + [1.0] + [0.0] * (k - j - 1), 0.0) for j in range(2, k)]
    w = [1.0] * len(rows)
    coef = None
    for _ in range(12):
        rr = rows + [r for r, _ in ridge]
        yy = ys + [y for _, y in ridge]
        ww = w + [RIDGE] * len(ridge)
        coef = fm._solve(rr, yy, ww)
        if coef is None:
            return None
        res = [y - sum(c * x for c, x in zip(coef, r)) for r, y in zip(rows, ys)]
        w = [1.0 if abs(e) <= HUBER_DELTA else HUBER_DELTA / abs(e) for e in res]
    return coef


def fit_display(points):
    """{terms, coef, n, mae, mae_loo} or None with too few points."""
    pts = [p for p in points if _f(p.get("disp_soc")) is not None and _f(p.get("can_soc")) is not None]
    if len(pts) < MIN_POINTS:
        return None
    terms = _terms(pts)
    coef = _fit(pts, terms)
    if coef is None:
        return None
    res = [_f(p["disp_soc"]) - sum(c * x for c, x in zip(coef, _row(p, terms))) for p in pts]
    loo = []
    for i in range(len(pts)):
        c = _fit(pts[:i] + pts[i + 1:], terms)
        if c is not None:
            loo.append(abs(_f(pts[i]["disp_soc"]) - sum(a * x for a, x in zip(c, _row(pts[i], terms)))))
    return {"terms": terms, "coef": [round(c, 5) for c in coef], "n": len(pts),
            "mae": round(sum(abs(e) for e in res) / len(res), 2),
            "mae_loo": round(sum(loo) / len(loo), 2) if loo else None}


def predict_display(model, can_soc, bat_temp=None, ext_temp=None):
    if not model or can_soc is None:
        return None
    p = {"can_soc": can_soc, "bat_temp": bat_temp, "ext_temp": ext_temp}
    return round(sum(c * x for c, x in zip(model["coef"], _row(p, model["terms"]))), 1)


def display_zero_can(model, bat_temp=None, ext_temp=None):
    """CAN SoC at which the display reads 0 % (the bottom buffer)."""
    if not model or abs(model["coef"][1]) < 1e-6:
        return None
    p0 = predict_display(model, 0.0, bat_temp, ext_temp)
    return round(-p0 / model["coef"][1], 2)


# ── VW range ──────────────────────────────────────────────────────────────

def range_offset(points):
    """How the displayed range relates to the range the stick reads from the
    car: {n, diff (median display − CAN), spread (median |deviation|),
    reliable}. Reliable = enough points and a stable offset; then the display
    range need not be typed in any more (display = CAN + diff)."""
    diffs = [_f(p["disp_range"]) - _f(p["can_range"]) for p in points
             if (_f(p.get("disp_range")) or 0) > 0 and _f(p.get("can_range")) is not None]
    if not diffs:
        return {"n": 0, "diff": None, "spread": None, "reliable": False}
    med = fm.median(diffs)
    spread = fm.median([abs(d - med) for d in diffs]) if len(diffs) > 1 else None
    return {"n": len(diffs), "diff": round(med, 1),
            "spread": round(spread, 1) if spread is not None else None,
            "reliable": len(diffs) >= RANGE_CAN_MIN_N and spread is not None and spread <= RANGE_CAN_SPREAD_KM}


def fill_range_from_can(points):
    """Points without a typed range get display range = CAN range + offset,
    once the offset is reliable. Typed values are never touched."""
    off = range_offset(points)
    if not off["reliable"]:
        return points
    out = []
    for p in points:
        if not (_f(p.get("disp_range")) or 0) > 0 and _f(p.get("can_range")) is not None:
            p = dict(p, disp_range=_f(p["can_range"]) + off["diff"], range_from_can=True)
        out.append(p)
    return out


def fit_range(points):
    """E (kWh per 100 % display) from range × consumption ÷ display SoC, the
    measured window the displayed average follows best, and the error of the
    rebuilt range. None without points carrying range and consumption."""
    off = range_offset(points)
    points = fill_range_from_can(points)
    rows = [p for p in points
            if (_f(p.get("disp_range")) or 0) > 0 and (_f(p.get("disp_cons")) or 0) > 0
            and (_f(p.get("disp_soc")) or 0) >= 5]
    if not rows:
        return None
    es = [_f(p["disp_range"]) * _f(p["disp_cons"]) / _f(p["disp_soc"]) for p in rows]
    e = fm.median(es)
    out = {"n": len(rows), "e_kwh": round(e, 2),
           "e_spread": round(fm.mad(es), 2) if len(es) > 1 else None,
           "range_mae": round(fm.median([abs(_f(p["disp_soc"]) * e / _f(p["disp_cons"]) - _f(p["disp_range"]))
                                         for p in rows]), 1),
           "window_km": None, "cons_factor": None, "cons_mae": None}
    best = None
    for n in WINDOWS_KM:
        pairs = [(_f(p["disp_cons"]), _f(p.get(f"cons_{n}"))) for p in rows]
        pairs = [(d, m) for d, m in pairs if m and m > 0]
        if len(pairs) < max(2, len(rows) // 2):
            continue
        k = fm.median([d / m for d, m in pairs])
        err = fm.median([abs(d - k * m) for d, m in pairs])
        if best is None or err < best[2]:
            best = (n, k, err)
    if best:
        out.update(window_km=best[0], cons_factor=round(best[1], 3), cons_mae=round(best[2], 1))
    # SoE (firmware 1.5.0+): the energy the BMS itself counts, per 100 % display
    soe = [_f(p["soe"]) / _f(p["disp_soc"]) * 100 for p in rows if _f(p.get("soe"))]
    out["soe_e_kwh"] = round(fm.median(soe), 2) if soe else None
    # The range the stick reads from the car: the displayed number or not?
    out["can_range_n"] = off["n"]
    out["can_range_diff"] = off["diff"]
    out["can_range_spread"] = off["spread"]
    out["can_range_reliable"] = off["reliable"]
    return out


# ── own range span ────────────────────────────────────────────────────────

def _wpct(pairs, q):
    """Weighted percentile of [(value, weight)]."""
    pairs = sorted(pairs)
    tot = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= q * tot:
            return v
    return pairs[-1][0]


def real_range(remaining_kwh, trips, temp_now):
    """trips: [{cons, km, temp}] (trip consumption, distance, outside °C of
    that day). Range span from the 90th / 50th / 10th percentile of the
    km-weighted consumption at a similar temperature."""
    if not remaining_kwh or remaining_kwh <= 0:
        return None
    ok = [t for t in trips if (t.get("km") or 0) >= fm.MIN_CONS_KM and (t.get("cons") or 0) > 0]
    pick, band = [], None
    if temp_now is not None:
        for b in TEMP_BAND_C:
            pick = [t for t in ok if t.get("temp") is not None and abs(t["temp"] - temp_now) <= b]
            if sum(t["km"] for t in pick) >= MIN_BAND_KM:
                band = b
                break
    if band is None:
        pick = ok
    if sum(t["km"] for t in pick) < MIN_BAND_KM:
        return None
    pairs = [(t["cons"], t["km"]) for t in pick]
    hi_c, mid_c, lo_c = (_wpct(pairs, q) for q in (0.9, 0.5, 0.1))
    return {"km_low": round(remaining_kwh / hi_c * 100), "km_mid": round(remaining_kwh / mid_c * 100),
            "km_high": round(remaining_kwh / lo_c * 100), "cons_mid": round(mid_c, 1),
            "trips": len(pick), "km": round(sum(t["km"] for t in pick)), "temp_band": band}


# ── range at motorway speed (one number instead of a span) ──────────────────
# Consumption of the own trips as a function of trip speed and cold:
#   cons = a + b·(v/100)² + c·max(0, COLD_C − T)
# (rolling + drive train, air drag, heating). Fitted km-weighted on trips
# with 25–125 km/h average speed, evaluated at CRUISE_KMH and today's
# temperature. Wind and topography come in later steps.

CRUISE_KMH = 130.0
COLD_C = 15.0
FIT_MIN_TRIPS = 20
FIT_MIN_KM = 300.0
FIT_MIN_FAST_KM = 100.0        # trips ≥ 80 km/h: anchor for the drag term


def _fit_speed(rows, ys, ws):
    x = fm._solve(rows, ys, ws)
    if not x:
        return None
    # one pass against outliers (GPS jumps, bad energy): drop > 3·MAD, refit
    res = [y - sum(a * b for a, b in zip(r, x)) for r, y in zip(rows, ys)]
    lim = 3 * (fm.mad(res) or 0) or None
    if lim:
        keep = [i for i, e in enumerate(res) if abs(e) <= lim]
        if len(keep) >= FIT_MIN_TRIPS and len(keep) < len(rows):
            x = fm._solve([rows[i] for i in keep], [ys[i] for i in keep], [ws[i] for i in keep]) or x
    return x


def speed_model(trips):
    """trips: [{km, cons, v, temp}] → {"a", "b", "c", "n", "km"} or None."""
    ok = [t for t in trips
          if (t.get("km") or 0) >= fm.MIN_CONS_KM and t.get("v") and 25 <= t["v"] <= 125
          and t.get("cons") and 5 <= t["cons"] <= 60]
    if len(ok) < FIT_MIN_TRIPS or sum(t["km"] for t in ok) < FIT_MIN_KM \
            or sum(t["km"] for t in ok if t["v"] >= 80) < FIT_MIN_FAST_KM:
        return None
    warm = [t for t in ok if t.get("temp") is not None]
    x = None
    if len(warm) >= FIT_MIN_TRIPS and sum(t["km"] for t in warm) >= FIT_MIN_KM:
        x = _fit_speed([[1.0, (t["v"] / 100) ** 2, max(0.0, COLD_C - t["temp"])] for t in warm],
                 [t["cons"] for t in warm], [t["km"] for t in warm])
        used = warm
    if not x or x[2] < 0:                          # no usable temperature effect
        x2 = _fit_speed([[1.0, (t["v"] / 100) ** 2] for t in ok], [t["cons"] for t in ok], [t["km"] for t in ok])
        x, used = (x2 + [0.0], ok) if x2 else (None, ok)
    if not x or x[1] <= 0:
        return None
    return {"a": round(x[0], 3), "b": round(x[1], 3), "c": round(x[2], 3),
            "n": len(used), "km": round(sum(t["km"] for t in used))}


def cruise_range(remaining_kwh, model, temp_now, v=CRUISE_KMH):
    """{"km", "cons", "v"} at speed v and today's temperature, or None."""
    if not model or not remaining_kwh or remaining_kwh <= 0:
        return None
    cold = max(0.0, COLD_C - temp_now) if temp_now is not None else 0.0
    cons = model["a"] + model["b"] * (v / 100) ** 2 + model["c"] * cold
    if cons <= 5:
        return None
    return {"km": round(remaining_kwh / cons * 100), "cons": round(cons, 1), "v": round(v)}
