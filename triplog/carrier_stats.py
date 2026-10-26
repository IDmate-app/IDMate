"""Long-term mobile carrier statistics for the telemetry tab.

The stick reports per telegram the PLMN it is booked into (`lp`), the signal
quality as CSQ (`ls`, 0-31, 99 = unknown), position and speed. The Influx
query (admin.py) samples one row per time window; every sample stands for
one window of connected time. Pure logic here, so it is testable without
Influx.
"""

import json
from datetime import date, datetime, timedelta, timezone

from plmn import plmn_info

# CSQ bands: below WEAK the modem barely holds the link, from STRONG on the
# signal is good (CSQ 20 ≈ -73 dBm).
CSQ_WEAK = 10
CSQ_STRONG = 20
# Samples further apart than this are not counted as a carrier change (the
# stick slept or had no data in between).
SWITCH_MAX_GAP_S = 600
DRIVE_KMH = 5
SETTING_COLORS = "carrier_colors"

# MCC → ISO country code (Europe, as far as plmn.py knows operators).
MCC_COUNTRY = {
    202: "GR", 204: "NL", 206: "BE", 208: "FR", 212: "MC", 213: "AD", 214: "ES",
    216: "HU", 218: "BA", 219: "HR", 220: "RS", 222: "IT", 226: "RO", 228: "CH",
    230: "CZ", 231: "SK", 232: "AT", 234: "GB", 235: "GB", 238: "DK", 240: "SE",
    242: "NO", 244: "FI", 246: "LT", 247: "LV", 248: "EE", 250: "RU", 255: "UA",
    257: "BY", 259: "MD", 260: "PL", 262: "DE", 266: "GI", 268: "PT", 270: "LU",
    272: "IE", 274: "IS", 276: "AL", 278: "MT", 280: "CY", 282: "GE", 283: "AM",
    284: "BG", 286: "TR", 288: "FO", 290: "GL", 292: "SM", 293: "SI", 294: "MK",
    295: "LI", 297: "ME",
}


def country_of(plmn):
    try:
        return MCC_COUNTRY.get(int(plmn) // 100, "")
    except (TypeError, ValueError):
        return ""


def load_colors(db):
    """User colour choices per carrier name (settings, JSON)."""
    row = db.execute("SELECT value FROM settings WHERE key = ?", (SETTING_COLORS,)).fetchone()
    try:
        data = json.loads(row["value"]) if row and row["value"] else {}
    except (TypeError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str) and _is_hex(v)}


def _is_hex(v):
    return len(v) == 7 and v[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in v[1:])


def save_color(db, name, color):
    """Set (or with an empty colour: reset) the colour of one carrier."""
    colors = load_colors(db)
    if color:
        if not _is_hex(color):
            raise ValueError("colour must be #rrggbb")
        colors[name] = color.lower()
    else:
        colors.pop(name, None)
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               (SETTING_COLORS, json.dumps(colors, ensure_ascii=False)))
    db.commit()


def carrier(plmn, colors):
    """(name, colour, brand colour) for a PLMN, the user's colour first."""
    name, brand = plmn_info(plmn)
    name = name or f"PLMN {plmn}"
    return name, colors.get(name, brand), brand


def parse_range(date_from, date_to, tz, today=None):
    """Local dates (inclusive) → (start UTC, stop UTC, days). Default: the
    last 30 days; an empty start means everything since 2020."""
    today = today or date.today()
    try:
        d1 = date.fromisoformat(date_to) if date_to else today
    except ValueError:
        d1 = today
    if date_from is None:
        d0 = d1 - timedelta(days=29)
    else:
        try:
            d0 = date.fromisoformat(date_from) if date_from else date(2020, 1, 1)
        except ValueError:
            d0 = d1 - timedelta(days=29)
    if d0 > d1:
        d0, d1 = d1, d0
    start = datetime(d0.year, d0.month, d0.day, tzinfo=tz).astimezone(timezone.utc)
    stop = datetime(d1.year, d1.month, d1.day, tzinfo=tz) + timedelta(days=1)
    stop = min(stop.astimezone(timezone.utc), datetime.now(timezone.utc))
    return start, stop, (d1 - d0).days + 1


