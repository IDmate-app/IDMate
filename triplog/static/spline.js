/* IDmate – runde Übergänge zwischen den GPS-Punkten einer Fahrt.

   Die Strecke wird weiter Punkt für Punkt gezeichnet; nur die Ecken an den
   Punkten verschwinden: Ein zentripetaler Catmull-Rom-Spline läuft durch
   jeden gespeicherten Punkt und hat an jedem Punkt dieselbe Steigung wie
   der Abschnitt davor und dahinter. Keine Glättung über mehrere Punkte,
   kein Überschwingen (zentripetal), die Daten bleiben unangetastet – nur
   die Zeichnung bekommt Zwischenpunkte.

   Wie viele Zwischenpunkte, hängt von der Zoomstufe ab (schrittFuer): etwa
   alle vier Bildschirmpixel einer. Im Überblick ist ein Abschnitt kürzer
   als ein Schritt und bleibt gerade, das sieht dort niemand; hineingezoomt
   werden aus einer Ecke mehrere kleine Schritte. Gerechnet in Web-Mercator,
   damit die Rundung auf der Karte gleichmäßig aussieht. */
(function () {
  "use strict";
  var R = 6378137, GRAD = Math.PI / 180;
  var JE_ABSCHNITT = 40, VORRAT = 20000;

  function nachXY(p) {
    var lat = Math.max(-85.05, Math.min(85.05, p[0]));
    return [R * p[1] * GRAD, R * Math.log(Math.tan(Math.PI / 4 + lat * GRAD / 2))];
  }
  function nachGrad(q) {
    return [(2 * Math.atan(Math.exp(q[1] / R)) - Math.PI / 2) / GRAD, q[0] / R / GRAD];
  }
  function laenge(a, b) {
    var r = 6371000, p1 = a[0] * GRAD, p2 = b[0] * GRAD, dp = p2 - p1, dl = (b[1] - a[1]) * GRAD;
    var h = Math.sin(dp / 2) * Math.sin(dp / 2) + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) * Math.sin(dl / 2);
    return 2 * r * Math.asin(Math.sqrt(h));
  }
  function knoten(a, b) { return Math.sqrt(Math.hypot(b[0] - a[0], b[1] - a[1])) || 1e-9; }
  function mische(a, b, ta, tb, t) {
    var u = (tb - t) / (tb - ta), w = (t - ta) / (tb - ta);
    return [a[0] * u + b[0] * w, a[1] * u + b[1] * w];
  }

  /* punkte: [[lat, lon], …]; schritt: Sollabstand der Zwischenpunkte in Metern. */
  function rund(punkte, schritt) {
    schritt = schritt > 0 ? schritt : 5;
    var pkt = [];
    for (var i = 0; i < punkte.length; i++) {
      var v = pkt[pkt.length - 1], p = [punkte[i][0], punkte[i][1]];
      if (!v || v[0] !== p[0] || v[1] !== p[1]) pkt.push(p);
    }
    if (pkt.length < 3) return pkt;
    var xy = pkt.map(nachXY), aus = [pkt[0]], eingefuegt = 0;
    for (var k = 0; k < xy.length - 1; k++) {
      var p1 = xy[k], p2 = xy[k + 1];
      var p0 = k > 0 ? xy[k - 1] : [2 * p1[0] - p2[0], 2 * p1[1] - p2[1]];
      var p3 = k + 2 < xy.length ? xy[k + 2] : [2 * p2[0] - p1[0], 2 * p2[1] - p1[1]];
      var n = Math.min(JE_ABSCHNITT, Math.floor(laenge(pkt[k], pkt[k + 1]) / schritt), VORRAT - eingefuegt);
      if (n > 0) {
        eingefuegt += n;
        var t0 = 0, t1 = knoten(p0, p1), t2 = t1 + knoten(p1, p2), t3 = t2 + knoten(p2, p3);
        for (var j = 1; j <= n; j++) {
          var t = t1 + (t2 - t1) * j / (n + 1);
          var a1 = mische(p0, p1, t0, t1, t), a2 = mische(p1, p2, t1, t2, t), a3 = mische(p2, p3, t2, t3, t);
          var b1 = mische(a1, a2, t0, t2, t), b2 = mische(a2, a3, t1, t3, t);
          aus.push(nachGrad(mische(b1, b2, t1, t2, t)));
        }
      }
      aus.push(pkt[k + 1]);
    }
    return aus;
  }

  /* Schrittweite aus der Karte: Meter je Bildschirmpixel am Kartenmittelpunkt, mal vier. */
  function schrittFuer(map) {
    // Vor der ersten Ansicht (fitBounds kommt nach dem Zeichnen) wirft
    // getCenter() – dann ein fester Schritt, zoomend rechnet gleich nach.
    if (!map._loaded) return 5;
    var z = map.getZoom(), lat = map.getCenter().lat;
    var meterJePixel = 156543.03 * Math.cos(lat * GRAD) / Math.pow(2, z);
    return Math.max(0.5, meterJePixel * 4);
  }

  /* Eine Leaflet-Linie, die bei jedem Zoomwechsel neu gerundet wird. */
  function linie(map, punkte, opts) {
    var line = window.L.polyline(rund(punkte, schrittFuer(map)), opts).addTo(map);
    map.on("zoomend", function () { line.setLatLngs(rund(punkte, schrittFuer(map))); });
    return line;
  }

  window.IDmateSpline = { rund: rund, schrittFuer: schrittFuer, linie: linie };
})();
