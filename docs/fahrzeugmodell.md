# Fahrzeugmodell: Bestandsaufnahme und Plan

Ziel: Alle Fahrzeug-Berechnungen an einer Stelle, mit einer Definition je
Größe. Darauf aufbauend ein lernendes Modell und später ein Tab
„Fahrzeug-Analyse“ mit einer Art Zertifikat.

Stand: 1.2.227. Der Diagnose-Export (Verwaltung → Diagnose) liefert die
Rohdaten, die abgeleiteten Werte und die Parameter, an denen man die Punkte
unten im echten Betrieb nachprüfen kann.

## 1. Was heute wo berechnet wird

| Größe | Wo (Hauptstelle) | Rechnung |
|---|---|---|
| Akkukapazität | `app.py` `get_bat_kwh`, `_capacity_points`, `_capacity_trend` | je Ladung `E_Akku / ΔSoC`, Theil-Sen-Trend über die Zeit, begrenzt auf Sollwert + 5 % |
| Ladewirkungsgrad | Einstellung `charge_grid_efficiency` (Vorgabe 0,90), `calibrate-efficiency` | ein Wert für AC und DC, für alle Fahrzeuge |
| SoH | `analysis.html` (im Browser) | aus Influx `ca` und getrennt aus Ladungen mit fest `/1,07` |
| Fahrt-Energie | `detector.py` `build_trip` | `ΔSoC × Kapazität`, Kapazität aus Influx `ca` oder Trend |
| Fahrt-Verbrauch | `detector.py`, `_recompute_all_consumption`, Zusammenführen, Teilen, Import, Lücken-Editor | `Energie / km × 100`, Mindeststrecke je Stelle 0, 1 oder 10 km |
| „Ø real“ | `app.py` `_real_consumption` | Energiebilanz zwischen Ladebeginnen, gemessene kWh × Wirkungsgrad |
| Monatsverbrauch Druck | `charges.py` `_interval_walk` | Zählerstand-kWh mit SoC-Verhältnis-Korrektur, ohne Kapazität |
| Dashboard Ø und Reichweite | `app.py` `vehicle_state` | letzte 1000 km, 10 % oben und unten weg, Kapazität = Sollwert |
| Analyse Ø | `analysis.html` `kmWeightedAvg` | Summe Fahrt-Energie / Summe km, ohne Ausreißerfilter |
| Ladezyklen | `app.py` `/api/analysis` | Influx-Zähler `kw` oder `ΔSoC × Sollwert / Wirkungsgrad` |
| Reise | `journey_detail.html` | Summe Fahrt-Energie / Summe km |
| Ladung kWh, Kosten, Dauer | `_rebuild_charge_sessions_impl`, Bearbeiten, externe Ladung | drei Varianten für Dauer und Ø kW |

## 2. Widersprüche

1. **Fünf Quellen für die Kapazität:** Trend zum Zeitpunkt, Sollwert ohne
   Zeit (Dashboard, Analyse, CSV-Import), eigene Sollwert-Abfrage
   (Zusammenführen, Teilen), Influx `ca` (Detektor, „zwischen Ladungen“,
   SoH), fest 86,5 kWh (Debug). Die gespeicherte Fahrt-Energie hängt davon ab,
   welcher Weg lief.
2. **Drei Wirkungsgrade:** die Einstellung, fest 1,07 beim SoH aus
   Ladungen, keiner beim Influx-Zähler.
3. **Sechs Monats- oder Ø-Verbräuche** mit unterschiedlicher Energiequelle,
   Mindeststrecke und Ausreißerregel.
4. **`charge_sessions.total_kwh` mischt Einheiten:** Wallbox (Netz), Beleg
   (Netz), TeslaMate (Akku), SoC-Schätzung (Akku). Summen addieren das ohne
   Unterschied.
5. **Strecke:** Druck nutzt km-Stand-Differenzen, Analyse und Reisen die
   Summe der Fahrten, „Ø real“ `charge_sessions.distance`.
6. **Ladeerkennung:** SoC-Sprung ab 3 % (externe Ladungen), 5 % (Detektor,
   Ladezyklen, zwischen Ladungen).
7. **Rechnungen im Browser**, die der Server besitzen sollte: SoH und
   Degradation, Reichweite, Lücken-Editor-Prüfung (eigene Kopie der
   Grenzwerte), Reise-Summen, Kosten je 100 km.
