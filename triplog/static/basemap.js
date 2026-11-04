/* IDmate – Grundebene jeder Karte. Übernommen aus Roundtrip (static/js/basemap.js),
   auf das Nötige gekürzt.

   Eine Kacheladresse, zwei Wege:
   - endet sie auf .mvt, sind es die Vektorkacheln der Roundtrip-Weltkarte
     (Flask reicht sie unter /tiles/{z}/{x}/{y}.mvt durch, ROUNDTRIP_URL in
     config.py). Der Browser zeichnet sie mit protomaps-leaflet nach den
     Regeln aus maprules.js, in den Farben aus mapstyle.js – beide Dateien
     sind unveränderte Kopien aus Roundtrip, damit die Karten in beiden Apps
     gleich aussehen. Zum Nachziehen: die zwei Dateien plus vendor/ neu
     kopieren, sonst nichts.
   - sonst gewöhnliche Rasterkacheln (die Kartenstile aus MAP_STYLES), wie
     bisher, für Installationen ohne Roundtrip.

   Reihenfolge: nach leaflet.js, protomaps-leaflet.js, mapstyle.js und
   maprules.js (siehe templates/_map_scripts.html), vor allem, was Karten baut. */
(function () {
  "use strict";
  var M = (window.IDmateMap = window.IDmateMap || {});

  // Quellenhinweis ohne „Leaflet"-Vorspann: OpenStreetMap zu nennen ist
  // Lizenzpflicht (ODbL), auch für die eigene Weltkarte – die Bibliothek zu
  // nennen ist keine.
  if (window.L && L.Control && L.Control.Attribution) L.Control.Attribution.prototype.options.prefix = false;
  var OSM = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';

  M.isDark = function () {
    // IDmate: '' = dunkel (Vorgabe), 'gt' = schwarz, 'light' = hell.
    return (document.documentElement.dataset.theme || "") !== "light";
  };
  M.isVectorUrl = function (url) { return /\.mvt(\?.*)?$/i.test(url || ""); };

  /* Beschriftung 15 % größer (Straßennamen, Orte), bei unverändertem Zoom
     und denselben Kacheln: Die Schrift der Karte war zu klein, aber die
     Fläche im Bild soll bleiben. Die Bibliothek setzt Schriftgrade je Stufe
     als CSS-Font („400 12px sans-serif"); hier wird der px-Wert skaliert –
     in jeder Regel, auch in verschachtelten (Gruppe, Schild, Versatz). */
  M.LABEL_SCALE = 1.15;
  M.scaleFont = function (font) {
    return String(font).replace(/(\d+(?:\.\d+)?)px/g, function (m, n) {
      return (Math.round(parseFloat(n) * M.LABEL_SCALE * 10) / 10) + "px";
    });
  };
  function schriftVergroessern(obj, tiefe) {
    if (!obj || typeof obj !== "object" || tiefe > 4) return;
    if (obj.font != null) {
      var f = obj.font;
      if (f && typeof f.get === "function") {
        // protomaps-leaflet 3: ein Attribut-Objekt mit get(zoom, feature)
        var get = f.get.bind(f);
        f.get = function (z, e) { return M.scaleFont(get(z, e)); };
      } else if (typeof f === "function") {
        obj.font = function (z, e) { return M.scaleFont(f(z, e)); };
      } else {
        obj.font = M.scaleFont(f);
      }
    }
    Object.keys(obj).forEach(function (k) {
      var v = obj[k];
      if (Array.isArray(v)) v.forEach(function (x) { schriftVergroessern(x, tiefe + 1); });
      else if (v && typeof v === "object" && k !== "font") schriftVergroessern(v, tiefe + 1);
      // (font selbst nicht weiter durchsuchen: ist bereits umgebogen)
    });
  }
  M.labelRules = function (flavor) {
    var rules = window.protomapsL.labelRules(flavor, "de");
    rules.forEach(function (r) { schriftVergroessern(r.symbolizer, 0); });
    return rules;
  };

  /* opts.maxZoom: wie weit die Karte vergrößert (Vorgabe 19). Die Weltkarte
     reicht bis Stufe 15 (maxDataZoom), darüber vergrößert der Browser die
     tiefste Kachel – kein Kachelabruf jenseits davon.
     opts.labels === false: keine Beschriftung (Netzabdeckungskarte, wo die
     farbigen Messpunkte allein sprechen sollen). */
  M.baseLayer = function (url, opts) {
    opts = opts || {};
    var maxZoom = opts.maxZoom || 19;
    var stil = window.RoundtripMapStyle;
    var vektor = M.isVectorUrl(url) && window.protomapsL && stil;
    var layer;
    if (!vektor) {
      layer = L.tileLayer(url, {
        attribution: opts.attribution, maxZoom: maxZoom,
        // Raster tiles end at 19; past that the finest tile is scaled up.
        maxNativeZoom: Math.min(maxZoom, 19),
        // OSM verlangt einen Referer; ohne diese Angabe erben die Kachelbilder
        // eine strengere Seitenrichtlinie und der Server antwortet mit 403.
        referrerPolicy: "strict-origin-when-cross-origin", crossOrigin: true,
      });
    } else {
      var dunkel = !!stil.followsTheme && M.isDark();
      var flavor = stil.flavor(dunkel);
      var regeln = window.RoundtripMapRules;
      layer = protomapsL.leafletLayer({
        url: url, attribution: OSM,
        paintRules: regeln ? regeln.paint(flavor, dunkel) : protomapsL.paintRules(flavor),
        labelRules: opts.labels === false ? [] : M.labelRules(flavor),
        backgroundColor: flavor.background,
        maxDataZoom: 15, maxZoom: maxZoom,
      });
    }
    // Die Vektorkarte färbt sich selbst (mapstyle.js): kein Rasterfilter
    // (idmate.css .map--vector). Und sie nennt ihre Quelle – auch auf Karten,
    // die ohne attributionControl angelegt wurden.
    layer.on("add", function (e) {
      var map = e.target._map;
      if (!map) return;
      if (map._container) map._container.classList.toggle("map--vector", !!vektor);
      if (vektor && !map.attributionControl) {
        map.attributionControl = L.control.attribution({ prefix: false }).addTo(map);
        map.attributionControl.addAttribution(OSM);
      }
    });
    return layer;
  };

  /* Ladeort eines Punkts, nach derselben Regel wie der Server
     (app.best_charge_location): Polygon wie gezeichnet, altes Rechteck, sonst
     Kreis. Überlappen sich Ladeorte (Allego und Tesla nebeneinander), gewinnt
     der kleinste, bei gleicher Größe die nähere Mitte. null = keiner. */
  function havM(la1, lo1, la2, lo2) {
    var r = Math.PI / 180, dLa = (la2 - la1) * r, dLo = (lo2 - lo1) * r;
    var h = Math.sin(dLa / 2) * Math.sin(dLa / 2)
          + Math.cos(la1 * r) * Math.cos(la2 * r) * Math.sin(dLo / 2) * Math.sin(dLo / 2);
    return 6371000 * 2 * Math.asin(Math.sqrt(h));
  }
  function polyOf(f) {
    if (f.shape !== "polygon" || !f.polygon_coords) return null;
    try {
      var c = typeof f.polygon_coords === "string" ? JSON.parse(f.polygon_coords) : f.polygon_coords;
      return c && c.length >= 3 ? c : null;
    } catch (e) { return null; }
  }
  function inPoly(lat, lon, c) {
    var inside = false;
    for (var i = 0, j = c.length - 1; i < c.length; j = i++) {
      var yi = c[i][0], xi = c[i][1], yj = c[j][0], xj = c[j][1];
      if ((yi > lat) !== (yj > lat) && lon < (xj - xi) * (lat - yi) / ((yj - yi) || 1e-12) + xi) inside = !inside;
    }
    return inside;
  }
  function isRect(f) { return f.shape === "rect" && f.lat2 != null && f.lon2 != null; }
  function contains(f, lat, lon) {
    var c = polyOf(f);
    if (f.shape === "polygon" && f.polygon_coords) return !!c && inPoly(lat, lon, c);
    if (f.lat == null || f.lon == null) return false;
    if (isRect(f)) return lat >= Math.min(f.lat, f.lat2) && lat <= Math.max(f.lat, f.lat2)
                       && lon >= Math.min(f.lon, f.lon2) && lon <= Math.max(f.lon, f.lon2);
    return havM(lat, lon, f.lat, f.lon) <= (f.radius_m || 200);
  }
  function areaM2(f) {
    var c = polyOf(f);
    if (c) {
      var lat0 = 0; c.forEach(function (p) { lat0 += p[0]; });
      var k = Math.cos(lat0 / c.length * Math.PI / 180), a = 0;
      for (var i = 0, j = c.length - 1; i < c.length; j = i++)
        a += (c[j][1] * 111320 * k) * (c[i][0] * 110540) - (c[i][1] * 111320 * k) * (c[j][0] * 110540);
      return Math.abs(a) / 2;
    }
    if (isRect(f)) return Math.abs(f.lat - f.lat2) * 110540
                        * Math.abs(f.lon - f.lon2) * 111320 * Math.cos(f.lat * Math.PI / 180);
    var r = f.radius_m || 200;
    return Math.PI * r * r;
  }
  M.chargeFenceAt = function (lat, lon, fences) {
    if (lat == null || lon == null) return null;
    var best = null, bestA = Infinity, bestD = Infinity;
    (fences || []).forEach(function (f) {
      if (!contains(f, lat, lon)) return;
      var clat = isRect(f) ? (f.lat + f.lat2) / 2 : f.lat, clon = isRect(f) ? (f.lon + f.lon2) / 2 : f.lon;
      var a = areaM2(f), d = clat != null ? havM(lat, lon, clat, clon) : 0;
      if (a < bestA || (a === bestA && d < bestD)) { best = f; bestA = a; bestD = d; }
    });
    return best;
  };
})();
