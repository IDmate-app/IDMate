# Read API for display devices

[← README](../README.md)

`/api/v1` serves vehicle data to display clients – for example an app for Android Auto on a tablet in
the car. It only reads: what the dashboard already shows (latest state) plus the trip and charge lists.

**Off by default.** Until an admin switches it on, every `/api/v1` route answers `404`.

## Switching it on

*Admin → Interfaces → Read API for display devices*

1. Tick **Enable read API**.
2. Enter the address at which the device reaches the server (it goes into the QR code). Expose the API to
   the internet over HTTPS only; inside a VPN or the home network the LAN address is fine.
3. For each vehicle that should be readable: **Create QR code** and scan it with the app.

The QR code is shown once. IDmate keeps only the SHA-256 of the token, so a lost code cannot be displayed
again – create a new one; the previous access of that vehicle stops working at once. **Delete access**
revokes it. Each vehicle has its own access; a token never reads another vehicle.

QR content (JSON):

```json
{"v":1,"t":"idmate-read","url":"https://idmate.example/api/v1","token":"…","vehicle":"id7","name":"ID.7"}
```

## Requests

`Authorization: Bearer <token>` on every request except `/health`. JSON, times in ISO 8601 UTC.

| Endpoint | Returns |
|---|---|
| `GET /api/v1/health` | `{"ok": true, "api": 1}` – no token; `404` while the API is off |
| `GET /api/v1/capabilities` | telegram `caps`: the fields the server has for this vehicle, with their interval in seconds |
| `GET /api/v1/config` | vehicle (`id`, `name`, `model`), what IDmate delivers (`modules`), poll interval drive / parked |
| `GET /api/v1/status` | telegram `status`: latest state |
| `GET /api/v1/trips?limit=n` | telegram `trip`: the last *n* trips (default 10, at most 100) |
| `GET /api/v1/charging?limit=n` | telegram `charge`: the last *n* charges |
| `GET /api/v1/energy-model` | telegram `energy_model`: the vehicle's own consumption by speed |
| `GET /api/v1/rain` | telegram `rain`: rain around the vehicle's range, an hour back and two hours ahead (see below) |

| `GET /api/v1/display` | opens a **display session** and redirects to the dashboard, which then shows only its map (see below) |

`401` wrong or missing token · `404` API off · `429` too many wrong tokens from this address (same
cool-down as the login).

## The dashboard map on a display device

`GET /api/v1/display` with the token of a vehicle sets a session cookie and redirects to `/`. In that
session the dashboard renders as its map alone – the original map with the range circles, the road-based
range area, wind, the car – filling the window. A browser view on the device (the IDmate app draws one onto
the Android Auto surface) therefore shows exactly what the dashboard shows.

A display session is not an account:

- it may only `GET` the dashboard and what its map reads (state, live stream, trail, range area, range
  check, wind, ferries, place pins, map tiles) – everything else, and every request that writes, is `403`;
- it sees only the vehicle of its token;
- it ends when that access is deleted or the read API is switched off.

## Telegrams

One envelope for every source a client may have (stick, OBD dongle, backend, car, tablet):

```json
{"v": 1, "src": "backend", "ts": "2026-11-12T14:03:21Z", "seq": 4711, "type": "status",
 "data": {"soc": 71.5, "range": 312, "power": -12.4, "speed": 87, "lat": 50.1, "lon": 12.3,
          "odometer": 23456.7, "charging": false, "dc": false, "t_ext": 9, "t_batt": 21}}
```

- `ts` – time of the newest value (for lists: time of the answer); `seq` – counts up per vehicle, restarts
  with the server.
- **A field the server has no value for is left out** – never `null`, never `0`. `capabilities` lists
  exactly the fields `status` will contain.

