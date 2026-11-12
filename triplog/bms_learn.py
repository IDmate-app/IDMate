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
from datetime import timedelta

import fahrzeugmodell as fm

_HOUR = timedelta(hours=1)

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


# ── where in a trip the car is ────────────────────────────────────────────

PARK_S = 15 * 60               # no km for this long: the trip is over
FRESH_TRIP_KM = 10.0           # below: the displayed average is the cold start


def trip_context(series, now):
    """series: [(time s, odometer, SoC)] oldest first, now: epoch s.
    → {"trip_km", "trip_min": km and minutes since the current (or last)
    trip began, "stood_h": how long the car stood before it, "parked_h": how
    long it stands now (0 while driving)}. None where the series does not
    tell: no km in it at all, or the stop before the trip began earlier."""
    pts = [(t, o) for t, o, _s in series if t is not None and o is not None]
    moves = [(pts[i][0], pts[i - 1][1], pts[i][1]) for i in range(1, len(pts)) if pts[i][1] > pts[i - 1][1]]
    if not moves:
        return {"trip_km": None, "trip_min": None, "stood_h": None, "parked_h": None}
    j = len(moves) - 1
    while j > 0 and moves[j][0] - moves[j - 1][0] < PARK_S:
        j -= 1
    last = moves[-1][0]
    parked = max(0.0, now - last)
    driving = parked < PARK_S
    return {"trip_km": round(moves[-1][2] - moves[j][1], 1),
            "trip_min": round(((now if driving else last) - moves[j][0]) / 60),
            "stood_h": round((moves[j][0] - moves[j - 1][0]) / 3600, 2) if j > 0 else None,
            "parked_h": 0.0 if driving else round(parked / 3600, 2)}


def fresh_trip(point):
    """A learning point taken within the first km of a trip."""
    km = _f(point.get("trip_km"))
    return km is not None and km < FRESH_TRIP_KM


def trip_temp(hourly, start, end):
    """Outside temperature of a trip: the mean of the hours it touches
    (hourly: {"YYYY-MM-DDTHH" UTC: °C}, start/end: aware datetimes), else
    the mean of that UTC day, else None."""
    if not hourly or start is None:
        return None
    end = end if end and end >= start else start
    vals, t = [], start.replace(minute=0, second=0, microsecond=0)
    while t <= end and len(vals) < 48:
        v = hourly.get(t.strftime("%Y-%m-%dT%H"))
        if v is not None:
            vals.append(v)
        t += _HOUR
    if not vals:
        day = start.strftime("%Y-%m-%d")
        vals = [v for k, v in hourly.items() if k.startswith(day)]
    return round(sum(vals) / len(vals), 1) if vals else None


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
    # Right after setting off the displayed average is the cold start alone
    # (40 kWh/100 km on the first km), the displayed range is not: such a
    # point says nothing about E. Left out as soon as others exist.
    settled = [p for p in rows if not fresh_trip(p)]
    fresh_n = len(rows) - len(settled)
    rows = settled or rows
    if not rows:
        return None
    es = [_f(p["disp_range"]) * _f(p["disp_cons"]) / _f(p["disp_soc"]) for p in rows]
    e = fm.median(es)
    out = {"n": len(rows), "e_kwh": round(e, 2),
           "e_spread": round(fm.mad(es), 2) if len(es) > 1 else None,
           "range_mae": round(fm.median([abs(_f(p["disp_soc"]) * e / _f(p["disp_cons"]) - _f(p["disp_range"]))
                                         for p in rows]), 1),
           "window_km": None, "cons_factor": None, "cons_mae": None,
           "fresh_left_out": fresh_n if settled else 0}
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
# Consumption of the own trips as a function of trip speed and cold. One
# trip is one point (average speed, kWh/100 km, outside temperature),
# weighted by its km:
#
#   cons = a + b·(v/100)²                    rolling + drive train, air drag
#        + c·cold                            cold per km           ("km")
#     or + p·cold·100/v                      heating as power: p kW per K,
#                                            per km more the slower ("hour")
#
# cold = max(0, COLD_C − T). Both forms are checked on trips left out of
# the fit; "hour" is only taken when it predicts them clearly better
# (CV_GAIN). On 377 real trips of one car (Nov 2026) the two could not be
# told apart, and "hour" read the few cold slow trips 2 kWh/100 km too high.
#
# What a trip costs once after the car stood (e_start, kWh) is not fitted
# with the rest but measured: short trips after a long stop against the
# model above (_start_energy). On those real trips it was 0.2 kWh and did
# not grow with the cold.
#
# Evaluated at CRUISE_KMH and today's temperature; wind and topography come
# on top along the route.

