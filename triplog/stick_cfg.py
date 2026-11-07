"""Learned corrections for the stick, pushed over MQTT (1.2.267).

The stick answers ABRP over Bluetooth from its own CAN cache. ABRP shows the
BMS SoC, not the one in the car's display, and the CAN odometer, not the
dashboard's. IDmate learns both (dashboard button "BMS lernen"); this module
sends the result to the stick, which then gives ABRP the display SoC and the
dashboard km. The values IDmate itself receives stay raw — the learning needs
them unchanged.

Wire: retained message on tele/<device>/cfg, base64 of a frame like the
telegrams ([0x20][IV16][AES-256-CBC PKCS7], same key) around compact JSON:
  {"o": <km offset>, "c": [c0, c1, …], "t": "c[b][e]"}
  display SoC = c0 + c1·CAN + c2·(bat_temp − 20) + c3·(ext_temp − 10),
  terms in the order of "t" (c = CAN SoC, b = battery temp., e = outside temp.).
An empty object switches the corrections off on the stick."""

import base64
import json
import logging
import os
import threading

import config
import telegram

log = logging.getLogger("triplog.stick_cfg")

VERSION_CFG = 0x20
TERM_CODE = {"can": "c", "bat_temp": "b", "ext_temp": "e"}

_client = None
_lock = threading.Lock()
_sent = {}                     # device -> last cfg published in this process


def set_client(client):
    global _client
    _client = client


def build(points):
    """The stick's config from the BMS points of one vehicle."""
    import bms_learn as bl
    out = {}
    off = bl.odo_offset(points)
    if off is not None:
        out["o"] = off["km"]
    disp = bl.fit_display(points)
    if disp and all(t in TERM_CODE for t in disp["terms"]):
        out["c"] = [round(float(c), 5) for c in disp["coef"]]
        out["t"] = "".join(TERM_CODE[t] for t in disp["terms"])
    return out


def frame(cfg, key, iv=None):
    plain = json.dumps(cfg, separators=(",", ":")).encode()
    return telegram.encode(VERSION_CFG, plain, key, iv or os.urandom(16))


def topic(device):
    return f"tele/{device}/cfg"


def push(device, cfg, force=False):
    """Publish retained; skipped when unchanged since the last push of this
    process (force: on (re)connect). False when there is no client or key."""
    if not device or not config.MQTT_AES_KEY:
        return False
    with _lock:
        client = _client
        if client is None:
            return False
        if not force and _sent.get(device) == cfg:
            return True
        try:
            key = bytes.fromhex(config.MQTT_AES_KEY)
            client.publish(topic(device), base64.b64encode(frame(cfg, key)), qos=1, retain=True)
        except Exception:
            log.exception("stick cfg: publish for %s failed", device)
            return False
        _sent[device] = cfg
    log.info("stick cfg: %s ← %s", device, json.dumps(cfg, separators=(",", ":")))
    return True


def push_vehicle(db, plate, device, force=False):
    points = [dict(r) for r in db.execute(
        "SELECT * FROM bms_points WHERE plate = ? ORDER BY created_at", (plate,))]
    return push(device, build(points), force=force)


def push_all(db, force=True):
    """Every vehicle with BMS learning and a device (on MQTT connect)."""
    for v in db.execute("SELECT plate, device FROM vehicles WHERE bms_learn = 1 "
                        "AND device IS NOT NULL AND device != ''").fetchall():
        push_vehicle(db, v["plate"], v["device"], force=force)
