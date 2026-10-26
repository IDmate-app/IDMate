/* IDmate – das Auto auf der Karte (statt des blauen Punkts im Stand).

   Draufsicht mit Scheiben in heutigen Proportionen (Model 3, ID.7, Enyaq):
   Limousine, Kombi und SUV, gezeichnet in der Wagenfarbe aus den Fahrzeug-
   Einstellungen. Umriss, Dach und Scheiben leiten sich aus der Farbe ab –
   dunkle Wagen bekommen einen helleren Umriss, helle einen dunkleren, damit
   Weiß und Schwarz gleich gut auf der Karte stehen. Die Lichtbänder sitzen
   in einem dunklen Gehäuse, sonst verschwänden sie auf einem weißen Wagen.

   IDmateKartenauto.svg(typ, farbe, { halo, breite, drehung }) -> SVG-Text.
   Abgestimmt in der Review-Palette (Ansatz B, Elektro, 32 px). */
(function () {
  "use strict";
  function hex(c) { var m = /^#?([0-9a-f]{6})$/i.exec(String(c || "").trim()); return m ? "#" + m[1].toLowerCase() : null; }
  function rgb(c) { var h = hex(c) || "#6e7681"; return [1, 3, 5].map(function (i) { return parseInt(h.slice(i, i + 2), 16); }); }
  function toHex(r) { return "#" + r.map(function (v) { return Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0"); }).join(""); }
  function mix(c, ziel, t) { var a = rgb(c); return toHex(a.map(function (v, i) { return v + (ziel[i] - v) * t; })); }
  function lum(c) { var p = rgb(c).map(function (v) { v /= 255; return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); }); return .2126 * p[0] + .7152 * p[1] + .0722 * p[2]; }
  function toene(f) {
    var dunkel = lum(f) < 0.18;
    return {
      body: f,
      dach: dunkel ? mix(f, [255, 255, 255], .16) : mix(f, [255, 255, 255], .14),
      tief: dunkel ? mix(f, [255, 255, 255], .06) : mix(f, [0, 0, 0], .10),
      rand: dunkel ? mix(f, [255, 255, 255], .5) : mix(f, [0, 0, 0], .4),
      glas: dunkel ? "#66788a" : "#243039",
      glasrand: dunkel ? "#9fb0bf" : mix(f, [0, 0, 0], .55),
    };
  }

  const SPIEGEL_60 = "M54 45 L57 45 C62 45 65.5 48 64.5 52 C63.5 55 59.5 55.5 56.5 53 L54 50 Z M6 45 L3 45 C-2 45 -5.5 48 -4.5 52 C-3.5 55 0.5 55.5 3.5 53 L6 50 Z";
  const SPIEGEL_64 = "M58 44 L61 44 C66 44 69.5 47 68.5 51 C67.5 54 63.5 54.5 60.5 52 L58 49 Z M6 44 L3 44 C-2 44 -5.5 47 -4.5 51 C-3.5 54 0.5 54.5 3.5 52 L6 49 Z";
  const TOP_MODERN = {
    limousine: { name: "Limousine", vb: [60, 152],
      body: "M30 3 C44 3 52 9 54 20 C55 28 56 36 55 48 C54 60 54 72 55 84 C55.5 96 56 104 55.5 116 C55 132 53 144 44 148 L16 148 C7 144 5 132 4.5 116 C4 104 4.5 96 5 84 C6 72 6 60 5 48 C4 36 5 28 6 20 C8 9 16 3 30 3 Z",
      front: "M9 47 L13 31 Q30 27.5 47 31 L51 47 Z",
      dach: "M10 53 L50 53 C50.5 68 50 84 49 97 L11 97 C10 84 9.5 68 10 53 Z",
      heck: "M11 104 L49 104 C48 118 44 128 38 131 L22 131 C16 128 12 118 11 104 Z",
      haube: "M14 12 C22 8 38 8 46 12 L51 40 L9 40 Z",
      spiegel: SPIEGEL_60,
      lichtVorn: "M13 13 Q30 7 47 13", lichtHinten: "M13 144 Q30 147 47 144", reling: null, spoiler: null },
    kombi: { name: "Kombi", vb: [60, 152],
      body: "M30 3 C44 3 52 9 54 20 C55 28 56 36 55 48 C54 60 54 72 55 84 C55.5 96 56 104 55.5 116 C55 132 53 144 44 148 L16 148 C7 144 5 132 4.5 116 C4 104 4.5 96 5 84 C6 72 6 60 5 48 C4 36 5 28 6 20 C8 9 16 3 30 3 Z",
      front: "M9 47 L13 31 Q30 27.5 47 31 L51 47 Z",
      dach: "M10 53 L50 53 C51 68 51 84 50.5 98 L9.5 98 C9 84 9 68 10 53 Z",
      heck: "M9 130 L51 130 C50 137 47 141 43 142 L17 142 C13 141 10 137 9 130 Z",
      haube: "M14 12 C22 8 38 8 46 12 L51 40 L9 40 Z",
      spiegel: SPIEGEL_60,
      lichtVorn: "M13 13 Q30 7 47 13", lichtHinten: "M13 144 Q30 147 47 144",
      reling: [[11, 52, 11, 124], [49, 52, 49, 124]], spoiler: "M9 126 L51 126 L51 130 L9 130 Z" },
    suv: { name: "SUV", vb: [64, 147],
      body: "M32 3 C46 3 56 8 58 20 C60 30 60 40 59 52 C58 66 58 80 59 94 C59.5 106 59.5 116 58.5 124 C57.5 136 53 143 44 145 L20 145 C11 143 6.5 136 5.5 124 C4.5 116 4.5 106 5 94 C6 80 6 66 5 52 C4 40 4 30 6 20 C8 8 18 3 32 3 Z",
      front: "M8 46 L12 29 Q32 25.5 52 29 L56 46 Z",
      dach: "M9 52 L55 52 C56 68 56 84 55.5 100 L8.5 100 C8 84 8 68 9 52 Z",
      heck: "M8 118 L56 118 C55 126 51 131 46 132 L18 132 C13 131 9 126 8 118 Z",
      haube: "M14 11 C22 7 42 7 50 11 L56 38 L8 38 Z",
      spiegel: SPIEGEL_64,
      lichtVorn: "M13 12 Q32 6 51 12", lichtHinten: "M13 139 Q32 143 51 139",
      reling: [[10, 50, 10, 112], [54, 50, 54, 112]], spoiler: "M8 114 L56 114 L56 118 L8 118 Z" },
  };

  var TYPEN = TOP_MODERN;
  var STANDARDFARBE = "#6e7681";
  var verlaufNr = 0;

  function svg(typ, farbe, opt) {
    opt = opt || {};
    var t = TYPEN[typ];
    if (!t) return "";
    var f = hex(farbe) || STANDARDFARBE, c = toene(f);
    var halo = opt.halo || "#ffffff";
    var vw = t.vb[0], vh = t.vb[1];
    var gid = "kartenauto-w" + (++verlaufNr);
    var reling = t.reling ? t.reling.map(function (l) {
      return '<line x1="' + l[0] + '" y1="' + l[1] + '" x2="' + l[2] + '" y2="' + l[3] + '" stroke="' + c.rand + '" stroke-width="2" stroke-linecap="round" opacity=".7"/>'; }).join("") : "";
    var breite = opt.breite || 24, hoehe = Math.round(breite * vh / vw);
    var dreh = opt.drehung ? ' style="transform:rotate(' + (Math.round(opt.drehung) % 360) + 'deg)"' : "";
    return '<svg viewBox="-5 -4 ' + (vw + 10) + ' ' + (vh + 8) + '" width="' + breite + '" height="' + hoehe + '" xmlns="http://www.w3.org/2000/svg" aria-label="' + t.name + '"' + dreh + '>'
      + '<defs><linearGradient id="' + gid + '" x1="0" x2="1" y1="0" y2="0"><stop offset="0" stop-color="' + c.tief + '"/><stop offset=".5" stop-color="' + c.dach + '"/><stop offset="1" stop-color="' + c.tief + '"/></linearGradient></defs>'
      + '<path d="' + t.body + '" fill="none" stroke="' + halo + '" stroke-width="7" stroke-linejoin="round"/>'
      + '<path d="' + t.body + '" fill="url(#' + gid + ')" stroke="' + c.rand + '" stroke-width="2" stroke-linejoin="round"/>'
      + '<path d="' + t.haube + '" fill="' + c.dach + '" opacity=".35"/>'
      // Dach: Frontscheibe, A-Holm in Wagenfarbe, Glasdach, hinterer Holm in
      // Wagenfarbe, Heckscheibe – die Holme sind das Blech zwischen den Gläsern.
      + '<path d="' + t.dach + '" fill="' + c.glas + '" opacity=".8" stroke="' + c.glasrand + '" stroke-width=".8"/>'
      + '<path d="' + t.front + '" fill="' + c.glas + '" stroke="' + c.glasrand + '" stroke-width=".8"/>'
      + '<path d="' + t.heck + '" fill="' + c.glas + '" stroke="' + c.glasrand + '" stroke-width=".8"/>'
      + (t.spoiler ? '<path d="' + t.spoiler + '" fill="' + c.tief + '"/>' : "")
      + reling
      + '<path d="' + t.lichtVorn + '" fill="none" stroke="' + c.glas + '" stroke-width="3.2" stroke-linecap="round"/>'
      + '<path d="' + t.lichtVorn + '" fill="none" stroke="#e9f4ff" stroke-width="1.3" stroke-linecap="round"/>'
      + '<path d="' + t.lichtHinten + '" fill="none" stroke="' + c.glas + '" stroke-width="3.2" stroke-linecap="round"/>'
      + '<path d="' + t.lichtHinten + '" fill="none" stroke="#ff3b3b" stroke-width="1.4" stroke-linecap="round"/>'
      + '<path d="' + t.spiegel + '" fill="' + c.body + '" stroke="' + c.rand + '" stroke-width="1.5" stroke-linejoin="round"/>'
      + '</svg>';
  }

  window.IDmateKartenauto = { svg: svg, TYPEN: Object.keys(TYPEN), STANDARDFARBE: STANDARDFARBE, hex: hex };
})();
