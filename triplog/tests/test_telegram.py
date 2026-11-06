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


# ── Old stick firmware (bits 0-19, v1 and v2) against the decoder before
#    1.2.255: same fields, same values, for any mask. ───────────────────────

def _decode_before_1_2_255(version, plaintext):
    """The decoder as it stood in app.py up to 1.2.254 (reference copy)."""
    v1 = [(0, 'la', '<i', 4, 1_000_000), (1, 'lo', '<i', 4, 1_000_000), (2, 'hd', '<H', 2, None),
          (3, 's', '<H', 2, 10), (4, 'u', '<H', 2, None), (5, 'i', '<h', 2, None), (6, 'p', '<h', 2, 10),
          (7, 'v', '<H', 2, 10), (8, 'c', None, 0, None), (9, 'dc', None, 0, None), (10, 'bt', '<b', 1, None),
          (11, 'et', '<b', 1, None), (12, 'r', '<H', 2, 10), (13, 'ca', '<H', 2, 10), (14, 'kw', '<H', 2, 10),
          (15, 'pk', None, 0, None), (16, 'od', '<I', 4, 10), (17, 'ls', '<B', 1, None),
          (18, 'bd', '<B', 1, None), (19, 'lp', '<H', 2, None)]
    v2 = [(b, k, '<I' if k == 'kw' else f, 4 if k == 'kw' else s, d) for b, k, f, s, d in v1]
    schema = {0x01: v1, 0x02: v2}[version]
    mask = struct.unpack_from('<I', plaintext, 0)[0]
    out, off = {}, 8
    for bit, key, fmt, size, div in schema:
        if not mask & (1 << bit):
            continue
        if fmt is None:
            out[key] = 1
            continue
        if off + size > len(plaintext):
            return None
        val = struct.unpack_from(fmt, plaintext, off)[0]
        out[key] = val / div if div else val
        off += size
    out['ts'] = struct.unpack_from('<I', plaintext, 4)[0]
    return out


def test_old_stick_telegrams_decode_exactly_as_before():
    import random
    rnd = random.Random(7)
    fields_v1 = {b: (f, s) for b, _, f, s, _ in tg.FIELDS_V1 if b <= 19}
    for version in (0x01, 0x02):
        for _ in range(400):
            bits = sorted(b for b in range(20) if rnd.random() < 0.5)
            body = b""
            for b in bits:
                fmt, size = fields_v1[b]
                if version == 0x02 and b == 14:
                    fmt, size = '<I', 4
                if fmt:
                    body += bytes(rnd.randrange(256) for _ in range(size))
            plain = struct.pack('<II', sum(1 << b for b in bits), rnd.randrange(1_700_000_000, 1_800_000_000)) + body
            kind, d = tg.decode(tg.encode(version, plain, KEY, bytes(rnd.randrange(256) for _ in range(16))), KEY)
            assert kind == "values"
            assert d == _decode_before_1_2_255(version, plain)
            assert not {"se", "hk", "ct"} & set(d)


def test_old_stick_never_sends_info_and_nothing_breaks_without_it():
    # No 0x10 from an old stick: decode of a plain value telegram must not
    # touch the info path, and an empty/short telegram stays rejected.
    assert tg.decode(b"", KEY) == (None, None)
    assert tg.decode(b"\x02" + bytes(10), KEY) == (None, None)
    assert tg.decode(b"\x07" + bytes(40), KEY) == (None, None)


def test_undecodable_warning_names_the_example_key(app_module, caplog):
    import logging
    app_module._undecodable.update(t=0.0, n=0)
    plain = values_plain([(3, "<H", 623)])
    zero = tg.encode(0x02, plain, bytes(32), IV)
    with caplog.at_level(logging.WARNING):
        app_module._warn_undecodable("tele/dev/data", zero)
    assert "EXAMPLE key" in caplog.text
    caplog.clear()
    app_module._undecodable.update(t=0.0, n=0)
    other = tg.encode(0x02, plain, bytes(range(1, 33)), IV)
    with caplog.at_level(logging.WARNING):
        app_module._warn_undecodable("tele/dev/data", other)
        app_module._warn_undecodable("tele/dev/data", other)      # within the minute: silent
    assert caplog.text.count("cannot be decrypted") == 1


def test_implausible_timestamp_is_dropped_not_written(app_module, caplog):
    import logging, time as _t
    assert app_module._plausible_ts(1_762_000_000)
    assert app_module._plausible_ts(_t.time())
    for bad in (None, 0, 12345, 1_600_000_000, _t.time() + 3 * 86400, 4_000_000_000):
        assert not app_module._plausible_ts(bad)
    app_module._implausible.update(t=0.0, n=0)
    with caplog.at_level(logging.WARNING):
        app_module._warn_implausible_ts("tele/dev/data", 4_000_000_000)
        app_module._warn_implausible_ts("tele/dev/data", 4_000_000_000)   # within the minute: silent
    assert caplog.text.count("implausible timestamp") == 1


def test_wrong_key_can_pass_padding_and_then_fails_the_timestamp_check(app_module):
    """No MAC: some telegrams decrypted with the wrong key still decode. Their
    timestamp is random, which is what the plausibility check catches."""
    plain = values_plain([(3, "<H", 623)]) + bytes(40)   # a full row's length
    right, wrong = bytes(range(1, 33)), bytes(32)
    hits = []
    for i in range(6000):
        raw = tg.encode(0x02, plain, right, i.to_bytes(16, "little"))
        kind, d = tg.decode(raw, wrong)
        if kind == "values":
            hits.append(d["ts"])
    assert hits, "expected false decodes (about 1 in 256)"
    assert sum(app_module._plausible_ts(ts) for ts in hits) <= len(hits) // 10
