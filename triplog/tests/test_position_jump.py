"""Dashboard position without GPS jumps (1.2.322): the last row of a trip
carried a position 18 km off (odometer standing). The trip log was right —
the detector drops jumps — but the map showed the car there."""

from datetime import datetime, timedelta, timezone

WORK = (53.84, 10.64)
HOME = (53.70, 10.40)          # ~22 km away


def _t(sec):
    return datetime(2026, 11, 11, 11, 20, tzinfo=timezone.utc) + timedelta(seconds=sec)


def _iso(sec):
    return _t(sec).strftime("%Y-%m-%dT%H:%M:%SZ")


def _drive_then(last):
    """A short drive ending at WORK (odometer 100 → 102), then one more row."""
    rows = []
    for i in range(20):
        f = i / 19.0
        rows.append({"_time": _t(i * 15), "la": WORK[0] - 0.02 * (1 - f), "lo": WORK[1] - 0.02 * (1 - f), "od": 100 + round(2 * f)})
    return rows + [last]


def test_parking_row_far_off_is_a_jump(app_module):
    track = _drive_then({"_time": _t(300), "la": HOME[0], "lo": HOME[1], "od": 102})
    la, lo, off = app_module.plausible_position(track)
    assert abs(la - WORK[0]) < 1e-6 and abs(lo - WORK[1]) < 1e-6 and 18 < off < 26
    # the same row at the place the trip ended: fine
    assert app_module.plausible_position(_drive_then({"_time": _t(300), "la": WORK[0], "lo": WORK[1], "od": 102})) is None
    # really driven there (odometer moved 22 km, 20 min later): fine
    assert app_module.plausible_position(_drive_then({"_time": _t(1500), "la": HOME[0], "lo": HOME[1], "od": 124})) is None
    # carried there (ferry, car train: odometer standing, but 40 min later): fine
    assert app_module.plausible_position(_drive_then({"_time": _t(2700), "la": HOME[0], "lo": HOME[1], "od": 102})) is None
    # too little track to judge: left alone
    assert app_module.plausible_position(_drive_then({"_time": _t(300), "la": HOME[0], "lo": HOME[1], "od": 102})[-2:]) is None


def test_state_shows_the_last_plausible_position(app_module):
    app_module._pos_memo.clear()
    asked = []
    def track(t):
        asked.append(t)
        return _drive_then({"_time": _t(300), "la": HOME[0], "lo": HOME[1], "od": 102})
    state = {"lat": HOME[0], "lon": HOME[1], "odometer": 102, "soc": 56, "field_times": {"lat": _iso(300)}}
    out = app_module.hold_plausible_position("devP", dict(state), track)
    assert abs(out["lat"] - WORK[0]) < 1e-6 and abs(out["lon"] - WORK[1]) < 1e-6 and out["position_held"]
    assert out["soc"] == 56                                 # everything else untouched
    # the poller comes by every 10 s: asked only once per new position
    app_module.hold_plausible_position("devP", dict(state), track)
    assert len(asked) == 1
    # a later heartbeat at the same wrong place, odometer still standing: stays replaced
    later = dict(state, field_times={"lat": _iso(4000)})
    out = app_module.hold_plausible_position("devP", later, lambda t: [])
    assert abs(out["lat"] - WORK[0]) < 1e-6 and out["position_held"]
    # the car drives on: the new position counts again
    moved = {"lat": 53.90, "lon": 10.70, "odometer": 110, "field_times": {"lat": _iso(9000)}}
    out = app_module.hold_plausible_position("devP", dict(moved), lambda t: [])
    assert out["lat"] == 53.90 and "position_held" not in out
    # a failing check never breaks the state
    def boom(t):
        raise OSError("influx down")
    out = app_module.hold_plausible_position("devQ", dict(state), boom)
    assert out["lat"] == HOME[0]
    app_module._pos_memo.clear()


def test_live_telegram_with_a_jump_does_not_move_the_car(app_module):
    with app_module._state_lock:
        app_module._state_cache["devL"] = {"lat": WORK[0], "lon": WORK[1], "odometer": 102, "soc": 60}
    app_module.sse_publish("devL", {"la": HOME[0], "lo": HOME[1], "od": 102, "s": 56})
    st = app_module._state_cache["devL"]
    assert st["lat"] == WORK[0] and st["lon"] == WORK[1] and st["soc"] == 56      # position kept, the rest taken
    # driven on (odometer moved): the position is taken
    app_module.sse_publish("devL", {"la": 53.86, "lo": 10.66, "od": 105, "s": 55})
    assert app_module._state_cache["devL"]["lat"] == 53.86
    with app_module._state_lock:
        app_module._state_cache.pop("devL", None)
