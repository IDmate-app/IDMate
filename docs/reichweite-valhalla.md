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

## Kacheln auf einem starken PC bauen (z. B. ganz Europa)

Der Bau braucht viel CPU, RAM und Platz, der Betrieb danach kaum. Große
Gebiete baut man daher besser auf einem kräftigen Rechner und schiebt nur das
Ergebnis auf den Server.

### Auf dem PC (Windows mit Docker Desktop)

1. Docker Desktop mit WSL2 installieren. WSL2 bekommt standardmäßig nur einen
   Teil des RAM; in `%UserProfile%\.wslconfig` freigeben, z. B.:

   ```ini
   [wsl2]
   memory=24GB
   processors=8
   swap=16GB
   ```

   Danach in PowerShell `wsl --shutdown` und Docker Desktop neu starten.
2. Platz: rund 100 GB frei auf dem Laufwerk mit dem Docker-Speicher (Docker
   Desktop → Settings → Resources → Disk image location).
3. Energieoptionen: Ruhezustand/Standby für die Bauzeit aus.
4. Bauen (PowerShell, `server_threads` = Anzahl Kerne):

   ```powershell
   docker volume create valhalla-build
   docker run -d --name valhalla-build -p 8002:8002 `
     -v valhalla-build:/custom_files `
     -e tile_urls=https://download.geofabrik.de/europe-latest.osm.pbf `
     -e build_admins=True -e build_time_zones=False -e build_elevation=False `
     -e build_tar=True -e use_tiles_ignore_pbf=True -e serve_tiles=True `
     -e server_threads=8 `
     ghcr.io/valhalla/valhalla-scripted:latest
   docker logs -f --tail 50 valhalla-build
   ```

   Ein Docker-Volume statt eines Windows-Ordners: das ist beim Bau um ein
   Vielfaches schneller. Fertig ist er, wenn der Dienst auf Port 8002 lauscht.
5. Grenze für große Reichweiten setzen und neu starten:

   ```powershell
   docker exec valhalla-build sed -i 's/"max_distance_contour": 200/"max_distance_contour": 800/' /custom_files/valhalla.json
   docker restart valhalla-build
   ```

6. Testen (Wien, 300 km), es muss eine Fläche zurückkommen:

   ```powershell
   Invoke-RestMethod -Method Post -Uri http://localhost:8002/isochrone -ContentType 'application/json' `
     -Body '{"locations":[{"lat":48.21,"lon":16.37}],"costing":"auto","contours":[{"distance":300}],"polygons":true}'
   ```

7. Ergebnis herauskopieren (Ordner `C:\valhalla-export` vorher anlegen):

   ```powershell
   docker stop valhalla-build
   docker run --rm -v valhalla-build:/data -v C:\valhalla-export:/out alpine `
     sh -c "cp /data/valhalla.json /data/valhalla_tiles.tar /data/admins.sqlite /out/"
   ```

   Die PBF-Datei (rund 33 GB) wird nicht gebraucht.

### Auf dem Server

1. Container `valhalla` stoppen (IDmate läuft weiter, zeigt den Kreis).
2. Einen normalen Ordner anlegen, z. B. `/volume1/docker/valhalla`, und die
   drei Dateien aus `C:\valhalla-export` hineinkopieren (Netzwerkfreigabe).
3. Im Stack beim Dienst `valhalla` das Volume durch diesen Ordner ersetzen:

   ```yaml
   volumes:
     - /volume1/docker/valhalla:/custom_files
   ```

   `use_tiles_ignore_pbf=True` bleibt gesetzt: vorhandene Kacheln werden
   benutzt, nichts wird neu geladen oder gebaut. `server_threads` reicht
   jetzt mit 2.
4. Stack neu deployen. Nach etwa einer Minute meldet das Log, dass der Dienst
   auf Port 8002 lauscht, und das Dashboard zeigt die Fläche.
5. Das alte Volume `valhalla-data` mit dem abgebrochenen Bau kann weg.

Beide Rechner sollten dieselbe Valhalla-Version benutzen (erste Zeile im
Log, „Valhalla version“). Den Server-Container danach nicht ohne Grund neu
ziehen.
