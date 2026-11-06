"""Stick telegrams: values v2 with the bits of firmware 1.5.0 (SoE, HVAC,
cabin temperature) appended, the info telegram 0x10, and that a server which
only knows bits 0-19 still reads a new telegram right."""

import json
import struct

import telegram as tg

KEY = bytes(range(32))
IV = bytes(16)


def values_plain(mask_fields, ts=1_762_000_000):
    """Plaintext for (bit, struct fmt, raw value) in ascending bit order."""
    mask = sum(1 << b for b, _, _ in mask_fields)
    body = b"".join(struct.pack(f, v) for _, f, v in mask_fields if f)
    return struct.pack("<II", mask, ts) + body


def test_new_bits_decode_and_old_fields_stay_right():
    plain = values_plain([(3, "<H", 623), (10, "<b", 6), (11, "<b", -1), (14, "<I", 123456),
                          (16, "<I", 374320), (20, "<H", 510), (21, "<h", 110), (22, "<b", 24)])
    kind, d = tg.decode(tg.encode(0x02, plain, KEY, IV), KEY)
    assert kind == "values"
    assert d["s"] == 62.3 and d["bt"] == 6 and d["et"] == -1 and d["kw"] == 12345.6 and d["od"] == 37432.0
    assert d["se"] == 51.0 and d["hk"] == 1.1 and d["ct"] == 24 and d["ts"] == 1_762_000_000


def test_a_server_knowing_bits_0_to_19_reads_a_new_telegram():
    plain = values_plain([(3, "<H", 623), (16, "<I", 374320), (19, "<H", 26201),
                          (20, "<H", 510), (21, "<h", 110), (22, "<b", 24)])
    old_schema = [f for f in tg.FIELDS_V2 if f[0] <= 19]
    real = tg.FIELDS_BY_VERSION[0x02]
    try:
        tg.FIELDS_BY_VERSION[0x02] = old_schema
        kind, d = tg.decode(tg.encode(0x02, plain, KEY, IV), KEY)
    finally:
        tg.FIELDS_BY_VERSION[0x02] = real
    assert kind == "values" and d == {"s": 62.3, "od": 37432.0, "lp": 26201, "ts": 1_762_000_000}


def test_info_telegram():
    info = {"fw": "1.5.0", "build": "1.5.0-0-gabc1234", "var": "full", "pv": 2, "bits": 23}
    kind, d = tg.decode(tg.encode(tg.INFO, json.dumps(info).encode(), KEY, IV), KEY)
    assert kind == "info" and d == info
    assert tg.decode(tg.encode(tg.INFO, b"not json", KEY, IV), KEY) == (None, None)


def test_wrong_key_unknown_version_truncated():
    raw = tg.encode(0x02, values_plain([(3, "<H", 623)]), KEY, IV)
    assert tg.decode(raw, bytes(32)) == (None, None)
    assert tg.decode(b"\x07" + raw[1:], KEY) == (None, None)
    short = values_plain([(3, "<H", 623), (16, "<I", 1)])[:-2]          # odometer cut off
    assert tg.decode(tg.encode(0x02, short, KEY, IV), KEY) == (None, None)


def test_stick_info_is_stored_and_reaches_the_dashboard(app_module, db):
    app_module._stick_info.clear()
    app_module._store_stick_info("dev1", {"fw": "1.5.0", "build": "1.5.0-2-gdeadbee-dirty",
                                          "var": "full", "pv": 2, "bits": 23})
    app_module._stick_info.clear()                     # as after a restart
    s = app_module.stick_info("dev1")
    assert s["fw"] == "1.5.0" and s["build"].endswith("-dirty") and s["var"] == "full" and s["seen"]
    assert app_module.stick_info("nope") is None
