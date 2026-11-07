"""Learned corrections for the stick, pushed over MQTT (1.2.267).

The stick answers ABRP over Bluetooth from its own CAN cache. ABRP shows the
BMS SoC, not the one in the car's display. IDmate learns the display SoC
(dashboard button "BMS lernen"); this module sends the function to the
stick, which gives it to ABRP only — the SoC IDmate receives stays raw, the
learning needs it. The odometer offset (config.ODO_OFFSET_KM, the dashboard
reads ~5 km more than CAN) goes along; from stick firmware 1.6.10 the stick
adds it to everything, so IDmate stores dashboard km from then on. The info
telegram reports the offset in force ("odo"); the first one marks the
switch (corrected_since), older charges keep the offset on the print.

Wire: retained message on tele/<device>/cfg, base64 of a frame like the
telegrams ([0x20][IV16][AES-256-CBC PKCS7], same key) around compact JSON:
  {"o": <km offset>, "c": [c0, c1, …], "t": "c[b][e]"}
  display SoC = c0 + c1·CAN + c2·(bat_temp − 20) + c3·(ext_temp − 10),
  terms in the order of "t" (c = CAN SoC, b = battery temp., e = outside temp.).
An empty object switches the corrections off on the stick.

At most once a week (user's wish, 2026-11-07): a changed config waits until
7 days after the last one; the first one goes out at once. The broker keeps
the retained message, the stick gets it on every connect."""

import base64
import json
import logging
import os
import threading
import time

import config
import telegram

log = logging.getLogger("triplog.stick_cfg")

VERSION_CFG = 0x20
MIN_INTERVAL_S = 7 * 86400
TERM_CODE = {"can": "c", "bat_temp": "b", "ext_temp": "e"}

_client = None
_lock = threading.Lock()


def set_client(client):
    global _client
    _client = client


def build(points):
    """The stick's config from the BMS points of one vehicle."""
    import bms_learn as bl
    # km: the fixed dashboard offset. Not learned: since 1.6.10 the stick
    # adds it to what IDmate receives too, a learned value would chase itself.
    out = {"o": config.ODO_OFFSET_KM}
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


def publish(device, cfg):
    """Publish retained. False when there is no client or key."""
    if not device or not config.MQTT_AES_KEY:
        return False
    with _lock:
        client = _client
        if client is None:
            return False
        try:
            key = bytes.fromhex(config.MQTT_AES_KEY)
            client.publish(topic(device), base64.b64encode(frame(cfg, key)), qos=1, retain=True)
        except Exception:
            log.exception("stick cfg: publish for %s failed", device)
            return False
    log.info("stick cfg: %s ← %s", device, json.dumps(cfg, separators=(",", ":")))
    return True


def _key(device):
    return f"stick_cfg:{device}"


def last_sent(db, device):
    """{"ts": unix, "cfg": {...}} of the last push, or None."""
    row = db.execute("SELECT value FROM settings WHERE key = ?", (_key(device),)).fetchone()
    try:
        return json.loads(row["value"]) if row else None
    except (TypeError, ValueError):
        return None


def push_vehicle(db, plate, device, now=None):
    """"sent" | "unchanged" | "wait" (last push < 7 days ago) | "failed"."""
    if not device:
        return "failed"
    points = [dict(r) for r in db.execute(
        "SELECT * FROM bms_points WHERE plate = ? ORDER BY created_at", (plate,))]
    cfg = build(points)
    now = time.time() if now is None else now
    last = last_sent(db, device)
    if last and last.get("cfg") == cfg:
        return "unchanged"
    if last and now - float(last.get("ts") or 0) < MIN_INTERVAL_S:
        return "wait"
    if not publish(device, cfg):
        return "failed"
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
               (_key(device), json.dumps({"ts": int(now), "cfg": cfg}, separators=(",", ":"))))
    db.commit()
    return "sent"


def push_all(db):
    """Every vehicle with BMS learning and a device (on MQTT connect and
    every few hours, so a change held back by the week goes out on time)."""
    for v in db.execute("SELECT plate, device FROM vehicles WHERE bms_learn = 1 "
                        "AND device IS NOT NULL AND device != ''").fetchall():
        push_vehicle(db, v["plate"], v["device"])


# ── switch to dashboard km ────────────────────────────────────────────────

def _cut_key(device):
    return f"odo_corr_since:{device}"


def note_stick_offset(db, device, odo):
    """Info telegram with the offset the stick adds: remember the first time
    (local time, like charge_sessions.start_time). Returns that time."""
    if not device or not odo:
        return None
    row = db.execute("SELECT value FROM settings WHERE key = ?", (_cut_key(device),)).fetchone()
    if row:
        return row["value"]
    from datetime import datetime
    import detector
    now = datetime.now(detector.LOCAL_TZ).strftime("%Y-%m-%dT%H:%M:%S")
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (_cut_key(device), now))
    db.commit()
    log.info("odometer: stick %s adds %s km from %s on — IDmate stores dashboard km", device, odo, now)
    return now


def corrected_since(db, device):
    """Local time from which the stick sends dashboard km, or None."""
    if not device:
        return None
    row = db.execute("SELECT value FROM settings WHERE key = ?", (_cut_key(device),)).fetchone()
    return row["value"] if row else None
