# Reichweite straßengenau (Valhalla)

Ohne Zusatz zeigt das Dashboard die Reichweite als Kreis um das Auto
(Luftlinie). Mit einem eigenen Routing-Dienst wird daraus die Fläche, die über
Straßen erreichbar ist: Wasser, Fähren und Umwege sind dann berücksichtigt.

Der Dienst läuft als zusätzlicher Container im eigenen Netz. Der Standort des
Autos geht an keinen fremden Server.

## Einrichten

1. `docker-compose.valhalla.yml` dazunehmen (Portainer: Dienst `valhalla`,
   Volume `valhalla-data` und bei `triplog` die Zeile
   `VALHALLA_URL=http://valhalla:8002` in den Stack übernehmen).
2. Welche Länder: `VALHALLA_PBF_URLS` (Leerzeichen-getrennte Geofabrik-Links,
   Standard Deutschland + Dänemark). Für die Reichweite reichen die Länder, in
   denen du fährst; jedes weitere kostet Bauzeit und RAM.
3. Erster Start: Download und Aufbau der Kacheln, 1–3 Stunden, viel RAM
   (Deutschland + Dänemark rund 8 GB). Wird der Container dabei beendet,
   `VALHALLA_THREADS` auf 1 setzen.
4. **Einmal danach:** in `valhalla.json` (im Volume `valhalla-data`) unter
   `service_limits` → `isochrone` den Wert `max_distance_contour` von `200`
   auf `800` setzen und den Container neu starten. Sonst lehnt Valhalla
   Reichweiten über 200 km ab und das Dashboard bleibt beim Kreis.

## Verhalten

- IDmate fragt Valhalla nach der Fläche für die untere und obere Reichweite
  (aus „BMS lernen“, sonst die berechnete Reichweite). Das Ergebnis wird
  zwischengespeichert, bis das Auto sich um mehr als ~2 km bewegt oder die
  Reichweite sich um 10 km ändert.
- Antwortet Valhalla nicht (noch im Aufbau, Grenze nicht erhöht, Standort
  außerhalb der Kartendaten), zeigt das Dashboard weiter den Kreis.
