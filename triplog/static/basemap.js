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
})();
