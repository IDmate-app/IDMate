"""The night the clocks go back, 02:00-02:59 local time happens twice. The
readings carry local time without offset, so sorted by text the second pass
came before the first: the session took its first meter from the second
pass, and the print was blocked with +8.05 / -8.05 kWh (25.10.2026)."""

import fahrzeugmodell as fm

# 27.10.2024: 03:00 CEST -> 02:00 CET. (timestamp, created_at UTC, start, end)
NIGHT = [
    ("2024-10-27T01:45:00", "2024-10-26 23:45:00", 7854.00, 7856.82),
    ("2024-10-27T02:15:00", "2024-10-27 00:15:00", 7856.82, 7859.45),
    ("2024-10-27T02:30:00", "2024-10-27 00:30:00", 7859.45, 7862.18),
    ("2024-10-27T02:45:00", "2024-10-27 00:45:00", 7862.18, 7864.87),
    ("2024-10-27T02:00:00", "2024-10-27 01:00:00", 7864.87, 7867.60),
    ("2024-10-27T02:15:00", "2024-10-27 01:15:00", 7867.60, 7870.34),
    ("2024-10-27T02:30:00", "2024-10-27 01:30:00", 7870.34, 7873.03),
    ("2024-10-27T02:45:00", "2024-10-27 01:45:00", 7873.03, 7875.77),
    ("2024-10-27T03:00:00", "2024-10-27 02:00:00", 7875.77, 7878.51),
]


def _rows(created=True):
    return [{"id": i + 1, "timestamp": t, "created_at": c if created else None,
             "meter_start": a, "meter_end": b, "session_id": 9}
            for i, (t, c, a, b) in enumerate(NIGHT)]


def test_order_follows_the_meter_through_the_double_hour():
    for created in (True, False):
        rows = _rows(created)
        out = fm.order_readings(list(reversed(rows)))
        assert [r["meter_start"] for r in out] == [r["meter_start"] for r in rows]
        steps = [b["_utc"] - a["_utc"] for a, b in zip(out, out[1:])]
        assert steps == [1800] + [900] * 7                    # 01:45 -> 02:15 CEST, then 15 min
    assert fm.session_meters(_rows()) == {9: (7854.00, 7878.51)}


def test_reading_without_meter_takes_the_pass_of_created_at():
    rows = [{"id": 1, "timestamp": "2024-10-27T02:15:00", "created_at": "2024-10-27 01:15:00"},
            {"id": 2, "timestamp": "2024-10-27T02:40:00", "created_at": "2024-10-27 00:40:00"}]
    assert [r["id"] for r in fm.order_readings(rows)] == [2, 1]


def _login(app_module, db):
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    db.execute("INSERT INTO users (id, username, password_hash, is_admin) VALUES (1, 'admin', 'x', 1)")
    db.commit()
    c = app_module.app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
    return c


def test_session_through_the_clock_change_reconciles(app_module, db):
    db.execute("INSERT INTO vehicles (plate, device) VALUES ('EV1', '')")
    for t, c, a, b in NIGHT:
        db.execute("INSERT INTO charge_readings (timestamp, created_at, vehicle_plate, meter_start, meter_end, kwh, tibber_price) "
                   "VALUES (?, ?, 'EV1', ?, ?, ?, 0.2)", (t, c, a, b, round(b - a, 2)))
    db.commit()
    app_module.rebuild_charge_sessions(db)
    s = db.execute("SELECT * FROM charge_sessions").fetchall()
    assert len(s) == 1
    s = s[0]
    assert (s["meter_start"], s["meter_end"]) == (7854.00, 7878.51)
    assert abs(s["total_kwh"] - (7878.51 - 7854.00)) < 1e-6
    assert s["start_time"] == "2024-10-27T01:45:00"
    assert s["duration_minutes"] >= 150                     # 01:45 CEST .. 03:15 CET = 2.5 h
    c = _login(app_module, db)
    d = c.get("/api/charge/gaps").get_json()
    assert d["gaps"] == [] and d["overcounts"] == []
    html = c.get("/charges/print?year=2024&month=10").get_data(as_text=True)
    assert "print_recon_warn" not in html and "Über-/Unterzählung" not in html
    rd = c.get(f"/api/charge/sessions/{s['id']}/readings").get_json()
    assert [r["meter_start"] for r in rd] == [n[2] for n in NIGHT] and "_utc" not in rd[0]


def test_migration_repairs_a_stored_session(app_module, db):
    import logging
    import migrations
    db.execute("INSERT INTO charge_sessions (id, vehicle_plate, start_time, end_time, meter_start, meter_end, total_kwh, is_external) "
               "VALUES (9, 'EV1', '2024-10-27T02:00:00', '2024-10-27T03:15:00', 7864.87, 7878.51, 24.51, 0)")
    for t, c, a, b in NIGHT:
        db.execute("INSERT INTO charge_readings (timestamp, created_at, vehicle_plate, meter_start, meter_end, kwh, session_id) "
                   "VALUES (?, ?, 'EV1', ?, ?, ?, 9)", (t, c, a, b, round(b - a, 2)))
    db.commit()
    migrations.m069_dst_session_meters(db, logging.getLogger("t"))
    s = db.execute("SELECT meter_start, meter_end FROM charge_sessions WHERE id = 9").fetchone()
    assert (s["meter_start"], s["meter_end"]) == (7854.00, 7878.51)