| Field | Unit | Stick field |
|---|---|---|
| `soc` | % | `s` |
| `range` | km | – (IDmate's own figure: energy left ÷ consumption of the last 1000 km, as on the dashboard) |
| `power` | kW | `p` |
| `speed` | km/h | `v` |
| `lat`, `lon` | WGS84 | `la`, `lo` |
| `odometer` | km | `od` |
| `charging`, `dc` | bool | `c`, `dc` (true only while the newest telegram carries the bit) |
| `t_ext`, `t_batt` | °C | `et`, `bt` |

Trip items: `id`, `start`, `end`, `from`, `to`, `distance` (km), `soc_start`, `soc_end`, `energy` (kWh),
`consumption` (kWh/100 km). Charge items: `id`, `start`, `end`, `at`, `dc`, `soc_start`, `soc_end`,
`energy` (kWh), `energy_battery` (kWh), `power_avg` (kW). Logbook details (purpose, destination, notes)
are not served.

The charges are those of the charge overview (wallbox meter, receipts, imports, detected external
charges), newest first; the first item is the last charge. `energy` is the grid side – what the meter
or the receipt says, the figure the overview lists –, `energy_battery` what reached the battery
(grid × charging efficiency). `dc` is false for a wallbox charge and, for an external one, follows
its average power (above 22 kW); it is left out when no power is known.

## Energy model

`GET /api/v1/energy-model` gives the consumption model IDmate learned for this vehicle – the one the range
on the dashboard and the range area on the map are computed with. It is fitted on the trips of the last
year (one trip = one point: average speed, kWh/100 km, outside temperature). Fields the server does not
know are left out; while the car has fewer than 20 usable trips there is no `consumption`.

| Field | Unit | Meaning |
|---|---|---|
| `consumption` | list of `{"v": km/h, "kwh100": kWh/100 km}` | consumption of the warm car by speed, 30 to 160 km/h in steps of 10, at `t_ext`, heating included |
| `t_ext` | °C | outside temperature `consumption` holds for: the car's sensor on the road; standing, the forecast at the car's place |
| `capacity` | kWh | usable battery capacity as IDmate learned it from the charges |
| `start` | kWh | energy of one start after standing long (measured on short trips) |
| `recent` | `{"hours", "distance" km, "consumption" kWh/100 km, "speed" km/h}` | what the trips ended in the last 6 hours add up to; speed over the time driven |
| `average` | same, with `"days"` | the same over the last 28 days |
| `basis` | `{"trips", "distance" km, "speed_max" km/h}` | what the model rests on; `speed_max` is the fastest trip average measured, above it the curve is extrapolated |
| `rain` | list of `{"level", "kwh100", "trips"}` | extra consumption in rain per grade of the rain raster (below), learned from the vehicle's own trips in rain against its dry trips; a grade appears once at least 8 trips (150 km) were driven in it. Add `kwh100` to `consumption` where `/rain` shows that grade on the way |

`v` is the average speed actually driven over a stretch, not the figure on the cruise control: the model
learns from trip averages, and a motorway trip at 130 averages about 110. `recent` and `average` come from
finished trips, so a trip in progress is not in them yet. A charging curve (`charge_curve`) is not served:
IDmate does not evaluate the charging power by state of charge so far.

```json
{"v": 1, "src": "backend", "ts": "2026-11-14T09:12:40Z", "seq": 7, "type": "energy_model",
 "data": {"capacity": 81.4, "t_ext": 5.0, "start": 0.3,
          "consumption": [{"v": 30, "kwh100": 20.0}, {"v": 100, "kwh100": 24.3}, {"v": 130, "kwh100": 27.6}],
          "recent": {"hours": 6, "distance": 120.0, "consumption": 21.7, "speed": 80},
          "average": {"days": 28, "distance": 1250.0, "consumption": 19.8, "speed": 55},
          "basis": {"trips": 240, "distance": 15200, "speed_max": 115}}}
```

## Rain

`GET /api/v1/rain` gives the rain layer of the dashboard map as data: the same area as the wind arrows
(the vehicle's range plus 50 km), the same raster the map draws and the range uses. Two radar sources
are folded into it – the Deutscher Wetterdienst (German composite, every 5 minutes, with a two-hour
nowcast) and, where that has no data, RainViewer (worldwide, the last two hours, no nowcast). Which source
a cell came from makes no difference.

| Field | Meaning |
|---|---|
| `cell_km` | size of a raster cell in km (5). Rows every `cell_km` in latitude, columns every `cell_km` along each row – fixed on the globe |
| `radius` | km around the vehicle the raster covers |
| `levels` | meaning of the grades: `1` light (0.1–1 mm/h), `2` moderate (1–5 mm/h), `3` heavy (over 5 mm/h) |
| `complete` | `false` while only the past and now are there and the forecast is still being fetched (a few seconds) – ask again |
| `frames` | one per time step (15 min), oldest first |
| `frames[].t` | time of the radar picture, UTC |
| `frames[].kind` | `past`, `now` (the newest measured picture) or `forecast` |
| `frames[].src` | sources in this frame: `dwd`, `rainviewer` |
| `frames[].cells` | cells with rain only, as `[lat, lon, level]` (cell centre); dry cells are left out |

`frames` is empty while the rain layer is off on the server, the vehicle has no range area yet, or the
first raster is still being worked out (a few seconds – ask again). Forecast frames only cover the area
of the German composite.

**How to use it in an app**

1. Get the token as for every `/api/v1` route (QR code from *Admin → Interfaces*), send it as
   `Authorization: Bearer <token>`.
2. Ask `GET /api/v1/rain`. The raster changes at most every 15 minutes: ask every 5 minutes at most
   (the server keeps the result; asking more often costs nothing but gains nothing). The first answer
   usually comes at once with the past and now (`complete: false`): show it and ask again after
   5 seconds for the forecast. While `frames` is empty, ask again after 30 seconds.
3. Draw each cell as a rectangle around its centre: `cell_km / 111.2` degrees high and
   `cell_km / (111.2 · cos(lat))` degrees wide. Colours used on the IDmate map: grade 1 `#9ecae1`,
   2 `#4292c6`, 3 `#08519c`, about 45 % opaque.
4. Show the frame with `kind = "now"` by default; the others for a time slider or an animation (past
   first, then forecast). Name the sources listed in `src` (see below).
5. For a range along a route: take the grade of the cell and frame at the place and time the car gets
   there and add the `rain` extra of `GET /api/v1/energy-model` for that grade. Without a learned
   extra, rain does not change the range.

```sh
curl -H "Authorization: Bearer $TOKEN" https://idmate.example/api/v1/rain
```

The server asks the radar services itself; an app never talks to them. Terms the server keeps: the
Deutscher Wetterdienst's open data (GeoNutzV, source named), RainViewer's free API (personal,
non-commercial, attribution, cached tiles). A client that shows the data has to name the sources
the same way: "Deutscher Wetterdienst" and "RainViewer".

```json
{"v": 1, "src": "backend", "ts": "2026-11-17T23:41:00Z", "seq": 12, "type": "rain",
 "data": {"cell_km": 5.0, "radius": 350, "complete": true,
          "levels": {"1": "light, 0.1–1 mm/h", "2": "moderate, 1–5 mm/h", "3": "heavy, over 5 mm/h"},
          "frames": [{"t": "2026-11-17T23:40:00Z", "kind": "now", "src": ["dwd", "rainviewer"],
                      "cells": [[53.5162, 9.9848, 1], [53.5612, 10.0606, 2]]}]}}
```

Radar data: Deutscher Wetterdienst (dwd.de, GeoNutzV), RainViewer (rainviewer.com).

## Modules

`config.modules` names what IDmate contributes: `dashboard`, the vehicle data above. Navigation, music and
messenger are parts of the app itself; IDmate neither delivers nor switches them.