CRUISE_KMH = 130.0
# The model learns from trip averages; cruise control 130 averages about
# 110 km/h over a motorway trip (ramps, traffic, junctions). Checked on the
# own data: fast trips in the cold months matched the model at ~110, not 130.
CRUISE_AVG_KMH = 110.0
COLD_C = 15.0
FIT_MIN_TRIPS = 20
FIT_MIN_KM = 300.0
FIT_MIN_FAST_KM = 100.0        # trips ≥ 80 km/h: anchor for the drag term
COOL_H = 2.0                   # standing: after about this long a start costs the full e_start
HEAT_V_MIN = 15.0              # km/h: below, heating per km stops growing (stop and go)
FORMS = ("km", "hour")         # the first is the simple reading, the second has to earn its place
CV_FOLDS = 5
CV_GAIN = 0.03                 # … by predicting trips left out of the fit this much better
START_KM = (3.0, 25.0)         # short trips show what a start costs
START_STOOD_H = 2.0            # after standing at least this long
START_V_MIN = 15.0             # not the crawl out of a car park
START_MIN_TRIPS = 15


def cold_k(temp):
    return max(0.0, COLD_C - temp) if temp is not None else 0.0


def start_weight(stood_h):
    """0 (just driven, still warm) … 1 (stood long, as cold as outside)."""
    return 1.0 if stood_h is None else 1.0 - math.exp(-max(0.0, stood_h) / COOL_H)


HUBER_K = 1.5                  # residuals beyond K·σ count less, the further the less
HUBER_ROUNDS = 8


def _fit_speed(rows, ys, ws):
    """km-weighted least squares, robust: a trip far off the others (GPS
    jump, bad energy, a cold start read as a whole trip) is weighted down by
    how far off it is (Huber), instead of being trusted fully or dropped at
    a hard limit. With 20 to 30 trips a few such points no longer bend the
    model."""
    x = fm._solve(rows, ys, ws)
    if not x:
        return None
    for _ in range(HUBER_ROUNDS):
        res = [y - sum(a * b for a, b in zip(r, x)) for r, y in zip(rows, ys)]
        sigma = 1.4826 * (fm.mad(res) or 0)
        if sigma <= 0:
            break
        x2 = fm._solve(rows, ys, [w * min(1.0, HUBER_K * sigma / abs(e)) if e else w for w, e in zip(ws, res)])
        if not x2:
            break
        x = x2
    return x


def _features(t, form):
    cold = cold_k(t["temp"])
    return [1.0, (t["v"] / 100) ** 2, cold if form == "km" else cold * 100.0 / max(t["v"], HEAT_V_MIN)]


def _fit_form(trips, form, e_start=0.0):
    """→ {"a", "b", "c", "p"} or None when the trips do not carry the form
    (no solution, or a term with the wrong sign). e_start: the energy of a
    start, taken off each trip first (it is not part of driving steadily)."""
    ys = [t["cons"] - e_start * start_weight(t.get("stood_h")) * 100.0 / t["km"] for t in trips]
    x = _fit_speed([_features(t, form) for t in trips], ys, [t["km"] for t in trips])
    if not x or x[1] <= 0 or x[2] < 0:
        return None
    return {"a": x[0], "b": x[1], "c": x[2] if form == "km" else 0.0, "p": x[2] if form == "hour" else 0.0}


def trip_prediction(model, t):
    """kWh/100 km the model expects for one trip {v, temp, km, stood_h}."""
    return consumption(model, t["v"], t.get("temp")) + \
        warmup_kwh(model, t.get("temp"), t.get("stood_h")) * 100.0 / t["km"]


def _cv_error(trips, form):
    """Energy error on trips left out of the fit: Σ|kWh off| ÷ Σ km × 100,
    i.e. kWh/100 km, every fifth trip held out in turn. None when a fold
    does not carry the form."""
    err = km = 0.0
    for f in range(CV_FOLDS):
        m = _fit_form([t for i, t in enumerate(trips) if i % CV_FOLDS != f], form)
        if not m:
            return None
        for t in (t for i, t in enumerate(trips) if i % CV_FOLDS == f):
            err += abs(trip_prediction(m, t) - t["cons"]) * t["km"]
            km += t["km"]
    return err / km if km else None


def _start_energy(trips, model, with_temp):
    """(kWh, n): what short trips after a long stop used beyond the model —
    the median over the car's own trips. 0 with too few of them."""
    extra = []
    for t in trips:
        km, v = t.get("km") or 0, t.get("v")
        if not (START_KM[0] <= km < START_KM[1] and v and START_V_MIN <= v <= 125):
            continue
        if t.get("stood_h") is None or t["stood_h"] < START_STOOD_H or (with_temp and t.get("temp") is None):
            continue
        kwh = t.get("kwh") or (t["cons"] * km / 100.0 if t.get("cons") else None)
        if kwh and kwh > 0:
            extra.append(kwh - consumption(model, v, t.get("temp")) * km / 100.0)
    if len(extra) < START_MIN_TRIPS:
        return 0.0, len(extra)
    return max(0.0, fm.median(extra)), len(extra)


