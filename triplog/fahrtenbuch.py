"""Monthly logbook ("Fahrtenbuch") of one vehicle: the data and the PDF.

The PDF is rendered on the server (fpdf2), not through the browser's print
dialog: a real text PDF, A4 landscape, page header and page numbers on every
sheet, the column heads repeated after each page break.

Rows, as a logbook has them:
  * a business trip: date, time, odometer at start and end, km, from, to,
    purpose, partner/destination and occasion;
  * private trips in a row are one line: dates from-to, odometer at start of
    the first and end of the last, km — no places (only the distance is
    needed for private use);
  * a trip without a purpose is listed like a business trip and marked;
  * a jump in the odometer between two rows becomes its own line, so a gap
    in the record is visible instead of hidden.
"""

import calendar
import logging
import os
from datetime import datetime

# fpdf2 subsets the font with fontTools, which logs every glyph table at INFO
# — dozens of lines per PDF in the app log.
logging.getLogger("fontTools").setLevel(logging.WARNING)

FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu")
LUECKE_KM = 0.5          # odometer jumps up to this are rounding, not a gap


def _privat_namen(db):
    return {r[0] for r in db.execute("SELECT name FROM purpose_meta WHERE is_private = 1")}


def _zeit(ts):
    try:
        return datetime.fromisoformat(str(ts).replace(" ", "T")[:19])
    except ValueError:
        return None


def monat_daten(db, device, jahr, monat):
    """Rows and totals of one month. ``rows`` in the order of the logbook,
    each a dict with ``art`` in fahrt/offen/privat/luecke."""
    letzter = calendar.monthrange(jahr, monat)[1]
    von, bis = f"{jahr:04d}-{monat:02d}-01T00:00:00", f"{jahr:04d}-{monat:02d}-{letzter:02d}T23:59:59"
    trips = [dict(r) for r in db.execute(
        "SELECT * FROM trips WHERE device = ? AND start_time >= ? AND start_time <= ? ORDER BY start_time",
        (device, von, bis))]
    privat = _privat_namen(db)

    rows = []
    for t in trips:
        km = t.get("distance_km") or 0.0
        if (t.get("purpose") or "") in privat and t.get("purpose"):
            letzte = rows[-1] if rows else None
            if letzte and letzte["art"] == "privat":
                letzte["bis"] = t["end_time"]
                letzte["odo_end"] = t.get("odo_end") if t.get("odo_end") is not None else letzte["odo_end"]
                letzte["km"] += km
                letzte["anzahl"] += 1
                letzte["ids"].append(t["id"])
                continue
            rows.append({"art": "privat", "von": t["start_time"], "bis": t["end_time"],
                         "odo_start": t.get("odo_start"), "odo_end": t.get("odo_end"),
                         "km": km, "anzahl": 1, "ids": [t["id"]]})
            continue
        rows.append({"art": "fahrt" if t.get("purpose") else "offen",
                     "von": t["start_time"], "bis": t["end_time"],
                     "odo_start": t.get("odo_start"), "odo_end": t.get("odo_end"), "km": km,
                     "start": t.get("start_address") or "", "ziel": t.get("end_address") or "",
                     "zweck": t.get("purpose") or "", "partner": t.get("destination") or "",
                     "anlass": t.get("visit_reason") or "", "notiz": t.get("note") or "",
                     "ids": [t["id"]]})

    # odometer gaps between consecutive rows
    mit_luecken = []
    for r in rows:
        vorher = mit_luecken[-1] if mit_luecken else None
        if (vorher and vorher.get("odo_end") is not None and r.get("odo_start") is not None
                and r["odo_start"] - vorher["odo_end"] > LUECKE_KM):
            mit_luecken.append({"art": "luecke", "km": r["odo_start"] - vorher["odo_end"],
                                "odo_start": vorher["odo_end"], "odo_end": r["odo_start"]})
        mit_luecken.append(r)

    summen = {"gesamt": 0.0, "privat": 0.0, "offen": 0.0, "luecken": 0.0, "zwecke": {}}
    for r in mit_luecken:
        if r["art"] == "luecke":
            summen["luecken"] += r["km"]
            continue
        summen["gesamt"] += r["km"]
        if r["art"] == "privat":
            summen["privat"] += r["km"]
        elif r["art"] == "offen":
            summen["offen"] += r["km"]
        else:
            summen["zwecke"][r["zweck"]] = summen["zwecke"].get(r["zweck"], 0.0) + r["km"]
    summen["dienstlich"] = sum(summen["zwecke"].values())
    odos = [t[k] for t in trips for k in ("odo_start", "odo_end") if t.get(k) is not None]
    summen["odo_anfang"] = min(odos) if odos else None
    summen["odo_ende"] = max(odos) if odos else None
    summen["fahrten"] = len(trips)
    return {"rows": mit_luecken, "summen": summen}


