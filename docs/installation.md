# Installation

[← README](../README.md)

IDmate runs as a Docker stack: **Triplog** (the web app), **InfluxDB** and **Mosquitto**. Images come from `ghcr.io`, nothing is built locally.

## Docker Compose

```bash
git clone https://github.com/IDmate-app/IDMate.git
cd IDMate
cp .env.example .env
nano .env
docker compose up -d
```

- Triplog: `http://your-host:3004`
- InfluxDB: `http://your-host:3001`

> Set `INFLUX_TOKEN` **before** the first start. Changing it later means resetting the InfluxDB volume.

Custom volume paths (e.g. Synology): copy `docker-compose.yml` to `docker-compose.local.yml`, change the volumes, start with `docker compose -f docker-compose.local.yml up -d`.

## Portainer

1. **Stacks → Add stack → Web editor**, name `idmate`
2. Paste [docker-compose.yml](../docker-compose.yml)
3. **Environment variables → Advanced mode**, paste and adjust:

```env
INFLUX_PORT=3001
INFLUX_USER=admin
INFLUX_PASS=change-me
INFLUX_ORG=home
INFLUX_BUCKET=can-scan
INFLUX_TOKEN=change-me-to-a-long-random-token
INFLUX_DEVICE=id7
TRIPLOG_PORT=3004
SECRET_KEY=
LANGUAGE=EN
TZ=Europe/Berlin
MQTT_PORT=3005
MQTT_DATA_TOPIC=tele/+/data
MQTT_DOMAIN=localhost
MQTT_TLS=0
MQTT_AES_KEY=
CHARGE_WEBHOOK_TOKEN=
```

4. Deploy the stack.

## First start

There are no default users. Open `http://your-host:3004/setup`; the wizard creates the admin account (password, optional 2FA). Passkeys can be added later in the profile.

## Keys and tokens

Generate them with a real random source — never ask a chatbot for a "random key":

```bash
openssl rand -hex 32        # MQTT_AES_KEY, SECRET_KEY
openssl rand -base64 32     # INFLUX_TOKEN, CHARGE_WEBHOOK_TOKEN
```

`MQTT_AES_KEY` must be the same in the IDTelemetry firmware.

## Environment variables

| Variable | Meaning | Default |
|---|---|---|
| `INFLUX_PORT` | InfluxDB port | 3001 |
| `INFLUX_USER` / `INFLUX_PASS` | InfluxDB admin | — |
| `INFLUX_ORG` / `INFLUX_BUCKET` | InfluxDB organisation / bucket | home / can-scan |
| `INFLUX_TOKEN` | InfluxDB API token | — |
| `INFLUX_DEVICE` | Default device tag | id7 |
| `TRIPLOG_PORT` | Web app port | 3004 |
| `SECRET_KEY` | Session secret (generated if empty) | — |
| `LANGUAGE` | Server default `DE` / `EN` | DE |
| `TZ` | Time zone of trips and charges (IANA, e.g. `Europe/Helsinki`) | Europe/Berlin |
| `MQTT_PORT` | Mosquitto port | 3005 |
| `MQTT_DATA_TOPIC` | Telemetry topic | `tele/+/data` |
| `MQTT_AES_KEY` | 32-byte hex key, same as the stick | — |
| `MQTT_TLS` / `MQTT_DOMAIN` | TLS to the broker / cert host name | 0 / localhost |
| `CHARGE_WEBHOOK_TOKEN` | Token for the Home Assistant webhook | empty = no auth |
| `ROUNDTRIP_URL` | Use a [Roundtrip](https://github.com/TheInGoF/roundtrip) world map instead of public tiles | empty |
| `ENABLE_DEBUG` | Debug routes | 1 |

## Updates

| Tag | Use |
|---|---|
| `1` | recommended |
| `1.2` | patches only |
| `1.2.3` | exact version |
| `latest` | newest release |
| `beta` | test builds — newer, less tested |

```bash
docker compose pull && docker compose up -d
```

Database migrations run by themselves on start. Images: `linux/amd64`, `linux/arm64`.

## Backup and restore

Volumes: `triplog-data`, `influxdb-data`, `influxdb-config`, `mosquitto-data`, `mosquitto-certs`.

```bash
mkdir -p backup && cd backup
for v in triplog-data influxdb-data influxdb-config mosquitto-data mosquitto-certs; do
  docker run --rm -v idmate_${v}:/src -v "$PWD":/dst alpine tar czf /dst/${v}-$(date +%F).tar.gz -C /src .
done
```

Restore:

```bash
docker compose down
for v in triplog-data influxdb-data influxdb-config mosquitto-data mosquitto-certs; do
  docker volume create idmate_${v}
  docker run --rm -v idmate_${v}:/dst -v "$PWD":/src alpine tar xzf /src/${v}-YYYY-MM-DD.tar.gz -C /dst
done
docker compose up -d
```

`idmate_` is the Compose project name — check with `docker volume ls`.

Next: [Setup guide](HOW-TO-SETUP.md) for MQTT, remote access and the first vehicle.
