"""BMS learning: what the car's display makes of the BMS.

The car shows a SoC, a range and an average consumption that differ from the
raw BMS SoC on the CAN bus (buffer at top and bottom, maybe temperature).
The user enters what the display shows; each entry is stored next to the CAN
values of that moment (bms_points). From those points:

  fit_display   display SoC = a + b·CAN SoC (+ c·battery °C + d·outside °C
                once there are enough points with enough temperature spread),
                robust (Huber), with leave-one-out error as the honest accuracy.
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

def fit_range(points):
    """E (kWh per 100 % display) from range × consumption ÷ display SoC, the
    measured window the displayed average follows best, and the error of the
    rebuilt range. None without points carrying range and consumption."""
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
    # The range the stick reads from the car: the displayed number or not?
    diffs = [_f(p["disp_range"]) - _f(p.get("can_range")) for p in rows if _f(p.get("can_range")) is not None]
    out["can_range_n"] = len(diffs)
    out["can_range_diff"] = round(fm.median(diffs), 1) if diffs else None
    out["can_range_mae"] = round(fm.median([abs(d) for d in diffs]), 1) if diffs else None
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
