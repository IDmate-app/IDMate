"""Beleg einer externen Ladung: Screenshot der Lade-App -> Felder.

Die Ladedetails der Anbieter-Apps (E.ON Drive, Ionity, EnBW, Tesla, ...)
zeigen dieselben vier Dinge in immer anderer Anordnung: geladene kWh,
Kosten, Start, Ende - dazu Station, Adresse, Betreiber und eine Referenz.
Statt je App eine Vorlage zu pflegen, liest `felder_aus_text` den Text
tolerant: Zahl neben "kWh" ist die Energie, Zahl neben "EUR"/"EUR-Zeichen"
der Preis, das erste Datum mit seiner Uhrzeit der Start, das zweite das
Ende. Den Text liefert `text_aus_bild` per Tesseract (deu+eng), oder er
kommt schon als Text (iOS "Live Text", Kopieren aus der App).

Alles Erkannte ist ein Vorschlag: Was fehlt, steht in `fehlt`; das Formular
speichert nur von selbst, wenn kWh, Start und Ende da sind.
"""
from __future__ import annotations

import io
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

MONATE = {
    "jan": 1, "januar": 1, "january": 1, "feb": 2, "februar": 2, "february": 2,
    "mär": 3, "mar": 3, "märz": 3, "maerz": 3, "march": 3, "apr": 4, "april": 4,
    "mai": 5, "may": 5, "jun": 6, "juni": 6, "june": 6, "jul": 7, "juli": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "okt": 10, "oct": 10, "oktober": 10, "october": 10, "nov": 11, "november": 11,
    "dez": 12, "dec": 12, "dezember": 12, "december": 12,
}
DATUM_RE = re.compile(
    r"(?<!\d)(?P<d1>\d{1,2})\.\s?(?P<m1>\d{1,2})\.\s?(?P<y1>\d{4}|\d{2})(?!\d)"          # 27.09.2026, 27.9.26
    r"|(?<!\d)(?P<y2>\d{4})-(?P<m2>\d{2})-(?P<d2>\d{2})(?!\d)"                          # 2026-09-27
    r"|(?<!\d)(?P<d3>\d{1,2})\.?\s+(?P<m3>[A-Za-zÄäÖöÜü]{3,9})\.?\s+(?P<y3>\d{4})(?!\d)"  # 27 Sept 2026, 27. September 2026
    r"|(?<![A-Za-z])(?P<m4>[A-Za-z]{3,9})\.?\s+(?P<d4>\d{1,2}),?\s+(?P<y4>\d{4})(?!\d)"  # Sep 27, 2026
)
ZEIT_RE = re.compile(r"(?<![\d:])(?P<h>\d{1,2}):(?P<mi>\d{2})(?![\d:])(?:\s*(?P<ampm>[AaPp]\.?[Mm]\.?))?")
ZAHL = r"\d{1,3}(?:\.\d{3})+,\d+|\d+,\d+|\d+\.\d+|\d+"
KWH_RE = re.compile(r"(?P<z>" + ZAHL + r")\s*kWh", re.I)
WAEHRUNGEN = {"€": "EUR", "eur": "EUR", "euro": "EUR", "sek": "SEK", "kr": "SEK", "nok": "NOK",
              "dkk": "DKK", "chf": "CHF", "pln": "PLN", "czk": "CZK", "gbp": "GBP", "£": "GBP"}
# Ein Betrag, kein Tarif: "0,44 €/kWh" (Tesla) ist der Preis je kWh, nicht die Kosten.
# (?<![^\s(:=]) : the amount starts a token. "12,/9€" or "12,?9€" (Tesseract
# tore the 7) must not yield 9 € from the fragment in front of the sign.
KOSTEN_RE = re.compile(
    r"(?<![^\s(:=])(?P<z>" + ZAHL + r")\s*(?P<w>€|eur(?:o)?|sek|kr|nok|dkk|chf|pln|czk|gbp|£)(?![a-z])(?!\s*/)"
    r"|(?P<w2>€|eur|chf|£)\s*(?P<z2>" + ZAHL + r")(?!\s*/)", re.I)
# Dauer statt Endzeit: "14 Min.", "1 Std. 5 Min.", "0h 30m", "Oh 30m" (Tesseract
# liest die Null als O), "1 h 5 min", "45 minutes".
DAUER_RE = re.compile(
    r"(?<![\d:])(?:(?P<h>\d{1,2}|[Oo])\s*(?:h|std\.?|stunden?|hours?|hrs?)\b\.?\s*)?"
    r"(?P<m>\d{1,3})\s*(?:min\.?|minuten|minutes?|mins?|m)(?![a-zA-Z])"
    r"|(?<![\d:])(?P<h2>\d{1,2})\s*(?:std\.?|stunden?|hours?|hrs?)(?![a-zA-Z])", re.I)
