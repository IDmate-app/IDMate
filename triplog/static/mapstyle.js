/* Farben der eigenen Weltkarte (Vektorkacheln, gezeichnet von protomaps-leaflet).

   Zwei Farbschemata, HELL und DUNKEL, übernommen aus @protomaps/basemaps
   (Flavors "light" und "dark", BSD-3-Clause). Darunter OVERRIDES: Das ist die
   Stelle für eigene Regeln – jede Zeile dort ersetzt den gleichnamigen Wert.

   Aus den Tabellen kommen Flächen und Schrift:
     water, earth, background   Wasser, Land, Hintergrund außerhalb der Daten
     park_a/b, wood_a/b, …      Flächen (a = Fläche, b = Muster/Rand)
     buildings                  Häuser
     railway                    Bahnstrecken der Bibliothek (ohne Wirkung,
                                seit die App die Schienen selbst zeichnet)
     *_label, *_label_halo      Schriftfarbe und Schriftrand der Beschriftungen

   Straßen, Grenzen, Häuserrand und Nahverkehr zeichnet die App dagegen nach
   eigenen Regeln (static/js/maprules.js, dort stehen auch die Vorgabefarben).
   Diese Schlüssel lassen sich unter OVERRIDES setzen:
     autobahn, bundesstrasse, landstrasse, kreisstrasse, nebenstrasse,
     erschliessung, auffahrt    die Straßenklassen im deutschen Schema
     …_rand                     die Fassung der jeweiligen Klasse
     weg                        Feld-, Fuß- und Radwege
     grenze                     Staatsgrenzen. Verwaltungsgrenzen zeichnet die
                                Karte nicht, Grenzen auf See deckt das Meer zu
     bahn, bahn_schwelle        Eisenbahn (Linie und Schwellen darüber)
     nahverkehr                 S-Bahn, U-Bahn, Tram, Einschienenbahn
     sonderbahn                 Seilbahn, Gondel, Standseilbahn
     faehre                     Fährlinien
     haus_rand                  Rand der Häuser

   Ohne Wirkung sind seit 1.65.0 die Straßenschlüssel der Bibliothek
   (highway, major, minor_a/b, link, other, die …_casing, bridges_*,
   tunnel_*, boundaries) – die Tabellen führen sie nur noch mit, weil sie
   eine unveränderte Kopie der Vorlage sind.
   "transparent" blendet eine Ebene aus. Farben als #rrggbb oder rgba(…).

   Wirksam wird eine Änderung beim nächsten Laden der Karte; kein Neubau nötig.
*/
window.RoundtripMapStyle = (function () {
  var LIGHT = {
  background: "#cccccc",
  earth: "#e2dfda",
  park_a: "#cfddd5",
  park_b: "#9cd3b4",
  hospital: "#e4dad9",
  industrial: "#d1dde1",
  school: "#e4ded7",
  wood_a: "#d0ded0",
  wood_b: "#a0d9a0",
  pedestrian: "#e3e0d4",
  scrub_a: "#cedcd7",
  scrub_b: "#99d2bb",
  glacier: "#e7e7e7",
  sand: "#e2e0d7",
  beach: "#e8e4d0",
  aerodrome: "#dadbdf",
  runway: "#e9e9ed",
  water: "#80deea",
  zoo: "#c6dcdc",
  military: "#dcdcdc",

  tunnel_other_casing: "#e0e0e0",
  tunnel_minor_casing: "#e0e0e0",
  tunnel_link_casing: "#e0e0e0",
  tunnel_major_casing: "#e0e0e0",
  tunnel_highway_casing: "#e0e0e0",
  tunnel_other: "#d5d5d5",
  tunnel_minor: "#d5d5d5",
  tunnel_link: "#d5d5d5",
  tunnel_major: "#d5d5d5",
  tunnel_highway: "#d5d5d5",

  pier: "#e0e0e0",
  buildings: "#cccccc",

  minor_service_casing: "#e0e0e0",
  minor_casing: "#e0e0e0",
  link_casing: "#e0e0e0",
  major_casing_late: "#e0e0e0",
  highway_casing_late: "#e0e0e0",
  other: "#ebebeb",
  minor_service: "#ebebeb",
  minor_a: "#ebebeb",
  minor_b: "#ffffff",
  link: "#ffffff",
  major_casing_early: "#e0e0e0",
  major: "#ffffff",
  highway_casing_early: "#e0e0e0",
  highway: "#ffffff",

  railway: "#a7b1b3",
  boundaries: "#adadad",

  bridges_other_casing: "#e0e0e0",
  bridges_minor_casing: "#e0e0e0",
  bridges_link_casing: "#e0e0e0",
  bridges_major_casing: "#e0e0e0",
  bridges_highway_casing: "#e0e0e0",
  bridges_other: "#ebebeb",
  bridges_minor: "#ffffff",
  bridges_link: "#ffffff",
  bridges_major: "#f5f5f5",
  bridges_highway: "#ffffff",

  roads_label_minor: "#91888b",
  roads_label_minor_halo: "#ffffff",
  roads_label_major: "#938a8d",
  roads_label_major_halo: "#ffffff",
  ocean_label: "#728dd4",
  subplace_label: "#8f8f8f",
  subplace_label_halo: "#e0e0e0",
  city_label: "#5c5c5c",
  city_label_halo: "#e0e0e0",
  state_label: "#b3b3b3",
  state_label_halo: "#e0e0e0",
  country_label: "#a3a3a3",

  address_label: "#91888b",
  address_label_halo: "#ffffff",

  pois: {
    blue: "#1A8CBD",
    green: "#20834D",
    lapis: "#315BCF",
    pink: "#EF56BA",
    red: "#F2567A",
    slategray: "#6A5B8F",
    tangerine: "#CB6704",
    turquoise: "#00C3D4",
  },

  landcover: {
    grassland: "rgba(210, 239, 207, 1)",
    barren: "rgba(255, 243, 215, 1)",
    urban_area: "rgba(230, 230, 230, 1)",
    farmland: "rgba(216, 239, 210, 1)",
    glacier: "rgba(255, 255, 255, 1)",
    scrub: "rgba(234, 239, 210, 1)",
    forest: "rgba(196, 231, 210, 1)",
  },
};
  var DARK = {
  background: "#34373d",
  earth: "#1f1f1f",
  park_a: "#1c2421",
  park_b: "#192a24",
  hospital: "#252424",
  industrial: "#222222",
  school: "#262323",
  wood_a: "#202121",
  wood_b: "#202121",
  pedestrian: "#1e1e1e",
  scrub_a: "#222323",
  scrub_b: "#222323",
  glacier: "#1c1c1c",
  sand: "#212123",
  beach: "#28282a",
  aerodrome: "#1e1e1e",
  runway: "#333333",
  water: "#31353f",
  zoo: "#222323",
  military: "#242323",

  tunnel_other_casing: "#141414",
  tunnel_minor_casing: "#141414",
  tunnel_link_casing: "#141414",
  tunnel_major_casing: "#141414",
  tunnel_highway_casing: "#141414",
  tunnel_other: "#292929",
  tunnel_minor: "#292929",
  tunnel_link: "#292929",
  tunnel_major: "#292929",
  tunnel_highway: "#292929",

  pier: "#333333",
  buildings: "#111111",

  minor_service_casing: "#1f1f1f",
  minor_casing: "#1f1f1f",
  link_casing: "#1f1f1f",
  major_casing_late: "#1f1f1f",
  highway_casing_late: "#1f1f1f",
  other: "#333333",
  minor_service: "#333333",
  minor_a: "#3d3d3d",
  minor_b: "#333333",
  link: "#3d3d3d",
  major_casing_early: "#1f1f1f",
  major: "#3d3d3d",
  highway_casing_early: "#1f1f1f",
  highway: "#474747",

  railway: "#000000",
  boundaries: "#5b6374",

  bridges_other_casing: "#2b2b2b",
  bridges_minor_casing: "#1f1f1f",
  bridges_link_casing: "#1f1f1f",
  bridges_major_casing: "#1f1f1f",
  bridges_highway_casing: "#1f1f1f",
  bridges_other: "#333333",
  bridges_minor: "#333333",
  bridges_link: "#3d3d3d",
  bridges_major: "#3d3d3d",
  bridges_highway: "#474747",

  roads_label_minor: "#525252",
  roads_label_minor_halo: "#1f1f1f",
  roads_label_major: "#666666",
  roads_label_major_halo: "#1f1f1f",
  ocean_label: "#717784",
  subplace_label: "#525252",
  subplace_label_halo: "#1f1f1f",
  city_label: "#7a7a7a",
  city_label_halo: "#212121",
  state_label: "#3d3d3d",
  state_label_halo: "#1f1f1f",
  country_label: "#5c5c5c",

  address_label: "#525252",
  address_label_halo: "#1f1f1f",

  pois: {
    blue: "#4299BB",
    green: "#30C573",
    lapis: "#2B5CEA",
    pink: "#EF56BA",
    red: "#F2567A",
    slategray: "#93939F",
    tangerine: "#F19B6E",
    turquoise: "#00C3D4",
  },

  landcover: {
    grassland: "rgba(30, 41, 31, 1)",
    barren: "rgba(38, 38, 36, 1)",
    urban_area: "rgba(28, 28, 28, 1)",
    farmland: "rgba(31, 36, 32, 1)",
    glacier: "rgba(43, 43, 43, 1)",
    scrub: "rgba(34, 36, 30, 1)",
    forest: "rgba(28, 41, 37, 1)",
  },
};

  // ---------------------------------------------------- Eigene Regeln
  // 1.137.0: die Vorlage „Kräftig" aus dem Farbbaukasten (Nr. 112) – Wald
  // und Wiese satter, Wasser klarer, Papier wärmer. Wer eigene Töne will,
  // ändert hier oder in der Kopie im
  // Datenordner (Einrichtung → Weltkarte).
  var OVERRIDES = {
    light: {
      earth: "#f1efe6",
      wood_a: "#a9d39e",
      park_a: "#bfe0b0",
      scrub_a: "#d6e9b2",
      water: "#6cc6e0",
      industrial: "#d7dee3",
      pedestrian: "#ece5d2",
      sand: "#f0e3bf",
      buildings: "#d8cbb6",
      haus_rand: "#ab9c85",
      autobahn: "#ffc542",
      autobahn_rand: "#d3391f",
      bundesstrasse: "#e8553c",
      bundesstrasse_rand: "#a72f1c",
      landstrasse: "#f7d066",
      landstrasse_rand: "#c99b2e",
      kreisstrasse: "#fcefb6",
      kreisstrasse_rand: "#d4bd72",
      nebenstrasse_rand: "#c4b9a5",
      erschliessung: "#f7f3ea",
      erschliessung_rand: "#d6ccb8",
      weg: "#9c8664",
      bahn: "#5b5f66",
      bahn_schwelle: "#f4f1ea",
      grenze: "#9a6fb0",
      city_label: "#2b2b2b",
      city_label_halo: "#f1efe6",
      subplace_label: "#5d5a55",
      subplace_label_halo: "#f1efe6",
      roads_label_major: "#5d554f",
      roads_label_minor: "#6f6862",
      landcover: {
        forest: "#a6cf9c",
        grassland: "#cde6b6",
        farmland: "#e6e2b4",
        scrub: "#dfe6b8",
        urban_area: "#e2ddd3",
        barren: "#f4e7c2",
        glacier: "#ffffff",
      },
    },
    dark: {
      // autobahn: "#8a7a3a",
    },
  };

  return {
    // Soll die Karte dem Dunkelmodus der App folgen? Nein: Die Karte sieht
    // bei Tag und Nacht gleich aus, nur die Oberfläche drumherum wechselt.
    followsTheme: false,
    flavor: function (dark) {
      return Object.assign({}, dark ? DARK : LIGHT, OVERRIDES[dark ? "dark" : "light"]);
    },
  };
})();
