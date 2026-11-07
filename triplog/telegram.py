"""Telegrams of the IDTelemetry stick (MQTT tele/<device>/data).

Wire format: [version 1 B][IV 16 B][AES-256-CBC ciphertext, PKCS7].

  0x01 / 0x02  values. Plaintext: [mask u32 LE][unix_s u32 LE][fields of the
               set bits, ascending]. v2 widened kw to u32. Bits are only ever
               APPENDED: a server that knows fewer bits reads the ones it knows
               and ignores the tail, so a newer stick never breaks an older
               server and the version byte stays 0x02.
  0x10         stick info, once per connection: UTF-8 JSON
               {"fw": "1.5.0", "build": "1.5.0-0-gabc1234", "var": "full",
                "pv": 2, "bits": 23}
               (pv = value telegram version, bits = number of known bits).
               An older server does not know 0x10 and drops it.

Bits 20-22 (firmware 1.5.0): soe = remaining energy the BMS reports, hvac
power, cabin temperature — what ABRP showed from the stick.
"""

import json
import struct

# (bit, key, struct format, bytes, divisor); format None = bit only, no bytes
FIELDS_V1 = [
    (0,  'la', '<i', 4, 1_000_000),
    (1,  'lo', '<i', 4, 1_000_000),
    (2,  'hd', '<H', 2, None),
    (3,  's',  '<H', 2, 10),
    (4,  'u',  '<H', 2, None),
    (5,  'i',  '<h', 2, None),
    (6,  'p',  '<h', 2, 10),
    (7,  'v',  '<H', 2, 10),
    (8,  'c',  None,  0, None),
    (9,  'dc', None,  0, None),
    (10, 'bt', '<b', 1, None),
    (11, 'et', '<b', 1, None),
    (12, 'r',  '<H', 2, 10),
    (13, 'ca', '<H', 2, 10),
    (14, 'kw', '<H', 2, 10),
    (15, 'pk', None,  0, None),
    (16, 'od', '<I', 4, 10),
    (17, 'ls', '<B', 1, None),
    (18, 'bd', '<B', 1, None),
    (19, 'lp', '<H', 2, None),   # LTE PLMN (MCC*100+MNC, e.g. 26201 = Telekom DE)
    (20, 'se', '<H', 2, 10),     # SoE: energy left per BMS, kWh
    (21, 'hk', '<h', 2, 100),    # HVAC power, kW
    (22, 'ct', '<b', 1, None),   # cabin temperature, °C
]
FIELDS_V2 = [(bit, key, '<I' if key == 'kw' else fmt, 4 if key == 'kw' else size, div)
             for (bit, key, fmt, size, div) in FIELDS_V1]
FIELDS_BY_VERSION = {0x01: FIELDS_V1, 0x02: FIELDS_V2}
INFO = 0x10
KNOWN = set(FIELDS_BY_VERSION) | {INFO}


def decrypt(raw, aes_key):
    """Plaintext of a telegram, or None (too short, wrong key, not authentic)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    if len(raw) < 17 or not aes_key:
        return None
    try:
        dec = Cipher(algorithms.AES(aes_key), modes.CBC(raw[1:17])).decryptor()
        padded = dec.update(raw[17:]) + dec.finalize()
        unpad = PKCS7(128).unpadder()
        return unpad.update(padded) + unpad.finalize()
    except Exception:
        return None


def decode_values(version, plaintext):
    """{key: value, 'ts': unix_s} or None for a short or truncated telegram."""
    schema = FIELDS_BY_VERSION.get(version)
    if schema is None or plaintext is None or len(plaintext) < 8:
        return None
    mask = struct.unpack_from('<I', plaintext, 0)[0]
    fields, off = {}, 8
    for bit, key, fmt, size, div in schema:
        if not mask & (1 << bit):
            continue
        if fmt is None:
            fields[key] = 1
            continue
        if off + size > len(plaintext):
            return None
        val = struct.unpack_from(fmt, plaintext, off)[0]
        fields[key] = val / div if div else val
        off += size
    fields['ts'] = struct.unpack_from('<I', plaintext, 4)[0]
    return fields


def decode_info(plaintext):
    """The stick's info dict (fw, build, var, pv, bits) or None."""
    try:
        d = json.loads(plaintext.decode('utf-8'))
    except (AttributeError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    out = {k: d.get(k) for k in ('fw', 'build', 'var', 'pv', 'bits')}
    if isinstance(d.get('odo'), (int, float)):     # stick 1.6.10+: km offset it adds
        out['odo'] = d['odo']
    for k in ('fw', 'build', 'var'):
        if out[k] is not None:
            out[k] = str(out[k])[:48]
    return out


def decode(raw, aes_key):
    """('values', dict) | ('info', dict) | (None, None)."""
    if not raw:
        return None, None
    version = raw[0]
    if version not in KNOWN:
        return None, None
    plain = decrypt(raw, aes_key)
    if plain is None:
        return None, None
    if version == INFO:
        info = decode_info(plain)
        return ('info', info) if info else (None, None)
    vals = decode_values(version, plain)
    return ('values', vals) if vals else (None, None)


def encode(version, plaintext, aes_key, iv):
    """Counterpart for tests and tools: the wire bytes of a telegram."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    pad = PKCS7(128).padder()
    data = pad.update(plaintext) + pad.finalize()
    enc = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).encryptor()
    return bytes([version]) + iv + enc.update(data) + enc.finalize()