8. **Pauschalpreis „Tarif am Datum, sonst 0,34“** steht fünfmal im Code.

### Behoben in 1.2.198

- **Rückkopplung im Kapazitätstrend:** automatisch erkannte externe Ladungen
  speichern `ΔSoC × Trend-Kapazität` als kWh. Der Trend nahm sie als Messung,
  multipliziert mit dem Wirkungsgrad, und zog sich so bei jeder Runde nach
  unten. Jetzt zählen nur gemessene Energien: Wallbox, Beleg mit Preis,
  TeslaMate.
- **TeslaMate-Kennung gelöscht:** „Neu berechnen“ setzte die Nummer aller
  externen Ladungen auf leer, auch das `TM-…`. Danach wurde deren
  akkuseitige Energie noch einmal mit dem Wirkungsgrad multipliziert.
  Migration 63 stellt die Kennung anhand der Import-Notiz wieder her.

### Umgesetzt in 1.2.200 (Schritt 1 des Moduls)

- `fahrzeugmodell.py` mit Akku-Abschnitten (`vehicle_batteries`, Migration 66):
  die Kapazität wird je Akku gerechnet, nie über einen Tausch hinweg.
  Mit genug Punkten ein Trend, sonst der Mittelwert, ohne Punkte der
  Sollwert des neuen Akkus. Ein bleibender Sprung nach oben wird als
  vermuteter Tausch gemeldet (Diagnose und Kapazitäts-Fenster).
- Ladewirkungsgrad je Fahrzeug (`vehicles.charge_efficiency`), messbar gegen
  die BMS-Kapazität (Influx `ca`); ohne eigenen Wert gilt der globale.
- Kapazität zum Zeitpunkt statt Sollwert jetzt auch in Dashboard-Reichweite,
  Analyse (Ladezyklen, Reichweite), Reichweiten-Verlauf, Zusammenführen,
  Teilen und CSV-Import.

### Erkenntnisse aus dem Langzeit-Export (Oktober 2026), umgesetzt in 1.2.205

- Der VW-Zähler `kw` zählt die Energie **in den Akku**: Laden (0,93–0,95 ×
  Wallbox) und Rekuperation (ID.7 ≈ 3,3 kWh/100 km, ≈ 15 % des
  Verbrauchs). Er läuft bei 6553,6 über (u16/10). Die Ladezyklen der
  Analyse haben ihn als Netz-kWh gezählt (7 % zu wenig) und den Überlauf
  nicht beachtet; behoben.
- Wirkungsgrad gegen die BMS-Kapazität über die ganze Historie: AC 0,949
  (68 Ladungen), DC 0,972 (28 Ladungen). Der Kapazitätstrend lag 2–4 % unter
  dem BMS-Wert, weil DC-Belege mit dem AC-Wert gerechnet wurden; mit beiden
  Werten trifft er ihn (84,4 zu 84,3 kWh). Der Wirkungsgrad wird jetzt je
  Fahrzeug für AC und DC getrennt geführt (Migration 67).

### Eigene Rechnung statt BMS (1.2.207)

Die BMS-Kapazität („ca“) ist eine Schätzung des Fahrzeugs und wird nur noch
zum Vergleich gezeigt. Gerechnet wird mit dem Energiezähler `kw`: beim
Parken geht die gezählte Energie in den Akku, geteilt durch den SoC-Hub
ergibt die Kapazität, ohne BMS-Wert und ohne angenommenen Wirkungsgrad. Der
Wirkungsgrad kommt aus demselben Zähler: Zähler-kWh ÷ Wallbox-kWh (AC) bzw.
÷ Beleg-kWh (DC). ID.7 aus dem Export: 84,5 kWh (94 Ladungen, flach), AC
0,933 (61, Mitte 50 % 0,930–0,942), DC 0,995 (39). Fahrzeuge ohne Zähler
(Model 3) rechnen weiter über die Ladungen.

### Widersprüche abgearbeitet (1.2.219–1.2.223)

