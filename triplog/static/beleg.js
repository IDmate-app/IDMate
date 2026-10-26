/* IDmate – Ladebeleg lesen: Screenshot der Lade-App oder der daraus kopierte
   Text -> Felder einer externen Ladung.

   Gemeinsam für „Externe Ladung erfassen" (charges.html) und „Externe Ladung
   bearbeiten" in den Ladedetails (charge_detail.html) – dort öffnet man die
   automatisch erkannte Ladung und lässt den Beleg über sie laufen. Die Seite
   ruft IDmateBeleg.einrichten() mit ihren Feld-IDs und ihrem Speichern-Aufruf;
   die Texte liegen im Markup (_beleg.html, #beleg-texte). Der Server erkennt
   das Layout (beleg.py); nur ein erkanntes Layout (auto) speichert von selbst,
   ein unbekanntes bleibt ein Vorschlag zum Prüfen. */
(function () {
  "use strict";
  var K = null;
  var T = {};
  try { T = JSON.parse((document.getElementById("beleg-texte") || {}).textContent || "{}"); } catch (e) { T = {}; }
  var farbe = function (name) { return (window.TC && window.TC[name]) || ""; };

  function status(text, farbwert) {
    var el = K && document.getElementById(K.status);
    if (!el) return;
    el.textContent = text || "";
    el.style.color = farbwert || "";
  }
  function setze(id, wert) {
    var el = id && document.getElementById(id);
    if (!el || wert == null || wert === "") return false;
    el.value = String(wert);
    el.dataset.auto = el.value;
    return true;
  }

  async function verarbeiten(quelle) {
    if (!K) return;
    status(T.lese || "…");
    var resp, d;
    try {
      if (typeof quelle === "string") {
        resp = await fetch("/api/charge/external/beleg", { method: "POST",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: quelle }) });
      } else {
        var fd = new FormData();
        fd.append("file", quelle, quelle.name || "beleg.png");
        resp = await fetch("/api/charge/external/beleg", { method: "POST", body: fd });
      }
      d = await resp.json();
    } catch (e) { status((T.fehler || "Fehler") + ": " + e.message, farbe("danger")); return; }
    if (!resp.ok || d.error) { status(d.error || T.fehler || "Fehler", farbe("danger")); return; }
    var f = d.felder || {};
    var F = K.felder || {};
    setze(F.start, f.start ? f.start.replace(" ", "T") : null);
    setze(F.end, f.end ? f.end.replace(" ", "T") : null);
    setze(F.kwh, f.kwh);
    setze(F.cost, f.cost_total);
    if (F.note && f.note) {
      // Die Notiz der erkannten Ladung („Automatisch erkannt (SoC 35%->80%)")
      // bleibt; der Beleg kommt dahinter.
      var n = document.getElementById(F.note);
      if (n) { var alt = n.value.trim(); n.value = alt ? (alt.indexOf(f.note) < 0 ? alt + " · " + f.note : alt) : f.note; }
    }
    if (K.nachLesen) await K.nachLesen(f);
    var fehlt = f.fehlt || [];
    var erkannt = f.schema_name ? (T.schema || "{0}").replace("{0}", f.schema_name) : "";
    if (f.auto) {
      status(erkannt + " – " + (T.speichern || ""));
      await K.speichern(f);
    } else if (!f.schema_name) {
      status((T.unbekannt || "") + (fehlt.length ? " " + (T.fehlt || "{0}").replace("{0}", fehlt.join(", ")) : ""), farbe("warn"));
    } else {
      status(erkannt + " – " + (T.fehlt || "{0}").replace("{0}", fehlt.join(", ")), farbe("warn"));
    }
  }

  function ausEreignis(ev) {
    var cd = ev.clipboardData;
    if (!cd) return false;
    var bild = Array.prototype.slice.call(cd.files || []).filter(function (f) { return f.type.indexOf("image/") === 0; })[0]
      || Array.prototype.slice.call(cd.items || []).filter(function (i) { return i.type.indexOf("image/") === 0; })
           .map(function (i) { return i.getAsFile(); }).filter(Boolean)[0];
    if (bild) { ev.preventDefault(); verarbeiten(bild); return true; }
    var text = cd.getData("text/plain");
    if (text && text.trim()) { ev.preventDefault(); verarbeiten(text); return true; }
    return false;
  }

  // Knopf „Aus Zwischenablage": erst ein Bild, sonst Text. Ohne Erlaubnis
  // (oder ohne die Schnittstelle) bleibt das Einfügen ins Feld.
  window.belegAusZwischenablage = async function () {
    try {
      if (navigator.clipboard && navigator.clipboard.read) {
        var items = await navigator.clipboard.read();
        for (var i = 0; i < items.length; i++) {
          var typ = items[i].types.filter(function (t) { return t.indexOf("image/") === 0; })[0];
          if (typ) { var blob = await items[i].getType(typ); return verarbeiten(new File([blob], "beleg.png", { type: typ })); }
        }
      }
      if (navigator.clipboard && navigator.clipboard.readText) {
        var text = await navigator.clipboard.readText();
        if (text && text.trim()) return verarbeiten(text);
      }
      status(T.leer || "", farbe("warn"));
    } catch (e) {
      status(T.zugriff || "", farbe("warn"));
      var feld = K && document.getElementById(K.textfeld);
      if (feld) feld.focus();
    }
  };
  window.belegDateiGewaehlt = function () {
    var f = this.files && this.files[0];
    if (f) verarbeiten(f);
    this.value = "";
  };

  window.IDmateBeleg = {
    verarbeiten: verarbeiten,
    einrichten: function (konf) {
      K = konf;
      var feld = document.getElementById(konf.textfeld);
      if (feld) feld.addEventListener("paste", ausEreignis);
      document.addEventListener("paste", function (ev) {
        if (konf.aktiv && !konf.aktiv()) return;
        var ziel = ev.target;
        var bildDabei = !!(ev.clipboardData && ev.clipboardData.files && ev.clipboardData.files.length);
        // Normales Einfügen in ein Formularfeld bleibt normal – außer es ist ein Bild.
        if (ziel && ziel.id !== konf.textfeld && /^(INPUT|TEXTAREA)$/.test(ziel.tagName) && !bildDabei) return;
        ausEreignis(ev);
      });
    },
  };
})();