def datum_text(ts):
    z = _zeit(ts)
    return z.strftime("%d.%m.%Y") if z else ""


def zeit_text(von, bis):
    a, b = _zeit(von), _zeit(bis)
    if not a:
        return ""
    return a.strftime("%H:%M") + ("–" + b.strftime("%H:%M") if b else "")


def tage_text(von, bis):
    """'03.09.' or '03.–07.09.' for a private block."""
    a, b = _zeit(von), _zeit(bis)
    if not a:
        return ""
    if not b or a.date() == b.date():
        return a.strftime("%d.%m.%Y")
    if a.month == b.month:
        return f"{a.strftime('%d.')}–{b.strftime('%d.%m.%Y')}"
    return f"{a.strftime('%d.%m.')}–{b.strftime('%d.%m.%Y')}"


def km_text(v, stellen=0):
    if v is None:
        return "--"
    return f"{v:,.{stellen}f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ── PDF ──────────────────────────────────────────────────────────────────────

def _schrift(pdf):
    """DejaVu (all of Unicode: Polish, Czech, Nordic place names) when the
    font is installed, else the PDF core font with Latin-1 only."""
    for d in FONT_DIRS:
        normal, fett = os.path.join(d, "DejaVuSans.ttf"), os.path.join(d, "DejaVuSans-Bold.ttf")
        if os.path.exists(normal) and os.path.exists(fett):
            pdf.add_font("DejaVu", "", normal)
            pdf.add_font("DejaVu", "B", fett)
            pdf.add_font("DejaVu", "I", normal)
            return "DejaVu", False
    return "Helvetica", True