def speed_model(trips):
    """trips: [{km, cons, v, temp, stood_h, kwh}] → {"a", "b", "c", "p",
    "e_start", "form", "n", "km", "temp", "cv", "start_n"} or None. form:
    which of FORMS (or "flat" without temperatures); cv: the error of each
    form on trips left out, kWh/100 km; e_start: kWh per start after
    standing, from start_n short trips."""
    ok = [t for t in trips
          if (t.get("km") or 0) >= fm.MIN_CONS_KM and t.get("v") and 25 <= t["v"] <= 125
          and t.get("cons") and 5 <= t["cons"] <= 60]
    if len(ok) < FIT_MIN_TRIPS or sum(t["km"] for t in ok) < FIT_MIN_KM \
            or sum(t["km"] for t in ok if t["v"] >= 80) < FIT_MIN_FAST_KM:
        return None
    warm = [t for t in ok if t.get("temp") is not None]
    form, used, cv = "flat", ok, {}
    if len(warm) >= FIT_MIN_TRIPS and sum(t["km"] for t in warm) >= FIT_MIN_KM \
            and sum(t["km"] for t in warm if t["v"] >= 80) >= FIT_MIN_FAST_KM:
        cv = {f: _cv_error(warm, f) for f in FORMS if _fit_form(warm, f)}
        cv = {f: e for f, e in cv.items() if e is not None}
        if cv:
            form = next(f for f in FORMS if f in cv)             # the simple one, if the trips carry it
            better = [f for f in cv if cv[f] <= cv[form] * (1 - CV_GAIN)]
            if better:
                form = min(better, key=cv.get)
            used = warm

    def fit(e_start):
        if form != "flat":
            return _fit_form(used, form, e_start)
        ys = [t["cons"] - e_start * start_weight(t.get("stood_h")) * 100.0 / t["km"] for t in used]
        x = _fit_speed([[1.0, (t["v"] / 100) ** 2] for t in used], ys, [t["km"] for t in used])
        return {"a": x[0], "b": x[1], "c": 0.0, "p": 0.0} if x and x[1] > 0 else None
    m = fit(0.0)
    if not m:
        return None
    # The trips the model is fitted on carry their start too: measure it
    # against the model, take it off the trips, fit again, measure again.
    e, n_start = _start_energy(trips, m, used is warm)
    if e > 0:
        m2 = fit(e)
        if m2:
            m = m2
            e, n_start = _start_energy(trips, m, used is warm)
    return {"a": round(m["a"], 3), "b": round(m["b"], 3), "c": round(m["c"], 4), "p": round(m["p"], 4),
            "e_start": round(e, 2), "start_n": n_start, "form": form,
            "n": len(used), "km": round(sum(t["km"] for t in used)), "temp": used is warm,
            "cv": {f: round(v, 2) for f, v in cv.items()}}


def consumption(model, v, temp, v_air=None):
    """kWh/100 km at speed v and outside temperature, warmed up. v_air: the
    air speed for the drag (head wind), v stays the speed over ground."""
    cold = cold_k(temp)
    va = v if v_air is None else max(0.0, v_air)
    return model["a"] + model["b"] * (va / 100) ** 2 + model.get("c", 0.0) * cold \
        + model.get("p", 0.0) * cold * 100.0 / max(v, HEAT_V_MIN)


def warmup_kwh(model, temp, stood_h):
    """Energy the next trip spends once on getting going: the more the
    longer the car stood (stood_h None: long). temp is not used: on the
    trips this was measured on, the cold made no difference to it."""
    return model.get("e_start", 0.0) * start_weight(stood_h)


def cruise_range(remaining_kwh, model, temp_now, v=CRUISE_KMH):
    """{"km", "cons", "v"} at speed v and today's temperature, or None."""
    if not model or not remaining_kwh or remaining_kwh <= 0:
        return None
    cons = consumption(model, v, temp_now)
    if cons <= 5:
        return None
    return {"km": round(remaining_kwh / cons * 100), "cons": round(cons, 1), "v": round(v)}


# ── driving or driven in the last 2 h: the model at the current speed ─────

DRIVE_WINDOW_H = 2
DRIVE_MIN_KM = 10.0
RECENT_S = 30 * 60
V_MAX = 200


def recent_speed(series):
    """series: [(time s, odometer, SoC)] of the last DRIVE_WINDOW_H, oldest
    first. Average speed from the first to the last odometer change (like a
    trip average, which is what the model was fitted on) → {"v", "km"} or
    None when the car did not drive at least DRIVE_MIN_KM lately."""
    pts = [(t, o) for t, o, _s in series if t is not None and o is not None]
    moved = [pts[i] for i in range(1, len(pts)) if pts[i][1] > pts[i - 1][1]]
    if not moved:
        return None
    first = next(pts[i - 1] for i in range(1, len(pts)) if pts[i][1] > pts[i - 1][1])
    last = moved[-1]
    # The current pace: only the last RECENT_S of driving when that is
    # already DRIVE_MIN_KM, so a fast motorway stretch is not watered down
    # by the slow start of the trip.
    recent = [p for p in pts if first[0] <= p[0] <= last[0] - RECENT_S]
    if recent and last[1] - recent[-1][1] >= DRIVE_MIN_KM:
        first = recent[-1]
    km, h = last[1] - first[1], (last[0] - first[0]) / 3600
    if km < DRIVE_MIN_KM or h <= 0:
        return None
    return {"v": round(min(km / h, V_MAX)), "km": round(km)}