| # | Stand |
|---|---|
| 1 Kapazität | Eine Quelle: eigene Messung je Akku (`get_bat_kwh` mit Zeit, `capacity_at` für den Detektor). Detektor, Rescan, Debug-Erkennung und „zwischen Ladungen“ nutzen sie; BMS `ca` nur noch, wenn es keine eigene gibt, und als Vergleich. Rückfall 86,5 kWh = `fm.DEFAULT_BAT_KWH`. |
| 2 Wirkungsgrad | Je Fahrzeug AC/DC (gemessen oder fest). Die feste 1,07 der Analyse ist weg: die SoH-Kurve zeigt die Punkte des Modells, die zweite Kurve die BMS-Angabe als Vergleich. |
| 3 Verbrauch | Eine Regel (`fm.MIN_CONS_KM` = 10, `fm.trip_consumption`, `fm.avg_consumption`): Fahrt-Verbrauch und alle Mittelwerte nur aus Fahrten ab 10 km, km-gewichtet. Detektor, Neuberechnung, Zusammenführen, Teilen, Import, Lücken-Editor, Dashboard (mit 10 % Trimmung für die Reichweite), Analyse, Reise, Bericht. |
| 4 Einheiten | Energiebuch `fm.session_energy`: je Ladung Netz- und Akku-kWh mit Quelle (Wallbox, Beleg, TeslaMate mit `kwh_used`, Schätzung). Summen in Analyse, Bericht, Reise und „Ø real“ auf der Netzseite. Kosten bleiben beim Modell des Drucks. |
| 5 Strecke | Verbrauchsmittel nur über Fahrten mit Energie (Reise zählte vorher alle km). Druck bleibt bei km-Ständen, „Ø real“ bei `distance` (beides Energiebilanzen zwischen Ladungen). |
| 6 Ladeerkennung | Eine Schwelle `config.SOC_JUMP_MIN` (5 %) für Detektor, externe Ladungen, Ladezyklen, „zwischen Ladungen“. |
| 7 Browser | SoH und Degradation vom Server; Lücken-Editor bekommt die Grenzen aus `trip_check`; Analyse-Mittel mit derselben Regel wie der Server. Offen: Monatsbalken und Reichweite der Analyse rechnen weiter im Browser, mit der gleichen Regel. |
| 8 Pauschale | `fm.flat_rates` / `fm.flat_rate`: Tarif am Tag, davor der erste, ohne Tag der neueste, ohne Tarif 0,34. |

### Streuung der Kapazitätspunkte (1.2.225)

Analyse am Export: Beim ID.7 passen Fahrzeugzähler und Wallbox auf 0,933
(Streuung 1,7 %), die Energie stimmt. Die Streuung der Punkte (±2–3 kWh)
kommt aus dem angezeigten SoC: etwa 1–1,5 Punkte Unschärfe je Ende und ein
systematischer Anteil, weil ein Prozent nicht überall gleich viel Energie
hält (20–40 %: 0,81 kWh, 80–100 %: 0,92 kWh). Ladungen 20→80 ergaben ≈ 81,
60→100 % 87–98 kWh. Die TeslaMate-Linie des Model 3 ist glatt, weil Energie
und SoC dort beide vom BMS kommen (in sich stimmig, keine unabhängige
Messung).

- `fm.soc_profile`: lernt aus den eigenen Ladungen kWh je % in fünf
  SoC-Bereichen (robust, Prior Richtung gleichmäßig), relativ zum
  Kapazitätsverlauf der ersten Anpassung; `fm.apply_soc_profile` gewichtet
  den SoC-Hub jeder Ladung damit, danach wird neu angepasst. ID.7: Streuung
  12 Monate 2,27 → 1,94 kWh; die Kapazität gilt jetzt für 0–100 % (85,5
  statt 84,5 kWh, vorher zu niedrig durch die vielen Ladungen unten).
- `fm.rolling_median`: Median der letzten 10 Ladungen je Akku als Linie in
  Bericht, Analyse und Kapazitäts-Fenster.

### DC- und AC-Punkte (1.2.227)

Nach dem SoC-Profil streuten Heimladungen (n 53, ±2,0 kWh, 15 h Standzeit)
mehr als DC-Stopps (n 40, ±1,4 kWh, 0,4 h) und lagen 1,3 kWh tiefer. Der
Zähler (wie der DC-Beleg) misst an den Akkuklemmen; bei 300+ A wird ein Teil
in den Zellen zu Wärme statt gespeichert, an der Wallbox kaum. DC-Punkte
lesen deshalb etwas mehr Kapazität. Der SoC aus den HA-Readings am Ladeende
war schlechter (ganze Prozent, ±2,9 statt ±2,3 kWh).

- Zählerladungen gelten als DC, wenn kWh ÷ Standzeit > 22 kW; Ladungen aus
  dem Ladelog über extern + `is_dc`.