def window_s(days, fine=False):
    """Sampling window for a range: fine for the map (road-following), coarser
    for long ranges so a year does not load millions of rows."""
    if fine:
        return 30 if days <= 31 else 60 if days <= 120 else 120
    return 60 if days <= 31 else 300 if days <= 400 else 900


def rfc(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def summarize(samples, window, days, tz, colors=None):
    """samples: (time UTC, plmn, csq, speed or None), any order.
    Returns carriers (sorted by time), buckets (per day, or per month for
    ranges over 62 days), countries and carrier changes."""
    colors = colors or {}
    per = {}
    buckets = {}
    countries = {}
    monthly = days > 62
    no_signal = 0
    switches = {}
    prev = None
    for t, plmn, csq, speed in sorted((s for s in samples if s[0] is not None), key=lambda s: s[0]):
        try:
            plmn = int(plmn)
        except (TypeError, ValueError):
            continue
        name, color, brand = carrier(plmn, colors)
        c = per.setdefault(name, {"name": name, "color": color, "brand_color": brand,
                                  "plmns": set(), "countries": set(), "n": 0,
                                  "drive": 0, "csq_sum": 0.0, "csq_n": 0, "weak": 0,
                                  "strong": 0, "first": t, "last": t})
        cc = country_of(plmn)
        c["plmns"].add(plmn)
        if cc:
            c["countries"].add(cc)
        c["n"] += 1
        c["last"] = t
        if speed is not None and speed >= DRIVE_KMH:
            c["drive"] += 1
        if csq is None or csq <= 0 or csq > 31:
            no_signal += 1
        else:
            c["csq_sum"] += csq
            c["csq_n"] += 1
            c["weak"] += csq < CSQ_WEAK
            c["strong"] += csq >= CSQ_STRONG
        loc = t.astimezone(tz)
        key = loc.strftime("%Y-%m") if monthly else loc.strftime("%Y-%m-%d")
        b = buckets.setdefault(key, {})
        b[name] = b.get(name, 0) + 1
        if cc:
            k = countries.setdefault(cc, {})
            k[name] = k.get(name, 0) + 1
        if prev and prev[1] != name and (t - prev[0]).total_seconds() <= SWITCH_MAX_GAP_S:
            pair = (prev[1], name)
            switches[pair] = switches.get(pair, 0) + 1
        prev = (t, name)

    total = sum(c["n"] for c in per.values()) or 1
    mins = window / 60
    carriers = []
    for c in sorted(per.values(), key=lambda c: -c["n"]):
        q = c["csq_n"]
        carriers.append({
            "name": c["name"], "color": c["color"], "brand_color": c["brand_color"],
            "custom_color": c["name"] in colors,
            "plmns": sorted(c["plmns"]), "countries": sorted(c["countries"]),
            "minutes": round(c["n"] * mins), "share": round(100 * c["n"] / total, 1),
            "drive_minutes": round(c["drive"] * mins),
            "avg_csq": round(c["csq_sum"] / q, 1) if q else None,
            "weak_pct": round(100 * c["weak"] / q) if q else None,
            "strong_pct": round(100 * c["strong"] / q) if q else None,
            "first": c["first"].astimezone(tz).strftime("%Y-%m-%d %H:%M"),
            "last": c["last"].astimezone(tz).strftime("%Y-%m-%d %H:%M"),
        })
    return {
        "window_s": window,
        "bucket": "month" if monthly else "day",
        "carriers": carriers,
        "buckets": [{"key": k, "minutes": {n: round(v * mins) for n, v in b.items()}}
                    for k, b in sorted(buckets.items())],
        "countries": [{"cc": cc, "minutes": round(sum(k.values()) * mins),
                       "carriers": [{"name": n, "minutes": round(v * mins)}
                                    for n, v in sorted(k.items(), key=lambda x: -x[1])]}
                      for cc, k in sorted(countries.items(), key=lambda x: -sum(x[1].values()))],
        "switches": sum(switches.values()),
        "top_switches": [{"from": a, "to": b, "n": n}
                         for (a, b), n in sorted(switches.items(), key=lambda x: -x[1])[:5]],
        "no_signal_minutes": round(no_signal * mins),
        "total_minutes": round(sum(c["n"] for c in per.values()) * mins),
    }
