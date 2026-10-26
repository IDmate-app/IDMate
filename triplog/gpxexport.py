"""GPX 1.1 writer for the trip and journey exports.

Names are XML-escaped (a journey called "Finnland & Schweden" used to break
the whole file), speed goes into <extensions> where GPX 1.1 allows it, and
the file name is reduced to safe ASCII.
"""

import re
from xml.sax.saxutils import escape


def gpx_dokument(name, tracks):
    """``tracks``: [(track name, [(lat, lon, time_iso or None, speed_kmh or None), ...]), ...]"""
    zeilen = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="IDmate"',
        '     xmlns="http://www.topografix.com/GPX/1/1"',
        '     xmlns:idmate="https://github.com/IDmate-app/IDMate"',
        '     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '     xsi:schemaLocation="http://www.topografix.com/GPX/1/1 http://www.topografix.com/GPX/1/1/gpx.xsd">',
        f'  <metadata><name>{escape(str(name or ""))}</name></metadata>',
    ]
    for trackname, punkte in tracks:
        zeilen += ['  <trk>', f'    <name>{escape(str(trackname or ""))}</name>', '    <trkseg>']
        for lat, lon, zeit, speed in punkte:
            zeile = f'      <trkpt lat="{float(lat):.6f}" lon="{float(lon):.6f}">'
            if zeit:
                zeile += f'<time>{escape(str(zeit))}</time>'
            if speed is not None:
                zeile += f'<extensions><idmate:speed>{float(speed) / 3.6:.1f}</idmate:speed></extensions>'
            zeilen.append(zeile + '</trkpt>')
        zeilen += ['    </trkseg>', '  </trk>']
    zeilen.append('</gpx>')
    return "\n".join(zeilen)


def dateiname(*teile):
    """``journey_Finnland_Schweden_2026_2026-09-13.gpx`` — ASCII, no spaces,
    nothing a header or a file system would choke on."""
    saubere = []
    for t in teile:
        t = (str(t or "").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
             .replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue"))
        t = re.sub(r"[^A-Za-z0-9.-]+", "_", t).strip("_")
        if t:
            saubere.append(t)
    return ("_".join(saubere) or "export") + ".gpx"
