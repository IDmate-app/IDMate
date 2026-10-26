"""Tests for app.rebuild_charge_sessions — the charge-session detector that
groups raw charge_readings into one session per plug-in.

Grouping rule: consecutive readings stay one session unless the car was DRIVEN
between them (a trip in the gap) — a pause keeps the session whole. External
sessions (is_external=1) are never recalculated. Energy/cost are summed over
the full plug-in. Runs in one transaction over the trips/settings/tariffs DB.

All timestamps are in 2024, > 730 days before now, so the InfluxDB SoC lookup
short-circuits and no telemetry backend is touched.
"""

import pytest


def _vehicle(db, plate, device):
    db.execute("INSERT INTO vehicles (plate, device) VALUES (?, ?)", (plate, device))


def _reading(db, plate, ts, kwh, meter_start, meter_end,
             price=0.30, grund=0.0):
    db.execute(
        """INSERT INTO charge_readings
           (timestamp, vehicle_plate, meter_start, meter_end, kwh,
            tibber_price, tibber_grundgebuehr)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ts, plate, meter_start, meter_end, kwh, price, grund),
    )


def _trip(db, device, start, end, distance_km):
    db.execute(
        """INSERT INTO trips (device, start_time, end_time, distance_km)
           VALUES (?, ?, ?, ?)""",
        (device, start, end, distance_km),
    )


def _sessions(db):
    return db.execute(
        "SELECT * FROM charge_sessions ORDER BY datetime(start_time)"
    ).fetchall()


class TestRebuildChargeSessions:
    def test_single_session_aggregates_energy_and_cost(self, app_module, db):
        # One plug-in: three 15-min readings, no driving in between → 1 session.
        _vehicle(db, "EV1", "")  # no device → no trip/influx grouping path
        _reading(db, "EV1", "2024-06-01T10:00:00", 2.0, 1000.0, 1002.0)
        _reading(db, "EV1", "2024-06-01T10:15:00", 2.0, 1002.0, 1004.0)
        _reading(db, "EV1", "2024-06-01T10:30:00", 2.0, 1004.0, 1006.0)
        db.commit()

        app_module.rebuild_charge_sessions(db)

        rows = _sessions(db)
        assert len(rows) == 1
        s = rows[0]
        assert s["vehicle_plate"] == "EV1"
        assert s["is_external"] == 0
        assert s["total_kwh"] == 6.0
        assert s["meter_start"] == 1000.0
        assert s["meter_end"] == 1006.0
        assert s["start_time"] == "2024-06-01T10:00:00"
        assert s["cost_tibber"] == 1.8       # 6 kWh * 0.30
        assert s["cost_pauschale"] == 2.04    # 6 kWh * 0.34 default tariff

    def test_readings_are_linked_to_their_session(self, app_module, db):
        _vehicle(db, "EV1", "")
        _reading(db, "EV1", "2024-06-01T10:00:00", 2.0, 1000.0, 1002.0)
        _reading(db, "EV1", "2024-06-01T10:15:00", 2.0, 1002.0, 1004.0)
        db.commit()

        app_module.rebuild_charge_sessions(db)

        session_id = _sessions(db)[0]["id"]
        linked = db.execute(
            "SELECT COUNT(*) AS n FROM charge_readings WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"]
        assert linked == 2

    def test_trip_between_readings_splits_into_two_sessions(self, app_module, db):
        # Same plate, a >20-min gap AND a real trip in between → two plug-ins.
        _vehicle(db, "EV2", "devx")
        _reading(db, "EV2", "2024-06-01T10:00:00", 2.0, 1000.0, 1002.0)
        _reading(db, "EV2", "2024-06-01T10:15:00", 2.0, 1002.0, 1004.0)
        _reading(db, "EV2", "2024-06-01T14:00:00", 2.0, 1004.0, 1006.0)
        _reading(db, "EV2", "2024-06-01T14:15:00", 2.0, 1006.0, 1008.0)
        _trip(db, "devx", "2024-06-01T12:00:00", "2024-06-01T12:10:00", 5.0)
        db.commit()

        app_module.rebuild_charge_sessions(db)

        assert len(_sessions(db)) == 2

    def test_pause_without_trip_stays_one_session(self, app_module, db):
        # Long gap but NO trip in between → one paused charge, still one session.
        _vehicle(db, "EV3", "devy")
        _reading(db, "EV3", "2024-06-01T10:00:00", 2.0, 1000.0, 1002.0)
        _reading(db, "EV3", "2024-06-01T10:15:00", 2.0, 1002.0, 1004.0)
        _reading(db, "EV3", "2024-06-01T14:00:00", 2.0, 1004.0, 1006.0)
        _reading(db, "EV3", "2024-06-01T14:15:00", 2.0, 1006.0, 1008.0)
        db.commit()

        app_module.rebuild_charge_sessions(db)

        assert len(_sessions(db)) == 1
        assert _sessions(db)[0]["total_kwh"] == 8.0

    def test_external_session_is_preserved(self, app_module, db):
        # is_external=1 rows must survive a rebuild untouched.
        db.execute(
            """INSERT INTO charge_sessions
               (is_external, vehicle_plate, start_time, total_kwh)
               VALUES (1, 'EXT', '2024-05-01T09:00:00', 12.5)"""
        )
        _vehicle(db, "EV1", "")
        _reading(db, "EV1", "2024-06-01T10:00:00", 2.0, 1000.0, 1002.0)
        _reading(db, "EV1", "2024-06-01T10:15:00", 2.0, 1002.0, 1004.0)
        db.commit()

        app_module.rebuild_charge_sessions(db)

        ext = db.execute(
            "SELECT * FROM charge_sessions WHERE is_external = 1"
        ).fetchall()
        assert len(ext) == 1
        assert ext[0]["vehicle_plate"] == "EXT"
        assert ext[0]["total_kwh"] == 12.5
        # plus exactly one freshly rebuilt automatic session
        auto = db.execute(
            "SELECT COUNT(*) AS n FROM charge_sessions WHERE is_external = 0"
        ).fetchone()["n"]
        assert auto == 1

    def test_rebuild_is_idempotent(self, app_module, db):
        _vehicle(db, "EV1", "")
        _reading(db, "EV1", "2024-06-01T10:00:00", 2.0, 1000.0, 1002.0)
        _reading(db, "EV1", "2024-06-01T10:15:00", 2.0, 1002.0, 1004.0)
        db.commit()

        app_module.rebuild_charge_sessions(db)
        app_module.rebuild_charge_sessions(db)

        rows = _sessions(db)
        assert len(rows) == 1
        assert rows[0]["total_kwh"] == 4.0

    def test_scoped_rebuild_keeps_trimmed_leading_reading(self, app_module, db):
        # Regression: a charge that starts mid-interval has a small leading
        # reading (handshake-sized, 0.30 kWh) that the display trim drops from
        # start_time but NOT from the session. The live webhook does a *scoped*
        # rebuild per reading (since=ts). The scope floor must extend back to the
        # session's earliest READING (not its trimmed start_time) — otherwise the
        # leading reading is orphaned ("Unassigned readings") and its Wh
        # vanish from the session total.
        _vehicle(db, "EV9", "")
        seq = [
            ("2024-06-01T19:00:00", 0.30, 5815.34, 5815.64),
            ("2024-06-01T19:15:00", 2.75, 5815.64, 5818.39),
            ("2024-06-01T19:30:00", 2.75, 5818.39, 5821.14),
            ("2024-06-01T19:45:00", 2.75, 5821.14, 5823.89),
        ]
        for ts, kwh, ms, me in seq:
            _reading(db, "EV9", ts, kwh, ms, me)
            db.commit()
            app_module.rebuild_charge_sessions(db, since=ts)  # mirrors the webhook

        # No reading left dangling (NULL or pointing at a deleted session row).
        orphans = db.execute(
            "SELECT COUNT(*) AS n FROM charge_readings "
            "WHERE session_id IS NULL "
            "OR session_id NOT IN (SELECT id FROM charge_sessions)"
        ).fetchone()["n"]
        assert orphans == 0

        rows = _sessions(db)
        assert len(rows) == 1
        # Every Wh counted, including the trimmed 0.30 leading reading.
        assert rows[0]["total_kwh"] == 8.55
        assert rows[0]["meter_start"] == 5815.34


class TestChargeGaps:
    """The /api/charge/gaps endpoint behind the Gaps tab. A gap = a WHOLE missing
    charge, detected on the SHARED wallbox meter ACROSS all vehicles — never
    per-vehicle (else car A sees car B's normal charge as a 'missing' gap)."""

    def _login(self, app_module, db):
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        db.execute("INSERT INTO users (username, password_hash, is_admin) VALUES ('admin', ?, 1)",
                   (app_module.generate_password_hash("Sup3r!secret"),))
        db.commit()
        c = app_module.app.test_client()
        c.post("/login", data={"username": "admin", "password": "Sup3r!secret"})
        return c

    def test_two_cars_sharing_meter_is_not_a_gap(self, app_module, db):
        # Car A ends at meter 106, car B starts at 106 (contiguous shared meter).
        # Two cars, two sessions, but NO missing energy → must report zero gaps.
        _vehicle(db, "CARA", "")
        _vehicle(db, "CARB", "")
        _reading(db, "CARA", "2024-06-01T10:00:00", 2.0, 100.0, 102.0)
        _reading(db, "CARA", "2024-06-01T10:30:00", 2.0, 104.0, 106.0)
        _reading(db, "CARB", "2024-06-02T14:00:00", 2.0, 106.0, 108.0)
        _reading(db, "CARB", "2024-06-02T14:30:00", 2.0, 110.0, 112.0)
        db.commit()
        app_module.rebuild_charge_sessions(db)
        c = self._login(app_module, db)

        d = c.get("/api/charge/gaps").get_json()
        assert d["gaps"] == []
        assert d["total_kwh_missing"] == 0.0

    def test_real_gap_detected_filtered_and_fillable(self, app_module, db):
        # Car A ends at 106, car B starts at 118 → 12 kWh charged with no session.
        _vehicle(db, "CARA", "")
        _vehicle(db, "CARB", "")
        _reading(db, "CARA", "2024-06-01T10:00:00", 2.0, 100.0, 102.0)
        _reading(db, "CARA", "2024-06-01T10:30:00", 2.0, 104.0, 106.0)
        _reading(db, "CARB", "2024-06-02T14:00:00", 2.0, 118.0, 120.0)
        _reading(db, "CARB", "2024-06-02T14:15:00", 2.0, 120.0, 122.0)
        db.commit()
        app_module.rebuild_charge_sessions(db)
        c = self._login(app_module, db)

        d = c.get("/api/charge/gaps?min_kwh=0.05").get_json()
        assert len(d["gaps"]) == 1
        g = d["gaps"][0]
        assert g["kwh_missing"] == 12.0
        assert g["before"]["plate"] == "CARA" and g["before"]["meter"] == 106.0
        assert g["after"]["plate"] == "CARB" and g["after"]["meter"] == 118.0
        assert d["total_kwh_missing"] == 12.0
        # min_kwh threshold hides it (control over large/small gaps)
        assert c.get("/api/charge/gaps?min_kwh=20").get_json()["gaps"] == []

        # Backfill a bridging reading + rebuild → gap closes, energy now counted.
        c.put("/api/charge/readings", json={
            "timestamp": "2024-06-01T20:00:00", "vehicle_plate": "CARA",
            "meter_start": 106.0, "meter_end": 118.0, "kwh": 12.0})
        c.post("/api/charge/rebuild")
        assert c.get("/api/charge/gaps").get_json()["total_kwh_missing"] == 0.0

    def test_overcount_flagged_when_readings_overlap(self, app_module, db):
        # Two readings covering the SAME meter range (e.g. a duplicate pasted
        # during backfill): summed kWh (4.0) exceeds the session's meter span
        # (102-100 = 2.0) → must be flagged so nothing is over-billed.
        _vehicle(db, "DUP", "")
        _reading(db, "DUP", "2024-06-01T10:00:00", 2.0, 100.0, 102.0)
        _reading(db, "DUP", "2024-06-01T10:15:00", 2.0, 100.0, 102.0)
        db.commit()
        app_module.rebuild_charge_sessions(db)
        c = self._login(app_module, db)

        d = c.get("/api/charge/gaps").get_json()
        assert len(d["overcounts"]) == 1
        o = d["overcounts"][0]
        assert o["span"] == 2.0 and o["total_kwh"] == 4.0
        assert abs(o["excess"] - 2.0) < 1e-6

    def test_clean_session_not_flagged_as_overcount(self, app_module, db):
        # Contiguous readings: summed kWh == meter span → no over-count.
        _vehicle(db, "OK1", "")
        _reading(db, "OK1", "2024-06-01T10:00:00", 2.0, 100.0, 102.0)
        _reading(db, "OK1", "2024-06-01T10:15:00", 2.0, 102.0, 104.0)
        db.commit()
        app_module.rebuild_charge_sessions(db)
        c = self._login(app_module, db)

        assert c.get("/api/charge/gaps").get_json()["overcounts"] == []


def test_print_recon_when_one_car_charges_inside_the_others_session(app_module, db):
    """10.10.2026: the Nox charged 01:15-17:15, the Sirius 11:00-12:00 in
    between on the same wallbox. In start order the Sirius session (ending at
    meter 138) comes after the Nox one (ending at 186.6); the next Sirius
    charge starts there. Against "the previous session's end" that was a 54 kWh gap
    and blocked the print; against the highest meter so far there is none."""
    db.execute("INSERT INTO vehicles (plate, device) VALUES ('NOX', ''), ('SIR', '')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin) VALUES (1, 'admin', 'x', 1)")
    m = 100.0
    def rd(plate, ts, kwh):
        nonlocal m
        db.execute("INSERT INTO charge_readings (timestamp, vehicle_plate, meter_start, meter_end, kwh) VALUES (?, ?, ?, ?, ?)",
                   (ts, plate, m, m + kwh, kwh))
        m += kwh
    for h in range(1, 11):
        rd("NOX", f"2026-10-10T{h:02d}:15:00", 2.7)
    for mi in ("00", "15", "30", "45"):
        rd("SIR", f"2026-10-10T11:{mi}:00", 2.75)
    for h in range(12, 18):
        for mi in ("15", "30", "45"):
            rd("NOX", f"2026-10-10T{h:02d}:{mi}:00", 2.7)
    rd("SIR", "2026-10-11T12:15:00", 4.0)
    db.commit()
    app_module.rebuild_charge_sessions(db)
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        html = c.get("/charges/print?year=2026&month=10").get_data(as_text=True)
    assert 'id="printBtn"' in html and "−" not in html.split('id="printRoot"')[0]