STRASSE_RE = re.compile(
    r"^\s*(?:\d{1,4}[a-z]?\s+\S.*?(?:straße|strasse|straBe|str\.|weg|platz|allee|gasse|ring|damm|ufer|chaussee|road|street|avenue|lane|gatan|vägen|vej|vei)\b.*"
    r"|\S.*?(?:straße|strasse|straBe|str\.|weg|platz|allee|gasse|ring|damm|ufer|chaussee|road|street|avenue|lane|gatan|vägen|vej|vei)\s+\d{1,4}[a-z]?\b.*)$",
    re.I | re.M)
EVSE_RE = re.compile(r"\b[A-Z]{2}\*[A-Z0-9]{3}\*[A-Z0-9*]{2,}")
REF_RE = re.compile(r"(?:referenz|reference|belegnummer|beleg-?nr|transaktion|transaction|session)[^\n\d]{0,40}\n?\s*(?P<r>[A-Z0-9-]{5,})", re.I)
ADRESSE_RE = re.compile(r"^(?=.*[A-Za-zÄÖÜäöüß])(?=.*\d).{4,}?,[ \t]*\d{4,5}[ \t]+\S.*$|^.*\b\d{5}[ \t]+[A-ZÄÖÜ][a-zäöüß].*$", re.M)
# Wörter, die in der Betreiberzeile nur Beiwerk sind ("IONITY Ladevorgang").
_BEIWERK = re.compile(r"\b(ladevorgang|ladedetails|ladung|beleg|quittung|rechnung|receipt|charging session|session|invoice)\b.*$", re.I)
# Betreiber, an denen man einen Beleg erkennt, wenn die Datenbank ihn nicht kennt.
BEKANNTE_BETREIBER = ("E.ON", "EON", "Ionity", "EnBW", "Tesla", "Allego", "Shell Recharge", "Aral pulse",
                      "Fastned", "Recharge", "Circle K", "Vattenfall", "Lidl", "Kaufland", "Plugsurfing",
                      "Elli", "Maingau", "Eways", "OKQ8", "Tanka", "Mer", "InCharge", "Ewiva", "Enel",
                      "Electra", "Ladenetz", "Pfalzwerke", "EWE Go", "Qwello", "TotalEnergies", "bp pulse",
                      "Virta", "Freshmile", "Greenway", "Comfortcharge", "Wirelane", "Chargepoint",
                      "Supercharg")
# Was der Beleg nennt, heißt in der Liste anders.
UMBENENNEN = {"Supercharg": "Tesla"}
# Ein Name je Anbieter, damit die Statistik nicht in Schreibweisen zerfällt.
MARKEN = ("E.ON", "IONITY", "EnBW", "Tesla", "Allego", "Shell Recharge", "Aral pulse", "Fastned",
          "Recharge", "Circle K", "Vattenfall", "Lidl", "Kaufland", "Plugsurfing", "Elli", "Maingau",
          "Eways", "OKQ8", "Tanka", "Mer", "InCharge", "Ewiva", "Enel", "Electra", "Ladenetz",
          "Pfalzwerke", "EWE Go", "Qwello", "TotalEnergies", "bp pulse", "Virta", "Freshmile",
          "Greenway", "Comfortcharge", "Wirelane", "ChargePoint", "Norlys", "Clever", "K-Lataus",
          "Wemag", "UTA")
# Betreiberkürzel der EVSE-ID (Land*Betreiber*...): eindeutig, OCR-fest.
EVSE_BETREIBER = {"TSL": "Tesla", "ION": "IONITY", "EBW": "EnBW", "EFC": "E.ON", "REC": "Recharge",
                  "NOR": "Norlys", "CLE": "Clever", "ALL": "Allego"}
# Was oft in der Zeile steht, aber kein Anbieter ist (Kartenvermerk der App).
NICHT_BETREIBER = ("google", "apple", "maps", "karten", "openstreetmap")