- `fm.dc_offset`: Median-Abstand DC − AC um die angepasste Linie, nur mit je
  ≥ 10 Punkten und wenn er mehr als zwei Standardfehler beträgt (höchstens
  6 %). `fm.apply_dc_offset` rechnet DC-Energie und -Kapazität auf
  AC-Niveau, danach SoC-Profil und Anpassung erneut.
- ID.7: +1,54 %; Streuung 12 Monate 1,94 → 1,68 kWh; Kapazität 84,1 kWh,
  Trend −1,2 kWh/Jahr (vorher hob der DC-reiche September das Ende an);
  eigene Rechnung und BMS jetzt 0,3 % auseinander. Model 3: zu wenige
  DC-Punkte, keine Korrektur.

### Puffer-Wächter und BMS-Rücksetzer (1.2.226)

Der BMS-Wert des ID.7 sinkt über Wochen von 86,4 auf 83–84 kWh und springt
nach einer Neukalibrierung (z. B. Tiefentladung 08.10.) wieder genau auf
86,4 — mehr meldet er nie. `fm.bms_resets` zählt die Sprünge zur Obergrenze.

Gibt ein Auto verborgenen Puffer frei, bleibt die nutzbare Energie zwischen
angezeigten 0 und 100 % gleich; die eigene Messung sieht das nicht. Die
Ruhespannung bei gleichem angezeigtem SoC schon: oben (70–90 %, auf 80 %
gerechnet, ≈ 1 V je %) stiege sie, unten (20–40 %, auf 30 %, ≈ 0,5 V je %)
fiele sie. `fm.buffer_watch` rechnet jeden Ruhemoment (steht, Strom unter
3 A, 10-min-Mittel aus Influx) über die Steigung der Spannungskurve auf den
Bandmittelpunkt und über einen Temperaturbeiwert auf 20 °C, je Monat der
Median. Verschiebung erste → letzte Monate in % SoC; ab 1 % nach außen ein
Hinweis, unter 0,5 % über ≥ 3 Monate „kein Hinweis“. Abfrage in
`blueprints/model.py` (`buffer_view`, 6 h Cache), Anzeige im Bericht und
im Kapazitäts-Fenster, im Diagnose-Export unter model.json.

## 3. Zielbild: ein Modul `fahrzeugmodell.py`

Eine Stelle, die alle Seiten fragen. Keine Rechnung mehr in Templates.

- **Energiebuch je Fahrzeug:** jede Ladung mit `E_netz`, `E_akku`, Quelle
  (Zähler, Beleg, TeslaMate, Schätzung) und Güte. Summen nur über eine
  Einheit.
- **Wirkungsgrad je Fahrzeug, AC und DC getrennt**, aus den Ladungen mit
  Zähler und SoC-Paar kalibriert.
- **Kapazität:** ein Schätzer über gemessene Punkte, Influx `ca` als
  zweite, getrennt ausgewiesene Beobachtung. SoH = Kapazität / Sollwert,
  mit Streuung und Zahl der Punkte.
- **Fahrt:** eine Funktion `energie(fahrt)` = `ΔSoC × Kapazität(Zeitpunkt)`
  mit Quelle; ein Verbrauchsbegriff mit einer Mindeststrecke.
- **Verbrauch:** genau zwei Größen, „akkuseitig“ und „netzseitig (mit
  Ladeverlusten)“, je Zeitraum aus dem Energiebuch und den km-Ständen.
- **Parameter** an einer Stelle (Grenzwerte, Mindeststrecken,
  Ladeerkennung), im Diagnose-Export als `model.json`.
- Jede Ausgabe mit **Vertrauensangabe** (n, Spanne), damit ein Zertifikat
  ehrlich sagen kann, wie sicher ein Wert ist.

## 4. Lernendes Modell

Was es sinnvoll leisten kann:

- **Verbrauchsmodell** je Fahrzeug: erwarteter Verbrauch aus
  Geschwindigkeit, Außentemperatur, Höhenmetern, Jahreszeit (Influx `v`,
  `et`, `al`). Die Abweichung vom Erwarteten zeigt Messfehler und
  schleichende Veränderungen. Daraus ein **normierter Verbrauch** (z. B. bei
  20 °C und 80 km/h), vergleichbar über Jahreszeiten.
- **Kapazität aus Ladekurven:** kWh je % in mittleren SoC-Bereichen,
  robuster als nur Start und Ende.