def pdf_bytes(daten, kopf, t):
    """``kopf``: {"titel", "monat", "fahrzeug", "kennzeichen", "erstellt", "version"};
    ``t``: the translations. Returns the PDF as bytes."""
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    class Blatt(FPDF):
        def header(self):
            self.set_font(familie, "B", 13)
            self.cell(0, 7, kopf["titel"], new_x="LMARGIN", new_y="TOP")
            self.set_font(familie, "", 9)
            self.cell(0, 7, rein(f'{kopf["monat"]} · {kopf["fahrzeug"]}'), align="R", new_x="LMARGIN", new_y="NEXT")
            self.set_draw_color(30, 30, 30)
            self.set_line_width(0.5)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.ln(3)

        def footer(self):
            self.set_y(-10)
            self.set_font(familie, "", 7)
            self.set_text_color(120, 120, 120)
            self.cell(0, 5, rein(f'IDmate {kopf["version"]} · {t["logbook_generated"]} {kopf["erstellt"]}'), new_x="LMARGIN", new_y="TOP")
            self.cell(0, 5, f"{t['logbook_page']} {self.page_no()}/{{nb}}", align="R")
            self.set_text_color(0, 0, 0)

    pdf = Blatt(orientation="L", unit="mm", format="A4")
    pdf.set_margins(10, 10, 10)
    pdf.set_auto_page_break(True, margin=14)
    familie, nur_latin1 = _schrift(pdf)

    def rein(s):
        s = str(s or "")
        if nur_latin1:
            s = s.replace("–", "-").replace("·", "-").replace("→", "->")
            s = s.encode("latin-1", "replace").decode("latin-1")
        return s

    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_font(familie, "", 7.5)

    breiten = (19, 19, 18, 18, 13, 52, 52, 26, 60)
    kopfzeile = (t["logbook_col_date"], t["logbook_col_time"], t["logbook_col_odo_start"],
                 t["logbook_col_odo_end"], t["logbook_col_km"], t["logbook_col_from"],
                 t["logbook_col_to"], t["logbook_col_purpose"], t["logbook_col_partner"])
    kopf_stil = FontFace(emphasis="BOLD", fill_color=(235, 235, 235))
    grau = (245, 245, 245)
    with pdf.table(col_widths=breiten, headings_style=kopf_stil, line_height=4.2, padding=1.2,
                   text_align=("LEFT", "LEFT", "RIGHT", "RIGHT", "RIGHT", "LEFT", "LEFT", "LEFT", "LEFT"),
                   repeat_headings=1, borders_layout="HORIZONTAL_LINES", first_row_as_headings=True) as tab:
        z = tab.row()
        for k in kopfzeile:
            z.cell(rein(k))
        for r in daten["rows"]:
            z = tab.row()
            if r["art"] == "luecke":
                z.cell(rein(t["logbook_gap"].format(km_text(r["km"], 1))), colspan=2,
                       style=FontFace(emphasis="ITALICS", color=(170, 90, 0)))
                z.cell(km_text(r["odo_start"]))
                z.cell(km_text(r["odo_end"]))
                z.cell(km_text(r["km"], 1))
                z.cell("", colspan=4)
                continue
            if r["art"] == "privat":
                stil = FontFace(fill_color=grau)
                z.cell(rein(tage_text(r["von"], r["bis"])), colspan=2, style=stil)
                z.cell(km_text(r["odo_start"]), style=stil)
                z.cell(km_text(r["odo_end"]), style=stil)
                z.cell(km_text(r["km"], 1), style=stil)
                z.cell(rein(t["logbook_private_rows"].format(r["anzahl"])), colspan=4,
                       style=FontFace(emphasis="ITALICS", fill_color=grau))
                continue
            partner = " · ".join(x for x in (r["partner"], r["anlass"]) if x)
            z.cell(rein(datum_text(r["von"])))
            z.cell(rein(zeit_text(r["von"], r["bis"])))
            z.cell(km_text(r["odo_start"]))
            z.cell(km_text(r["odo_end"]))
            z.cell(km_text(r["km"], 1))
            z.cell(rein(r["start"]))
            z.cell(rein(r["ziel"]))
            if r["art"] == "offen":
                z.cell(rein(t["logbook_no_purpose"]), style=FontFace(emphasis="BOLD", color=(190, 30, 30)))
            else:
                z.cell(rein(r["zweck"]))
            z.cell(rein(partner))

    s = daten["summen"]
    pdf.ln(4)
    if pdf.get_y() > pdf.h - 60:
        pdf.add_page()
    pdf.set_font(familie, "B", 9)
    pdf.cell(0, 6, rein(t["logbook_totals"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(familie, "", 8.5)

    def zeile(links, rechts, fett=False):
        pdf.set_font(familie, "B" if fett else "", 8.5)
        pdf.cell(70, 5, rein(links), new_x="RIGHT", new_y="TOP")
        pdf.cell(30, 5, rein(rechts), align="R", new_x="LMARGIN", new_y="NEXT")

    zeile(t["logbook_odo_month_start"], km_text(s["odo_anfang"]))
    zeile(t["logbook_odo_month_end"], km_text(s["odo_ende"]))
    zeile(t["logbook_trips"], str(s["fahrten"]))
    for zweck, km in sorted(s["zwecke"].items()):
        zeile(zweck, km_text(km, 1) + " km")
    if s["offen"]:
        zeile(t["logbook_no_purpose"], km_text(s["offen"], 1) + " km")
    zeile(t["logbook_private"], km_text(s["privat"], 1) + " km")
    if s["luecken"]:
        zeile(t["logbook_gaps"], km_text(s["luecken"], 1) + " km")
    zeile(t["logbook_total"], km_text(s["gesamt"], 1) + " km", fett=True)

    pdf.ln(14)
    y = pdf.get_y()
    pdf.set_line_width(0.3)
    pdf.line(10, y, 90, y)
    pdf.line(110, y, 200, y)
    pdf.set_font(familie, "", 7.5)
    pdf.cell(100, 4, rein(t["logbook_sign_date"]), new_x="RIGHT", new_y="TOP")
    pdf.cell(90, 4, rein(t["logbook_sign_driver"]))
    return bytes(pdf.output())