def _schluessel(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def betreiber_name(roh: str | None) -> str | None:
    """Ein gelesener Anbietername auf seinen festen Namen gebracht:
    "E.ON Drive Infrastructure Sweden" und "eon" -> "E.ON", "K Lataus" ->
    "K-Lataus", "Tr: SLA" -> "Tesla". Kartenvermerke ("oogle", "Google & L")
    und Bruchstücke -> None, ein sauberer unbekannter Name bleibt."""
    from difflib import SequenceMatcher
    if not roh:
        return None
    s = str(roh).strip(" ·:,;-–|")
    key = _schluessel(s)
    if len(key) < 3:
        return None
    for bad in NICHT_BETREIBER:
        if bad in key or SequenceMatcher(None, key, bad).ratio() >= 0.8:
            return None
    woerter = re.findall(r"[a-z0-9]+", s.lower())
    for marke in sorted(MARKEN, key=len, reverse=True):      # "Shell Recharge" vor "Recharge"
        mw = re.findall(r"[a-z0-9]+", marke.lower())
        if woerter[:len(mw)] == mw or key == _schluessel(marke):
            return marke
    beste = max(MARKEN, key=lambda m: SequenceMatcher(None, key, _schluessel(m)).ratio())
    if len(_schluessel(beste)) >= 4 and SequenceMatcher(None, key, _schluessel(beste)).ratio() >= 0.8:
        return beste
    if re.fullmatch(r"[A-Za-zÄÖÜäöüß0-9 .&'-]{3,40}", s) and len(re.findall(r"[A-Za-zÄÖÜäöüß]", s)) >= 3:
        return s
    return None


def betreiber_aus_evse(evse: str | None) -> str | None:
    """"SE*TSL*E1B96MF" -> "Tesla" über das Betreiberkürzel."""
    m = EVSE_RE.search(evse or "")
    return EVSE_BETREIBER.get(m.group(0).split("*")[1]) if m else None

_LABEL_KWH = re.compile(r"kwh|energie|energy|geladen|charged", re.I)
_LABEL_KOSTEN = re.compile(r"kosten|preis|betrag|price|cost|total|summe|amount", re.I)


def ocr_verfuegbar() -> bool:
    return shutil.which("tesseract") is not None


def text_aus_bild(daten: bytes, breite: int = 1200) -> str:
    """Tesseract über den Screenshot: Graustufen, auf ``breite`` Pixel
    gebracht (kleine Bilder vergrößert). Eine zweite Lesung mit anderer
    Breite liest Ziffern oft anders – der Belegweg nutzt das, wenn der
    ersten Lesung Betrag oder kWh fehlen. Wirft, wenn das Bild nicht lesbar
    oder Tesseract nicht da ist."""
    import pytesseract
    from PIL import Image
    from PIL import ImageOps, ImageStat
    bild = Image.open(io.BytesIO(daten))
    bild = bild.convert("L")
    # Dunkler Modus: helle Schrift auf Schwarz liest sich invertiert sauberer.
    if ImageStat.Stat(bild).mean[0] < 100:
        bild = ImageOps.invert(bild)
    if bild.width != breite:
        faktor = breite / bild.width
        bild = bild.resize((int(bild.width * faktor), int(bild.height * faktor)))
    sprachen = "deu+eng" if "deu" in (pytesseract.get_languages(config="") or []) else "eng"
    return pytesseract.image_to_string(bild, lang=sprachen)


def _zahl(s: str) -> float | None:
    s = s.strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d+", s):
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _datum(m: re.Match):
    g = m.groupdict()
    try:
        if g["d1"]:
            j = int(g["y1"]); j = j + 2000 if j < 100 else j
            return datetime(j, int(g["m1"]), int(g["d1"])).date()
        if g["y2"]:
            return datetime(int(g["y2"]), int(g["m2"]), int(g["d2"])).date()
        if g["d3"]:
            mon = MONATE.get(g["m3"].lower().rstrip("."))
            return datetime(int(g["y3"]), mon, int(g["d3"])).date() if mon else None
        if g["m4"]:
            mon = MONATE.get(g["m4"].lower().rstrip("."))
            return datetime(int(g["y4"]), mon, int(g["d4"])).date() if mon else None
    except (ValueError, TypeError):
        return None
    return None


def _zeit(m: re.Match):
    h, mi = int(m.group("h")), int(m.group("mi"))
    ampm = (m.group("ampm") or "").lower().replace(".", "")
    if ampm == "pm" and h < 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    return (h, mi) if h < 24 and mi < 60 else None


def _zeitpunkte(text: str) -> list[datetime]:
    """Start und Ende: jedes Datum mit der ersten Uhrzeit danach; ein
    einzelnes Datum nimmt die nächsten zwei Uhrzeiten (27.09.2026, 13:13 –
    13:43). Uhrzeiten VOR dem ersten Datum (die Statusleiste des Handys)
    zählen nicht."""
    daten = [(m.start(), _datum(m)) for m in DATUM_RE.finditer(text)]
    daten = [(p, d) for p, d in daten if d]
    zeiten = [(m.start(), _zeit(m)) for m in ZEIT_RE.finditer(text)]
    zeiten = [(p, z) for p, z in zeiten if z]
    if not daten:
        return []
    punkte: list[datetime] = []
    for i, (pos, tag) in enumerate(daten):
        grenze = daten[i + 1][0] if i + 1 < len(daten) else len(text) + 1
        danach = [z for p, z in zeiten if pos < p < grenze]
        # Ein einzelnes Datum mit zwei Uhrzeiten: Start und Ende am selben Tag.
        wieviele = 2 if len(daten) == 1 else 1
        for z in danach[:wieviele]:
            punkte.append(datetime(tag.year, tag.month, tag.day, z[0], z[1]))
        if not danach and len(daten) == 1:
            # Uhrzeit vor dem Datum in derselben Zeile ("13:13, 27.09.2026").
            zeilenanfang = text.rfind("\n", 0, pos) + 1
            davor = [z for p, z in zeiten if zeilenanfang <= p < pos]
            for z in davor[:2]:
                punkte.append(datetime(tag.year, tag.month, tag.day, z[0], z[1]))
    return punkte[:2]


def _ohne_zeiten(text: str) -> str:
    """Datum und Uhrzeit ausgeblendet, damit ihre Zahlen nicht als kWh gelten."""
    def blank(m):
        return " " * (m.end() - m.start())
    return ZEIT_RE.sub(blank, DATUM_RE.sub(blank, text))


def _reparieren(text: str) -> str:
    """Typical Tesseract slips inside amounts: a 7 read as a slash
    („12,/9 €“ → „12,79 €“)."""
    text = re.sub(r"(\d),/(\d)", r"\g<1>,7\g<2>", text)
    text = re.sub(r"(\d)/(\d{1,2})(?=\s*(?:€|eur\b))", r"\g<1>7\g<2>", text, flags=re.I)
    return text


def _ohne_statusleiste(text: str) -> str:
    """Die erste Zeile eines Handy-Screenshots ist die Statusleiste: Uhrzeit,
    Netz, Akku – Tesseract macht aus „5G" und dem Akkusymbol gern „56 €".
    Eine erste Zeile mit Uhrzeit und ohne Datum fällt weg."""
    zeilen = text.split("\n")
    for i, z in enumerate(zeilen):
        if not z.strip():
            continue
        if ZEIT_RE.search(z) and not DATUM_RE.search(z) and not KWH_RE.search(z):
            zeilen[i] = ""
        break
    return "\n".join(zeilen)


def _erste_zahl(treffer, gruppen):
    """Die erste Zahl mit Nachkommastellen, sonst die erste überhaupt – ein
    Betrag hat Cent, ein Zählerrest aus einem Symbol meist nicht."""
    werte = [(m, next(m.group(g) for g in gruppen if m.group(g))) for m in treffer]
    for m, z in werte:
        if "," in z or "." in z:
            return m, _zahl(z)
    return (werte[0][0], _zahl(werte[0][1])) if werte else (None, None)


def _energie_und_kosten(text: str) -> tuple[float | None, float | None, str | None]:
    rest = _ohne_zeiten(_ohne_statusleiste(text))
    _, kwh = _erste_zahl(list(KWH_RE.finditer(rest)), ("z",))
    kosten, waehrung = None, None
    m, wert = _erste_zahl(list(KOSTEN_RE.finditer(rest)), ("z", "z2"))
    if m:
        kosten = wert
        waehrung = WAEHRUNGEN.get((m.group("w") or m.group("w2")).lower(), "EUR")
    if kwh is None:
        # Beschriftung in der einen Zeile, Zahlen in der nächsten ("kWh geladen
        # Kosten inkl. MwSt" / "44,78 24,63 €" bei E.ON, oft mit Leerzeile
        # dazwischen): Die Zahl ohne Einheit unter einer kWh-Beschriftung ist
        # die Energie.
        zeilen = [z for z in rest.split("\n") if z.strip()]
        for i, zeile in enumerate(zeilen[:-1]):
            if not _LABEL_KWH.search(zeile) or KWH_RE.search(zeile):
                continue
            naechste = KOSTEN_RE.sub(lambda mm: " " * (mm.end() - mm.start()), zeilen[i + 1])
            zahlen = [_zahl(z) for z in re.findall(ZAHL, naechste)]
            zahlen = [z for z in zahlen if z is not None and 0 < z < 1000]
            if zahlen:
                kwh = zahlen[0]
                break
    if kwh is not None and not (0 < kwh < 1000):
        kwh = None
    return kwh, kosten, waehrung


def _betreiber(text: str, bekannte: list[str] | tuple[str, ...]) -> str | None:
    zeilen = [z.strip() for z in text.split("\n") if z.strip()]
    for name in bekannte:                       # was die Datenbank kennt, gewinnt
        if name and any(name.lower() in z.lower() for z in zeilen):
            return name
    for z in zeilen:
        for name in BEKANNTE_BETREIBER:
            # als Wort, nicht als Teil eines Worts ("Mer" in "Mercedes",
            # "Total" in "Total 12,50 €"); "Supercharg" ist ein Wortanfang
            ende = "" if name in UMBENENNEN else r"(?![A-Za-z])"
            if re.search(r"(?<![A-Za-z])" + re.escape(name) + ende, z, re.I):
                if name in UMBENENNEN:
                    return UMBENENNEN[name]
                kurz = _BEIWERK.sub("", re.split(r"\s*[\(\-–|]", z, 1)[0]).strip(" ·:,")
                return betreiber_name(kurz) or betreiber_name(name)
    return None


def _dauer_minuten(text: str) -> int | None:
    """Die Sitzungsdauer, wenn der Beleg statt einer Endzeit nur sie nennt."""
    rest = _ohne_zeiten(_ohne_statusleiste(text))
    for m in DAUER_RE.finditer(rest):
        if m.group("h2"):
            return int(m.group("h2")) * 60
        stunden = m.group("h")
        stunden = 0 if stunden in (None, "O", "o") else int(stunden)
        minuten = stunden * 60 + int(m.group("m"))
        if 0 < minuten < 24 * 60:
            return minuten
    return None


def _adresse(text: str) -> str | None:
    """Erst die Zeile mit Postleitzahl ("Brogatan 141, 89435 Örnsköldsvik"),
    sonst eine Straßenzeile samt Ort und Postleitzahl in den Zeilen darunter
    ("27 Hannoversche Straße / Hannover Niedersachsen Germany / 30916")."""
    for m in ADRESSE_RE.finditer(text):
        z = m.group(0).strip()
        if not KWH_RE.search(z) and not EVSE_RE.search(z) and not DATUM_RE.search(z):
            return z
    zeilen = [z.strip() for z in text.split("\n") if z.strip()]
    for i, z in enumerate(zeilen):
        if STRASSE_RE.match(z) and not DATUM_RE.search(z) and not KWH_RE.search(z):
            teile = [z]
            for weiter in zeilen[i + 1:i + 3]:
                if re.fullmatch(r"\d{4,5}", weiter) or (re.fullmatch(r"[A-Za-zÄÖÜäöüß .\-]{3,60}", weiter)
                                                        and not _LABEL_KWH.search(weiter)):
                    teile.append(weiter)
                else:
                    break
            return ", ".join(teile)[:160]
    return None


# ---------------------------------------------------------------- Schemata
# Jede Lade-App hat ihr festes Layout. Erst wird das Layout erkannt (an
# seinen Beschriftungen), dann liest ein Leser je Layout die Werte an den
# Stellen, an denen sie dort stehen – Beschriftung, dann Wert. Nur so ein
# Treffer speichert von selbst. Kein bekanntes Layout: der freie Leser
# (_generisch) macht einen Vorschlag, und das Formular wartet auf den Blick
# des Menschen (auto = False).

@dataclass(frozen=True)
class Schema:
    id: str
    name: str
    merkmale: tuple[str, ...]          # Regex je Merkmal, Groß/Klein egal
    mindestens: int                    # so viele Merkmale müssen da sein
    lesen: Callable[[list[str], str], dict]


def _zeilen(text: str) -> list[str]:
    return [z.strip() for z in text.split("\n") if z.strip()]


def _index(zeilen: list[str], muster: str) -> int | None:
    r = re.compile(muster, re.I)
    return next((i for i, z in enumerate(zeilen) if r.search(z)), None)


def _rest_und_folgende(zeilen: list[str], muster: str, anzahl: int = 2) -> str:
    """Was hinter einer Beschriftung steht: der Rest ihrer Zeile und die
    nächsten Zeilen – Werte stehen rechts daneben oder darunter."""
    i = _index(zeilen, muster)
    if i is None:
        return ""
    rest = re.split(muster, zeilen[i], 1, flags=re.I)[-1]
    return "\n".join([rest] + zeilen[i + 1:i + 1 + anzahl])


def _datum_zeit_in(text: str) -> datetime | None:
    """Erstes Datum und die erste Uhrzeit danach (oder davor in derselben Zeile)."""
    md = DATUM_RE.search(text)
    tag = _datum(md) if md else None
    if not tag:
        return None
    for mz in ZEIT_RE.finditer(text):
        if mz.start() > md.end():
            z = _zeit(mz)
            if z:
                return datetime(tag.year, tag.month, tag.day, z[0], z[1])
    zeile = text[:md.start()].rsplit("\n", 1)[-1]
    mz = ZEIT_RE.search(zeile)
    z = _zeit(mz) if mz else None
    return datetime(tag.year, tag.month, tag.day, z[0], z[1]) if z else None


def _kwh_in(text: str) -> float | None:
    _, wert = _erste_zahl(list(KWH_RE.finditer(text)), ("z",))
    if wert is None:
        zahlen = [_zahl(z) for z in re.findall(ZAHL, KOSTEN_RE.sub(" ", text))]
        zahlen = [z for z in zahlen if z is not None and 0 < z < 1000]
        wert = zahlen[0] if zahlen else None
    return wert if wert is not None and 0 < wert < 1000 else None


def _kosten_in(text: str) -> tuple[float | None, str | None]:
    m, wert = _erste_zahl(list(KOSTEN_RE.finditer(text)), ("z", "z2"))
    if not m:
        return None, None
    return wert, WAEHRUNGEN.get((m.group("w") or m.group("w2")).lower(), "EUR")


def _kosten_fenster(zeilen: list[str], kwh: float | None) -> tuple[float | None, str | None]:
    """The amount after a cost label when the usual line pairing failed: the
    next few non-empty lines after „Kosten“/„Preis“/…, first one with a
    currency, else the first decimal number that is not the kWh value and
    not a kWh reading — Tesseract drops the € sign now and then, or an
    address or a reference slips between label and amount."""
    i = _index(zeilen, r"kosten|preis|betrag|price|cost|amount")
    if i is None:
        return None, None
    rest = re.split(r"kosten|preis|betrag|price|cost|amount", zeilen[i], 1, flags=re.I)[-1]
    fenster = [rest] + [z for z in zeilen[i + 1:i + 8] if z.strip()][:4]
    text = "\n".join(fenster)
    kosten, waehrung = _kosten_in(text)
    if kosten is not None:
        return kosten, waehrung
    for zeile in fenster:
        if re.search(r"kwh", zeile, re.I):
            continue
        for m in re.finditer(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})*,\d{2}|\d+\.\d{2})(?![\d])", zeile):
            wert = _zahl(m.group(1))
            if wert is not None and wert != kwh:
                mw = re.search(r"\b(eur|euro|sek|nok|dkk|chf|pln|czk|gbp)\b", text, re.I)
                return wert, WAEHRUNGEN.get(mw.group(1).lower(), "EUR") if mw else "EUR"
    return None, None


