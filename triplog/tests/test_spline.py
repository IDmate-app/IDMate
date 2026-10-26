"""Runde Übergänge zwischen den GPS-Punkten (1.2.152-dev, static/spline.js).

Asked for: no smoothing across several points, only the transitions – the
curve must pass through every GPS point and leave each point with the same
slope it arrived with. The script is plain JavaScript; the test runs it in
node with a stub window and checks the geometry, and checks the pages wire
it in."""

import json
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PRUEFUNG = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
global.window = {};
new Function("window", src)(window);
const S = window.IDmateSpline;
// Kreisverkehr und Stadtstraße: dichte Punkte alle ~30 m mit echten Ecken.
const pts = [[63.29, 18.71], [63.2902, 18.7104], [63.2903, 18.711], [63.2902, 18.7116], [63.29, 18.7118],
             [63.2898, 18.7115], [63.2897, 18.7109], [63.2899, 18.7104], [63.2903, 18.7101]];
const rund = S.rund(pts, 3);
const gleich = (a, b) => a[0] === b[0] && a[1] === b[1];
const idx = pts.map(p => rund.findIndex(q => gleich(p, q)));
// Winkel in Metern (Web-Mercator), nicht in Grad – bei 63° Breite ist ein
// Längengrad nur halb so lang wie ein Breitengrad.
const R = 6378137, G = Math.PI / 180, xy = p => [R * p[1] * G, R * Math.log(Math.tan(Math.PI / 4 + p[0] * G / 2))];
const winkel = (a, b, c) => { a = xy(a); b = xy(b); c = xy(c); const u = [b[0]-a[0], b[1]-a[1]], v = [c[0]-b[0], c[1]-b[1]];
  const d = (u[0]*v[0]+u[1]*v[1]) / (Math.hypot(...u)*Math.hypot(...v)); return Math.acos(Math.max(-1, Math.min(1, d))) * 180 / Math.PI; };
// Knick an den Originalpunkten (ankommendes gegen abgehendes Teilstück) und
// zum Vergleich die Biegung der Teilstücke dazwischen: gleiche Steigung heißt,
// am Punkt biegt die Kurve nicht stärker als daneben.
const knicke = idx.slice(1, -1).map(i => winkel(rund[i-1], rund[i], rund[i+1]));
const innen = rund.map((q, i) => (i > 0 && i < rund.length - 1 && !idx.includes(i)) ? winkel(rund[i-1], q, rund[i+1]) : 0);
const ohne = pts.slice(1, -1).map((p, i) => winkel(pts[i], p, pts[i+2]));
const lats = pts.map(p => p[0]), lons = pts.map(p => p[1]);
console.log(JSON.stringify({
  mehr: rund.length > pts.length * 4,
  alleDrin: idx.every(i => i >= 0), reihenfolge: idx.every((i, n) => n === 0 || i > idx[n-1]),
  endenGenau: gleich(rund[0], pts[0]) && gleich(rund[rund.length-1], pts[pts.length-1]),
  maxKnick: Math.max(...knicke), maxInnen: Math.max(...innen), maxKnickVorher: Math.max(...ohne),
  imRahmen: rund.every(q => q[0] >= Math.min(...lats) - 0.0002 && q[0] <= Math.max(...lats) + 0.0002
                         && q[1] >= Math.min(...lons) - 0.0004 && q[1] <= Math.max(...lons) + 0.0004),
  grobUnveraendert: S.rund(pts, 5000).length === pts.length,
  zweiBleibenZwei: S.rund([[1, 2], [3, 4]], 1).length === 2,
  doppelteOk: !S.rund([[1, 2], [1, 2], [1.001, 2.001], [1.002, 2.0]], 1).some(q => q.some(Number.isNaN)),
  deckel: S.rund(Array.from({ length: 3000 }, (_, i) => [50 + i * 0.01, 8 + (i % 2) * 0.01]), 0.1).length <= 3000 + 20000,
  schritt: S.schrittFuer({ _loaded: true, getZoom: () => 17, getCenter: () => ({ lat: 63 }) }),
  // Leaflet vor der ersten Ansicht: getCenter() wirft. Die Linie muss trotzdem entstehen
  // (1.2.152 blieb die Karte grau, weil das Skript hier abbrach).
  ohneAnsicht: (() => {
    window.L = { polyline: (p, o) => ({ n: p.length, addTo() { return this; }, setLatLngs() {} }) };
    const ereignisse = [];
    const map = { _loaded: false, getZoom: () => undefined, getCenter: () => { throw new Error("Set map center and zoom first."); },
                  on: (name) => ereignisse.push(name) };
    try { const l = S.linie(map, pts, {}); return l.n > pts.length && ereignisse.includes("zoomend"); } catch (e) { return "wirft: " + e.message; }
  })(),
}));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node fehlt")
def test_spline_passes_every_point_and_removes_the_kinks(tmp_path):
    skript = tmp_path / "pruefung.js"
    skript.write_text(PRUEFUNG, encoding="utf-8")
    r = subprocess.run(["node", str(skript), os.path.join(HERE, "static", "spline.js")],
                       capture_output=True, text=True, check=True)
    e = json.loads(r.stdout)
    assert e["mehr"] and e["alleDrin"] and e["reihenfolge"] and e["endenGenau"]
    # Vorher echte Ecken (über 100 Grad im Kreisverkehr). Jetzt biegt die Kurve
    # am Punkt nicht stärker als zwischen den Punkten: gleiche Steigung.
    assert e["maxKnickVorher"] > 60 and e["maxKnick"] < 12, (e["maxKnickVorher"], e["maxKnick"])
    assert e["maxKnick"] <= e["maxInnen"] * 1.3 + 1, (e["maxKnick"], e["maxInnen"])
    assert e["imRahmen"] and e["grobUnveraendert"] and e["zweiBleibenZwei"] and e["doppelteOk"] and e["deckel"]
    # Zoom 17 an 63° Breite: rund 0,54 m je Pixel, also etwa 2 m Schritt.
    assert 1.5 < e["schritt"] < 3
    assert e["ohneAnsicht"] is True, e["ohneAnsicht"]


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'dev1')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) "
               "VALUES (1, 'admin', 'x', 1, 1)")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def test_pages_draw_routes_through_the_spline(client, db):
    assert "IDmateSpline" in client.get("/static/spline.js").get_data(as_text=True)
    db.execute("INSERT INTO trips (id, device, start_time, end_time, distance_km) VALUES (1, 'dev1', '2026-09-27T10:00:00', '2026-09-27T10:30:00', 12)")
    db.commit()
    html = client.get("/trips/1").get_data(as_text=True)
    assert "IDmateSpline.linie(map, routePoints" in html and "spline.js" in html
    jid = client.post("/api/journeys", json={"title": "Test", "date_from": "2026-09-27", "date_to": "2026-09-28", "excluded_ids": []}).get_json()["id"]
    assert "IDmateSpline.linie(map, seg, lineOpts)" in client.get(f"/journeys/{jid}").get_data(as_text=True)
