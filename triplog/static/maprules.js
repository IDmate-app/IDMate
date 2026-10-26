/* Zeichenregeln der eigenen Weltkarte: wer ist Autobahn, wer ist Feldweg,
   was ist Grenze, was fährt auf Schienen.

   Die Bibliothek protomaps-leaflet bringt eigene Regeln mit. Die malen jede
   Straße gleich weiß, zeichnen jede Verwaltungsgrenze – auch die im offenen
   Meer – und legen Häuser halb durchsichtig aufs Land. Deshalb zeichnet die
   App nach diesen Regeln hier:

     Straßen     wie in der deutschen Straßenkarte: die Autobahn als Band
                 mit roter Fassung und orangem Kern („dreifache Linien"),
                 Bundesstraße rot, Landstraße gelb, Kreisstraße blassgelb,
                 Nebenstraße weiß – jede mit ihrer Fassung, damit sie sich
                 vom Land absetzt.
     Grenzen     nur Staatsgrenzen, und nur an Land. Der Trick steckt in der
                 Reihenfolge: Die Grenzlinie liegt UNTER dem Meer. Wo sie
                 aufs Wasser hinausläuft, malt die Karte anschließend das
                 Meer darüber – die Linie endet also von selbst an der Küste.
                 Flüsse und Seen liegen darunter, Grenzen im Rhein oder im
                 Bodensee bleiben deshalb sichtbar.
     Nahverkehr  nach dem Vorbild von Andrang: Bahn, S-/U-Bahn und Tram,
                 Seilbahn und Fähre, jede mit eigener Farbe und Strichelung.
                 Auch von dort übernommen: Bahnhofs- und Rangiergleise
                 (service=…) bleiben weg, sonst versinkt jeder Bahnhof in
                 einem Gewirr paralleler Striche.
     Wasser      bleibt bei der Bibliothek – Seen, Meer und Wasserläufe.
                 1.135.2 zeichnete Flüsse und Bäche selbst, breiter und
                 früher, weil „der Fluss vor der Haustür" fehlte; der fehlte
                 aber in den Daten (in Finnland sind viele Bäche nicht
                 erfasst), nicht in der Zeichnung. Seit 1.138.0 wieder ohne
                 eigene Wasserlinien.

   Die Farben holt jede Regel aus mapstyle.js, sobald die Datei den Schlüssel
   kennt, sonst aus der Tabelle unten. Eine ältere Kopie von mapstyle.js im
   Datenordner hält die neue Optik deshalb nicht auf.

   Zoomstufen: minzoom einer Regel und die Stützstellen der Breiten zählen
   beide die Anzeigestufe. Die Kachel darunter ist eine Stufe gröber
   (protomaps-leaflet, levelDiff 1) und ab 15 kommt keine tiefere mehr – was
   in der Kachel dieser Stufe nicht steht, kann keine Regel zeigen. Deshalb
   erscheinen Bahn und Fähre erst ab Anzeigestufe 12, U-Bahn, Tram und S-Bahn
   erst ab 15; Breiten wachsen darüber hinaus weiter.
*/
window.RoundtripMapRules = (function () {
  "use strict";

  /* Vorgabefarben. Wer sie ändern will, tut das in mapstyle.js – jeder
     Schlüssel dort sticht den hier. Die Schlüssel stehen im Kopf jener Datei.

     Die Straßenfarben folgen der deutschen Straßenkarte (Generalkarte, ADAC,
     Falk), nicht der Beschilderung. Der Unterschied steckt in der Autobahn:
     Auf dem Schild ist sie blau, auf der Karte ist sie ein „dreifaches Band,
     außen zweimal rot, in der Mitte orange" – Fassung und Füllung sind zwei
     verschiedene Farben, nicht ein Ton und sein dunklerer Schatten. Darunter
     geht es weiter wie im Atlas: Bundesstraße rot, Landstraße gelb,
     Kreisstraße blassgelb, Nebenstraße weiß. */
  var STD = {
    light: {
      autobahn: "#fac44e", autobahn_rand: "#d2472c",
      bundesstrasse: "#e2604a", bundesstrasse_rand: "#ac3b28",
      landstrasse: "#f0cd74", landstrasse_rand: "#c9a049",
      kreisstrasse: "#f8e7b4", kreisstrasse_rand: "#d6c087",
      nebenstrasse: "#ffffff", nebenstrasse_rand: "#c9c2b6",
      erschliessung: "#f6f3ec", erschliessung_rand: "#d8d2c5",
      weg: "#b0a18b",
      bahn: "#78889b", bahn_schwelle: "#f0ede7",
      nahverkehr: "#2f9187",
      sonderbahn: "#6f8296",
      faehre: "#4a8fb6",
      grenze: "#93809b",
      haus_rand: "#b5ad9e",
      buildings: "#d5cec2", water: "#80deea", runway: "#e9e9ed", pier: "#e0e0e0",
    },
    dark: {
      autobahn: "#a87a2d", autobahn_rand: "#8c3524",
      bundesstrasse: "#8a4033", bundesstrasse_rand: "#5e251c",
      landstrasse: "#6f6235", landstrasse_rand: "#4b4124",
      kreisstrasse: "#4c4837", kreisstrasse_rand: "#343125",
      nebenstrasse: "#3d3d3d", nebenstrasse_rand: "#282828",
      erschliessung: "#333333", erschliessung_rand: "#222222",
      weg: "#4a463e",
      bahn: "#5f6a78", bahn_schwelle: "#1f1f1f",
      nahverkehr: "#2b7b73",
      sonderbahn: "#55616f",
      faehre: "#3b6f8c",
      grenze: "#6e6478",
      haus_rand: "#2b2b2b",
      buildings: "#161616", water: "#31353f", runway: "#333333", pier: "#333333",
    },
  };

  // Breiten der Straßenklassen, von der Kachelstufe abhängig. Übernommen aus
  // den Vorlagen von @protomaps/basemaps, damit die Karte in den Stufen
  // atmet wie gewohnt.
  var BREITEN = {
    autobahn:      [[5, 0.4], [7, 1], [10, 1.5], [12, 2], [15, 5], [18, 15]],
    bundesstrasse: [[6, 0.4], [9, 0.9], [12, 1.6], [15, 3.4], [18, 13]],
    landstrasse:   [[8, 0.4], [11, 0.8], [14, 1.8], [18, 11]],
    kreisstrasse:  [[10, 0.4], [13, 1.1], [18, 9]],
    nebenstrasse:  [[12, 0.4], [14, 0.9], [18, 8]],
    erschliessung: [[13, 0.4], [15, 0.9], [18, 6]],
    auffahrt:      [[11, 0.4], [14, 1], [18, 6]],
  };
  // Wie breit die Fassung im Verhältnis zur Füllung ist. Nur die Autobahn
  // trägt hier einen Wert: Sie ist auf der deutschen Karte ein Band mit
  // sichtbaren roten Kanten, keine Linie mit einem Saum. Alle anderen
  // bekommen den festen Zuschlag (rand), der bloß die Klasse vom Land trennt.
  var RANDANTEIL = { autobahn: 1.75 };
  // Ab welcher Anzeigestufe eine Klasse überhaupt erscheint. Darunter wäre
  // sie ein Haar auf dem Bild und kostet nur Zeit.
  var AB = {
    autobahn: 6, bundesstrasse: 7, landstrasse: 9, kreisstrasse: 11,
    nebenstrasse: 13, erschliessung: 14, auffahrt: 12,
  };

  function istLink(p) {
    return !!p.is_link || /_link$/.test(p.kind_detail || "");
  }

  return {
    /* Alle Zeichenregeln für eine Kachelquelle. `flavor` ist die Farbtafel
       aus mapstyle.js, `dunkel` sagt, welche der beiden Vorgabetafeln
       einspringt, wo die Farbtafel schweigt. */
    paint: function (flavor, dunkel) {
      var P = window.protomapsL;
      var std = STD[dunkel ? "dark" : "light"];
      function f(schluessel) {
        var wert = flavor && flavor[schluessel];
        return wert == null ? std[schluessel] : wert;
      }
      var linie = function (o) { return new P.LineSymbolizer(o); };
      var flaeche = function (o) { return new P.PolygonSymbolizer(o); };
      // Die Fassung ist so viel breiter als die Füllung – in der Stadt ein
      // klarer Rand, im Überblick ein Hauch.
      var rand = P.exp(1.6, [[6, 0.25], [11, 0.5], [14, 1], [18, 2.4]]);

      // Eine Straßenklasse: Fassung und Füllung aus derselben Breitenkurve.
      // `farbe` und `randfarbe` sind nur bei den Auffahrten nötig – die
      // erben die Farbe der Straße, an der sie hängen.
      function klasse(name, filter, farbe, randfarbe) {
        var breite = P.exp(1.6, BREITEN[name]);
        var gemeinsam = { dataLayer: "roads", minzoom: AB[name], filter: filter };
        // Die Autobahn trägt ihre Fassung als Anteil der Füllung, nicht als
        // festen Zuschlag: Das rote Band bleibt in jeder Stufe so breit, wie
        // es die Karte will – in der Stadtstufe ein deutlicher Rand, im
        // Überblick ein Hauch. Unter den Zuschlag fällt sie nie.
        var anteil = RANDANTEIL[name];
        return {
          fassung: Object.assign({}, gemeinsam, { symbolizer: linie({
            color: randfarbe || f(name + "_rand"), lineCap: "round", lineJoin: "round",
            width: function (z) {
              var b = breite(z);
              return anteil ? Math.max(b * anteil, b + rand(z)) : b + rand(z);
            } }) }),
          fuellung: Object.assign({}, gemeinsam, { symbolizer: linie({
            color: farbe || f(name), width: breite, lineCap: "round", lineJoin: "round" }) }),
        };
      }
      // Die Auffahrt trägt die Farbe ihrer Straßenklasse, nur schmaler.
      function auffahrtsfarbe(nachsatz) {
        return function (z, e) {
          var d = e.props.kind_detail || "";
          if (e.props.kind === "highway" || d.indexOf("motorway") === 0) return f("autobahn" + nachsatz);
          if (d.indexOf("trunk") === 0 || d.indexOf("primary") === 0) return f("bundesstrasse" + nachsatz);
          if (d.indexOf("secondary") === 0) return f("landstrasse" + nachsatz);
          return f("kreisstrasse" + nachsatz);
        };
      }
      // Von unten nach oben: Was später kommt, liegt oben. Auffahrten unter
      // den durchgehenden Straßen, damit die Linie durchläuft.
      var klassen = [
        klasse("erschliessung", function (z, e) { return e.props.kind === "other"; }),
        klasse("nebenstrasse", function (z, e) { return e.props.kind === "minor_road"; }),
        klasse("kreisstrasse", function (z, e) {
          var p = e.props;
          return p.kind === "medium_road"
            || (p.kind === "major_road" && !istLink(p) && p.kind_detail === "tertiary");
        }),
        klasse("auffahrt", function (z, e) {
          var p = e.props;
          return istLink(p) && (p.kind === "major_road" || p.kind === "highway");
        }, auffahrtsfarbe(""), auffahrtsfarbe("_rand")),
        klasse("landstrasse", function (z, e) {
          var p = e.props;
          return p.kind === "major_road" && !istLink(p) && p.kind_detail === "secondary";
        }),
        klasse("bundesstrasse", function (z, e) {
          var p = e.props;
          return p.kind === "major_road" && !istLink(p)
            && (p.kind_detail === "trunk" || p.kind_detail === "primary");
        }),
        klasse("autobahn", function (z, e) {
          return e.props.kind === "highway" && !istLink(e.props);
        }),
      ];

      var eigene = [];
      // Häuser: volle Deckkraft und ein Rand. Vorher lagen sie halb
      // durchsichtig auf dem Land und verschwammen mit ihm.
      eigene.push({ dataLayer: "buildings", symbolizer: flaeche({
        fill: f("buildings"), stroke: f("haus_rand"), width: 0.5 }) });

      // Staatsgrenzen – und gleich darauf das Meer, das die Linien auf See
      // wieder zudeckt (siehe Kopf). Verwaltungsgrenzen bleiben weg: Auf
      // einer Reisekarte zerschneiden sie das Land ohne Nutzen.
      eigene.push({ dataLayer: "boundaries",
        filter: function (z, e) { return e.props.kind === "country"; },
        symbolizer: linie({ color: f("grenze"), dash: [4, 2.5],
          width: P.exp(1.6, [[1, 0.7], [4, 1.1], [6, 1.4], [9, 1.7], [13, 2.2]]) }) });
      eigene.push({ dataLayer: "water",
        filter: function (z, e) { return e.geomType === 3 && e.props.kind === "ocean"; },
        symbolizer: flaeche({ fill: f("water") }) });

      // Start- und Landebahnen, Stege, Wege: unter allen Straßen.
      eigene.push({ dataLayer: "roads", minzoom: 12,
        filter: function (z, e) { return e.props.kind_detail === "runway"; },
        symbolizer: linie({ color: f("runway"), width: P.exp(1.6, [[11, 0], [13, 4], [19, 30]]) }) });
      eigene.push({ dataLayer: "roads", minzoom: 15,
        filter: function (z, e) { return e.props.kind_detail === "taxiway"; },
        symbolizer: linie({ color: f("runway"), width: P.exp(1.6, [[14, 0], [14.5, 1], [16, 6]]) }) });
      eigene.push({ dataLayer: "roads", minzoom: 14,
        filter: function (z, e) { return e.props.kind === "path" && e.props.kind_detail === "pier"; },
        symbolizer: linie({ color: f("pier"), width: P.exp(1.6, [[13, 0], [13.5, 0.5], [21, 16]]) }) });
      // Wege gestrichelt. Bürgersteige, Überwege und Zufahrten bleiben weg:
      // Sie verdoppeln nur die Straße, an der sie liegen, und legten einen
      // zweiten Strichraster über jeden Häuserblock.
      var WEGE = ["footway", "path", "track", "steps", "cycleway", "bridleway", "pedestrian"];
      eigene.push({ dataLayer: "roads", minzoom: 15,
        filter: function (z, e) {
          return e.props.kind === "path" && WEGE.indexOf(e.props.kind_detail) >= 0;
        },
        symbolizer: linie({ color: f("weg"), dash: [2.5, 2],
          width: P.exp(1.6, [[13, 0.5], [15, 0.9], [18, 2]]) }) });

      klassen.forEach(function (k) { eigene.push(k.fassung); });
      klassen.forEach(function (k) { eigene.push(k.fuellung); });

      // ------------------------------------------------- Bahn und Fähre
      // Bahnstrecken: dunkle Linie, helle Schwellen darüber – das Zeichen,
      // das jede deutsche Karte für die Eisenbahn benutzt.
      var bahnBreite = P.exp(1.6, [[6, 0.4], [10, 0.9], [13, 1.6], [15, 2.4], [18, 5]]);
      function fernbahn(e) {
        var p = e.props;
        return p.kind === "rail" && !p.service
          && (p.kind_detail === "rail" || p.kind_detail === "narrow_gauge");
      }
      eigene.push({ dataLayer: "roads", minzoom: 11, filter: function (z, e) { return fernbahn(e); },
        symbolizer: linie({ color: f("bahn"), width: bahnBreite }) });
      eigene.push({ dataLayer: "roads", minzoom: 11, filter: function (z, e) { return fernbahn(e); },
        symbolizer: linie({ color: f("bahn_schwelle"), dash: [1.5, 2.5],
          width: function (z) { return Math.max(0.5, bahnBreite(z) - 1); } }) });
      // Nahverkehr: S-Bahn, U-Bahn, Tram, Einschienenbahn – eine Farbe,
      // gestrichelt, erst in der Stadtstufe.
      eigene.push({ dataLayer: "roads", minzoom: 13, filter: function (z, e) {
          var p = e.props;
          return p.kind === "rail" && !p.service && (p.kind_detail === "light_rail"
            || p.kind_detail === "subway" || p.kind_detail === "tram"
            || p.kind_detail === "monorail");
        },
        symbolizer: linie({ color: f("nahverkehr"), dash: [3, 2],
          width: P.exp(1.6, [[11, 0.6], [14, 1.3], [18, 3.5]]) }) });
      // Seilbahn, Gondel, Standseilbahn: gepunktet.
      eigene.push({ dataLayer: "roads", minzoom: 12, filter: function (z, e) {
          return e.props.kind === "aerialway" || e.props.kind_detail === "funicular";
        },
        symbolizer: linie({ color: f("sonderbahn"), dash: [0.6, 2.6], lineCap: "round",
          width: P.exp(1.6, [[11, 1], [14, 1.6], [18, 3.2]]) }) });
      // Fähren: gepunktet übers Wasser, deshalb NACH dem Meer gezeichnet.
      eigene.push({ dataLayer: "roads", minzoom: 11,
        filter: function (z, e) { return e.props.kind === "ferry"; },
        symbolizer: linie({ color: f("faehre"), dash: [0.1, 2.6], lineCap: "round",
          width: P.exp(1.6, [[11, 1.2], [14, 1.8], [18, 3.4]]) }) });

      // Flächen (Land, Wald) und alles Wasser bleiben bei der Bibliothek;
      // Straßen, Grenzen und Häuser kommen von oben.
      return P.paintRules(flavor).filter(function (r) {
        return r.dataLayer !== "roads" && r.dataLayer !== "boundaries"
          && r.dataLayer !== "buildings";
      }).concat(eigene);
    },
  };
})();