def _dauer_in(text: str) -> int | None:
    m = DAUER_RE.search(text)
    if not m:
        return None
    if m.group("h2"):
        return int(m.group("h2")) * 60
    stunden = m.group("h")
    return (0 if stunden in (None, "O", "o") else int(stunden)) * 60 + int(m.group("m"))


def _uta(zeilen: list[str], text: str) -> dict:
    """UTA eCharge – Ladedetails: Station (EVSE-ID) mit Adresse darunter,
    Betreiber vor „Ladevorgangsreferenz“, „kWh geladen“ und „Kosten inkl.
    MwSt“ nebeneinander mit den Werten in der Zeile darunter, „Starten“ und
    „Beenden“ mit Datum und Uhrzeit darunter, „Gesamtzeit“."""
    f: dict = {}
    for i, z in enumerate(zeilen):
        m = EVSE_RE.search(z)
        if m:
            f["station"] = m.group(0)
            if i + 1 < len(zeilen) and re.search(r"\d", zeilen[i + 1]) and not DATUM_RE.search(zeilen[i + 1]):
                f["address"] = zeilen[i + 1]
            break
    iref = _index(zeilen, r"referenz")
    if iref is not None:
        for z in reversed(zeilen[max(0, iref - 3):iref]):
            if ")" in z and "(" not in z:
                continue                    # Ende einer Klammer aus der Zeile davor
            if re.search(r"[A-Za-z]{3}", z) and not EVSE_RE.search(z) and z != f.get("address"):
                f["operator"] = re.split(r"\s*\(", z, 1)[0].strip()[:80]
                break
        mref = re.search(r"\b\d{5,}\b", _rest_und_folgende(zeilen, r"referenz", 1))
        f["ref"] = mref.group(0) if mref else None
    werte = _rest_und_folgende(zeilen, r"kwh geladen", 1)
    f["kwh"] = _kwh_in(werte)
    kosten, waehrung = _kosten_in(werte)
    if kosten is None:
        kosten, waehrung = _kosten_in(_rest_und_folgende(zeilen, r"kosten", 1))
    if kosten is None:
        kosten, waehrung = _kosten_fenster(zeilen, f["kwh"])
    f["kosten"], f["waehrung"] = kosten, waehrung
    f["start"] = _datum_zeit_in(_rest_und_folgende(zeilen, r"^start(en)?\b", 3))
    f["ende"] = _datum_zeit_in(_rest_und_folgende(zeilen, r"^(beenden|ende|end)\b", 3))
    f["dauer"] = _dauer_in(_rest_und_folgende(zeilen, r"gesamtzeit|dauer", 1))
    return f