- **Ausreißer-Erkennung** für die Diagnose statt fester Grenzen 8–40.

Datenlage: ein Fahrzeug liefert einige tausend Fahrten. Das reicht für
erklärbare Modelle (Regression, Gradient Boosting), nicht für ein tiefes
Netz. Training auf dem Server mit scikit-learn, Modell als Datei je Fahrzeug,
Parameter im Diagnose-Export.

Voraussetzung: erst die Rechnungen vereinheitlichen (Abschnitt 3). Ein Modell
auf widersprüchlichen Zielwerten lernt die Widersprüche mit.

## 5. Fahrzeug-Analyse und Zertifikat

Umgesetzt in 1.2.209 als Tab „Fahrzeug“ der Analyse (`/api/vehicle-report`,
`blueprints/report.py`): Akku (SoH aus eigener Rechnung mit Streuung, Trend,
Quelle, BMS-Vergleich, Akkutausch und Verdacht), Verbrauch (akku-/netzseitig
gesamt und 12 Monate, je Jahreszeit, Rekuperation), Laden (Zuhause/extern,
AC/DC, Wirkungsgrade mit Herkunft, SoC-Fenster, Ø Preis), Fahrleistung,
Datenqualität; Fußzeile mit Datum, Version und Prüfcode (SHA-256 des
Inhalts). PDF über den PDF-Knopf.

1.2.212: Verbrauch je Monat (Balken) mit mittlerer Außentemperatur (Linie,
Influx „et“) und eine erste Normierung: km-gewichtete Gerade Verbrauch über
Temperatur aus mindestens 6 Monaten → Verbrauch bei 20 °C und Mehrverbrauch
je 10 °C kälter. Offen: Normierung auch nach Geschwindigkeit (je Fahrt),
Datenvollständigkeit der Telemetrie.

1.2.213: IDmate-Stempel mit Datengenauigkeit (Note A–D, 0–100 %). Vier
Teile, gewichtet: SoH-Messung 35 % (Streuung der Messungen von 12 Monaten,
Interquartilsabstand 0 → 100 %, ab 10 Punkten → 0; unter 10 Messungen
anteilig), Energie erfasst 25 % (Fahrten mit Energie), km lückenlos 25 %
(1 − 10 × offene Lücken-km ÷ km), ohne Befunde 15 % (−10 % je Fehler, −2 %
je Hinweis der Diagnose). Der SoH trägt ± die halbe Streuung.

1.2.214: statt Siegel und Note ein schlichtes Kästchen „IDmate check ·
80 % · Datenkontinuität“ (gestrichelt, bewusst kein echter Stempel), ohne
Bewertung A–D.

1.2.215: Ladezähler in der Nacht der Zeitumstellung. Readings tragen
Ortszeit ohne Offset, 02:00–02:59 kommt Ende Oktober zweimal vor. Nach Text
sortiert nahm die Ladung ihren ersten Zählerstand aus dem zweiten Durchlauf
(25.10.2026: +8,05 / −8,05 kWh, Druck gesperrt). `fm.order_readings` ordnet
die doppelte Stunde nach dem Zähler (der nur wächst), ohne Zähler nach
created_at (UTC); Sessions, Lücken-Tab, Ladedetail und SoC-Kurve nutzen es.
Migration 69 korrigiert gespeicherte Ladungen (nicht abgerechnete).

Eine Seite und ein PDF je Fahrzeug:

- SoH mit Spanne und Trend je Jahr, Zahl der Messpunkte
- normierter Verbrauch, Verbrauch nach Jahreszeit
- Ladeverhalten: AC/DC-Anteil, übliches SoC-Fenster, Ladeverluste
- km, Alter, Datenvollständigkeit (Telemetrie-Abdeckung, offene Lücken)
- Prüfdatum, Version des Modells, Prüfsumme

## 6. Reihenfolge

1. Diagnose-Export im echten Betrieb ziehen und die Widersprüche an den
   echten Daten beziffern.
2. `fahrzeugmodell.py` mit Energiebuch, Kapazität, Wirkungsgrad, Verbrauch;
   alte Stellen nacheinander darauf umstellen, je Schritt mit Test, der alt
   und neu vergleicht.
3. Browser-Rechnungen auf Server-Endpunkte umstellen.
4. Verbrauchsmodell (lernend) und normierter Verbrauch.
5. Tab „Fahrzeug-Analyse“ mit Zertifikat-PDF.
