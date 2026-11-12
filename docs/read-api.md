# Read API for display devices

[← README](../README.md)

`/api/v1` serves vehicle data to display clients – for example an app for Android Auto on a tablet in
the car. It only reads: what the dashboard already shows (latest state) plus the trip and charge lists.

**Off by default.** Until an admin switches it on, every `/api/v1` route answers `404`.

## Switching it on

*Admin → Home Assistant → Read API for display devices*

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
| `GET /api/v1/config` | vehicle (`id`, `name`, `model`), active modules, poll interval drive / parked |
| `GET /api/v1/status` | telegram `status`: latest state |
| `GET /api/v1/trips?limit=n` | telegram `trip`: the last *n* trips (default 10, at most 100) |
| `GET /api/v1/charging?limit=n` | telegram `charge`: the last *n* charges |

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
| `range` | km | `r` |
| `power` | kW | `p` |
| `speed` | km/h | `v` |
| `lat`, `lon` | WGS84 | `la`, `lo` |
| `odometer` | km | `od` |
| `charging`, `dc` | bool | `c`, `dc` |
| `t_ext`, `t_batt` | °C | `et`, `bt` |

Trip items: `id`, `start`, `end`, `from`, `to`, `distance` (km), `soc_start`, `soc_end`, `energy` (kWh),
`consumption` (kWh/100 km). Charge items: `id`, `start`, `end`, `at`, `dc`, `soc_start`, `soc_end`,
`energy` (kWh), `power_max` (kW). Logbook details (purpose, destination, notes) are not served.

## Modules

`config.modules` tells the client which parts to show: `dashboard`, `navi`, `music`, `messenger`. Set per
vehicle next to its access; default `dashboard`. A module that is not listed stays invisible on the device.