def _tesla(zeilen: list[str], text: str) -> dict:
    """Tesla-App – Ladevorgang: Ort und „Datum · Uhrzeit“ im Kopf, „Zugeführte
    Energie“, „Ladegebühren“ mit Tarif je kWh und Betrag, „Sitzungszeit“ statt
    Endzeit, Adresse unter „Standort“."""
    f: dict = {}
    ikopf = _index(zeilen, r"(^|<\s*)(Ladevorgang|Charging session)\s*$")
    if ikopf is not None:
        for z in zeilen[ikopf + 1:ikopf + 4]:
            if DATUM_RE.search(z):
                f["start"] = _datum_zeit_in(z)
                break
            if "location_name" not in f and re.search(r"[A-Za-z]{3}", z):
                f["location_name"] = re.sub(r"\s*[-–]\s*\S*\.{3}$|\.{3}$", "", z).strip()[:80]
    if f.get("start") is None:
        f["start"] = _datum_zeit_in("\n".join(zeilen[:8]))
    f["operator"] = "Tesla"
    f["kwh"] = _kwh_in(_rest_und_folgende(zeilen, r"Zugeführte Energie|Zugefuehrte Energie|Energy added|Energy delivered", 1))
    if f["kwh"] is None:
        f["kwh"] = _kwh_in(_rest_und_folgende(zeilen, r"Ladegebühren|Charging fees", 1))
    f["kosten"], f["waehrung"] = _kosten_in(_rest_und_folgende(zeilen, r"Ladegebühren|Ladegebuehren|Charging fees", 2))
    f["dauer"] = _dauer_in(_rest_und_folgende(zeilen, r"Sitzungszeit|Session time", 1))
    f["ende"] = _datum_zeit_in(_rest_und_folgende(zeilen, r"^(Ende|End)\b", 2)) if _index(zeilen, r"^(Ende|End)\b") is not None else None
    iort = _index(zeilen, r"^(Standort|Location)\s*$")
    if iort is not None:
        teile = []
        for z in zeilen[iort + 1:iort + 5]:
            if re.search(r"karten|maps|rechtl|legal|©", z, re.I):
                break
            if not re.search(r"\d{4,5}|[A-Za-zÄÖÜäöüß]{4,}", z):
                break                       # Reste der Karte, kein Adressteil
            teile.append(z)
        f["address"] = ", ".join(teile)[:160] or None
    return f


