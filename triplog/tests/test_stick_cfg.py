"""Learned corrections for the stick (1.2.267): display SoC function and
dashboard km offset, pushed retained over MQTT, encrypted like the telegrams."""

import base64
import json

import pytest

import bms_learn as bl
import telegram as tg


def _pts(n, odo=None):
    out = []
    for i in range(n):
        can = 30 + i * 7
        out.append({"can_soc": can, "disp_soc": round(1.02 * can - 0.6, 1), "bat_temp": 20, "ext_temp": 10,
                    "odometer": 37900 + i, "disp_odo": (37905 + i) if odo else None})
    return out


def test_odo_offset_is_the_median_of_dashboard_minus_can():
    assert bl.odo_offset(_pts(3)) is None
    pts = _pts(3, odo=True)
    pts[1]["disp_odo"] = 99999          # a typo does not move the median
    assert bl.odo_offset(pts) == {"n": 3, "km": 5}


def test_build_carries_offset_and_display_function(monkeypatch):
    import config
    import stick_cfg
    monkeypatch.setattr(config, "ODO_OFFSET_KM", 5)
    assert stick_cfg.build(_pts(2)) == {"o": 5}           # standard offset, no function yet
    pts = _pts(4, odo=True)
    for p in pts:
        p["disp_odo"] += 2                                  # typed km win over the standard
    cfg = stick_cfg.build(pts)
    assert cfg["o"] == 7 and cfg["t"] == "c"
    c0, c1 = cfg["c"]
    assert abs(c0 + 0.6) < 0.05 and abs(c1 - 1.02) < 0.005


def test_frame_decrypts_to_the_json_with_the_shared_key():
    import stick_cfg
    key = bytes(range(1, 33))
    raw = stick_cfg.frame({"o": 5, "c": [-0.6, 1.02], "t": "c"}, key, iv=bytes(16))
    assert raw[0] == 0x20
    assert json.loads(tg.decrypt(raw, key)) == {"o": 5, "c": [-0.6, 1.02], "t": "c"}


class _Client:
    def __init__(self):
        self.pub = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.pub.append((topic, payload, qos, retain))


def test_push_is_retained_base64_and_at_most_weekly(app_module, db, monkeypatch):
    import config
    import stick_cfg
    monkeypatch.setattr(config, "MQTT_AES_KEY", "01" * 32)
    monkeypatch.setattr(config, "ODO_OFFSET_KM", 5)
    db.execute("INSERT INTO vehicles (id, plate, device, bms_learn) VALUES (1, 'EV1', 'dev1', 1)")
    db.commit()
    c = _Client()
    stick_cfg.set_client(c)
    day = 86400
    try:
        assert stick_cfg.push_vehicle(db, "EV1", "dev1", now=1000) == "sent"      # first: at once
        assert stick_cfg.push_vehicle(db, "EV1", "dev1", now=1000 + day) == "unchanged"
        for p in _pts(4):
            db.execute("INSERT INTO bms_points (plate, created_at, disp_soc, can_soc, odometer) "
                       "VALUES ('EV1', ?, ?, ?, ?)", (f"2026-11-0{p['can_soc'] % 7 + 1}T10:00:00Z",
                                                     p["disp_soc"], p["can_soc"], p["odometer"]))
        db.commit()
        assert stick_cfg.push_vehicle(db, "EV1", "dev1", now=1000 + 2 * day) == "wait"   # changed, < 7 days
        assert stick_cfg.push_vehicle(db, "EV1", "dev1", now=1000 + 7 * day) == "sent"
        assert len(c.pub) == 2
        topic, payload, qos, retain = c.pub[-1]
        assert topic == "tele/dev1/cfg" and qos == 1 and retain
        sent = json.loads(tg.decrypt(base64.b64decode(payload), bytes.fromhex("01" * 32)))
        assert sent["o"] == 5 and sent["t"] == "c" and len(sent["c"]) == 2
        assert stick_cfg.last_sent(db, "dev1")["ts"] == 1000 + 7 * day
    finally:
        stick_cfg.set_client(None)


def test_bms_point_with_dashboard_km_pushes_to_the_stick(app_module, db, monkeypatch):
    import config
    import stick_cfg
    from blueprints import bms
    db.execute("INSERT INTO vehicles (id, plate, device, bms_learn) VALUES (1, 'EV1', 'dev1', 1)")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) "
               "VALUES (1, 'admin', 'x', 1, 1)")
    db.commit()
    monkeypatch.setattr(config, "MQTT_AES_KEY", "01" * 32)
    monkeypatch.setattr(bms, "_live", lambda dev: {"can_soc": 61.0, "age_s": 10, "fresh": True,
                                                    "odometer": 37945, "bat_temp": 20, "ext_temp": 8})
    monkeypatch.setattr(bms, "_series", lambda dev, hours=36: [])
    c = _Client()
    stick_cfg.set_client(c)
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    try:
        with app_module.app.test_client() as cl:
            with cl.session_transaction() as s:
                s["_user_id"] = "1"
            r = cl.post("/api/bms-learn", json={"disp_soc": "61", "disp_odo": "37950"})
            assert r.status_code == 200, r.get_data(as_text=True)
            d = r.get_json()
            assert d["odo_offset"] == {"n": 1, "km": 5} and d["stick_sent"]
        assert c.pub and c.pub[-1][0] == "tele/dev1/cfg"
        sent = json.loads(tg.decrypt(base64.b64decode(c.pub[-1][1]), bytes.fromhex("01" * 32)))
        assert sent == {"o": 5}
    finally:
        stick_cfg.set_client(None)