SCHEMATA: tuple[Schema, ...] = (
    Schema("uta", "UTA App",
           (r"Ladedetails", r"Ladevorgangsreferenz", r"Status Ladevorgang", r"kWh geladen", r"Kosten inkl"),
           2, _uta),
    Schema("tesla", "Tesla App",
           (r"Ladevorgang|Charging session", r"Einzelheiten zur Sitzung|Session details",
            r"Zugef(ü|ue)hrte Energie|Energy added|Energy delivered", r"Sitzungszeit|Session time", r"Supercharg"),
           3, _tesla),
)


def schema_erkennen(text: str) -> Schema | None:
    """Das Layout mit den meisten wiedergefundenen Beschriftungen – wenn es
    seine Mindestzahl erreicht. Zwei Apps mit denselben Wörtern gibt es
    nicht: Merkmale sind Beschriftungen, keine Zahlen."""
    beste, treffer = None, 0
    for schema in SCHEMATA:
        n = sum(1 for m in schema.merkmale if re.search(m, text, re.I))
        if n >= schema.mindestens and n > treffer:
            beste, treffer = schema, n
    return beste


def _generisch(text: str, bekannte) -> dict:
    """Ohne bekanntes Layout: Zahl neben kWh, Betrag neben Währung, erstes
    Datum mit Uhrzeit als Start, zweites als Ende – ein Vorschlag."""
    kwh, kosten, waehrung = _energie_und_kosten(text)
    punkte = _zeitpunkte(text)
    station = EVSE_RE.search(text)
    ref = REF_RE.search(text)
    return {
        "kwh": kwh, "kosten": kosten, "waehrung": waehrung,
        "start": punkte[0] if punkte else None, "ende": punkte[1] if len(punkte) > 1 else None,
        "dauer": _dauer_minuten(text) if punkte and len(punkte) < 2 else None,
        "station": station.group(0) if station else None, "address": _adresse(text),
        "ref": ref.group("r") if ref else None, "operator": _betreiber(text, bekannte),
    }


def felder_aus_text(text: str, bekannte: list[str] | tuple[str, ...] = (),
                    beschriftung: dict | None = None) -> dict:
    """Alles, was der Beleg hergibt. `bekannte`: Betreibernamen aus der
    Datenbank (ein Name, der im Beleg vorkommt, gewinnt – so passen Symbol und
    Farbe der Liste). `beschriftung`: Wörter für die Fehlt-Liste (kwh, start,
    end). `auto` sagt dem Formular, ob es ohne Nachfrage speichern darf: nur
    bei erkanntem Layout."""
    b = {"kwh": "kWh", "start": "Start", "end": "Ende", **(beschriftung or {})}
    text = _reparieren(text.replace("\r", ""))
    schema = schema_erkennen(text)
    roh = schema.lesen(_zeilen(text), text) if schema else _generisch(text, bekannte)
    kwh, kosten, waehrung = roh.get("kwh"), roh.get("kosten"), roh.get("waehrung")
    start, ende, dauer = roh.get("start"), roh.get("ende"), roh.get("dauer")
    if start and ende and ende < start:
        start, ende = ende, start
    if start and not ende and dauer:
        ende = start + timedelta(minutes=dauer)     # Tesla: Start plus Sitzungszeit
    if kwh is not None and not (0 < kwh < 1000):
        kwh = None
    station, adresse, ref = roh.get("station"), roh.get("address"), roh.get("ref")
    # Anbieter: ein Name aus der Datenbank, der im Beleg steht, gewinnt; dann
    # das Betreiberkürzel der EVSE-ID; zuletzt das Gelesene, bereinigt.
    betreiber = None
    for name in bekannte:
        if name and name.lower() in text.lower():
            betreiber = name
            break
    if betreiber is None:
        betreiber = (betreiber_aus_evse(station) or betreiber_aus_evse(text)
                     or betreiber_name(roh.get("operator")))
    teile = [t for t in (betreiber, station, adresse, f"Ref {ref}" if ref else None) if t]
    if kosten is not None and waehrung and waehrung != "EUR":
        teile.append(f"{kosten:.2f} {waehrung}")
    ort = roh.get("location_name") or betreiber or (adresse.split(",")[-1].strip() if adresse else None)
    fehlt = [b[k] for k, v in (("kwh", kwh), ("start", start), ("end", ende)) if v is None]
    return {
        "schema": schema.id if schema else "generisch",
        "schema_name": schema.name if schema else None,
        "auto": bool(schema) and not fehlt,
        "kwh": kwh,
        "cost_total": kosten if (kosten is not None and (waehrung in (None, "EUR"))) else None,
        "currency": waehrung,
        "start": start.strftime("%Y-%m-%d %H:%M") if start else None,
        "end": ende.strftime("%Y-%m-%d %H:%M") if ende else None,
        "station": station,
        "address": adresse,
        "ref": ref,
        "operator": betreiber,
        "location_name": ort,
        "note": " · ".join(teile) or None,
        "duration_min": dauer,
        "fehlt": fehlt,
    }
