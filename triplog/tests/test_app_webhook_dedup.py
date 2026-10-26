"""Tests for the charge webhook's meter-span idempotency guard.

Home Assistant delivers readings at-least-once: it advances its meter anchor
only after IDMate confirms storage. When the HTTP RESPONSE is lost although the
reading WAS stored, HA re-sends the same span later — with a new timestamp and
a possibly grown meter_end. The webhook must treat any overlap with an
already-stored span as already-billed energy: fully covered spans are
confirmed without insert, partial overlaps store only the uncovered tail.
Overlap candidates are limited to recent rows so a meter swap/reset (span
numbers starting over) can't false-positive against old readings.
"""

from datetime import datetime, timedelta, timezone

import pytest


def _utc_ts(minutes_ago=0):
    """Naive UTC timestamp string, like HA's utcnow().strftime(...)."""
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S")


@pytest.fixture
def client(app_module, db, monkeypatch):
    import config
    monkeypatch.setattr(config, "CHARGE_WEBHOOK_TOKEN", "test-token")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def _post(client, payload):
    return client.post("/api/charge/reading", json=payload,
                       headers={"Authorization": "Bearer test-token"})


def _readings(db):
    return db.execute(
        "SELECT meter_start, meter_end, kwh FROM charge_readings "
        "ORDER BY timestamp, id").fetchall()


class TestWebhookSpanDedup:
    def test_normal_reading_is_stored(self, client, db):
        r = _post(client, {"vehicle": "EV1", "timestamp": _utc_ts(30),
                           "meter_start": 1000.0, "meter_end": 1002.0})
        assert r.status_code == 200 and r.get_json()["ok"]
        rows = _readings(db)
        assert len(rows) == 1
        assert rows[0]["kwh"] == 2.0

    def test_full_resend_is_confirmed_without_insert(self, client, db):
        # Same span, NEW timestamp (the HA resend signature) → no second row,
        # but a CONFIRMING response so HA advances its anchor and stops.
        _post(client, {"vehicle": "EV1", "timestamp": _utc_ts(30),
                       "meter_start": 1000.0, "meter_end": 1002.0})
        r = _post(client, {"vehicle": "EV1", "timestamp": _utc_ts(15),
                           "meter_start": 1000.0, "meter_end": 1002.0})
        body = r.get_json()
        assert r.status_code == 200 and body["ok"]
        assert body.get("deduped") is True
        assert "skipped" not in body  # HA must advance its anchor
        assert len(_readings(db)) == 1

    def test_partial_resend_stores_only_the_tail(self, client, db):
        # Stored [1000, 1002); resend [1000, 1003) after the meter grew →
        # only [1002, 1003) = 1 kWh may be added, not another 2 kWh.
        _post(client, {"vehicle": "EV1", "timestamp": _utc_ts(30),
                       "meter_start": 1000.0, "meter_end": 1002.0})
        r = _post(client, {"vehicle": "EV1", "timestamp": _utc_ts(15),
                           "meter_start": 1000.0, "meter_end": 1003.0})
        assert r.status_code == 200 and r.get_json()["ok"]
        rows = _readings(db)
        assert len(rows) == 2
        assert rows[1]["meter_start"] == 1002.0
        assert rows[1]["meter_end"] == 1003.0
        assert rows[1]["kwh"] == 1.0
        assert sum(x["kwh"] for x in rows) == 3.0  # meter span total, no double

    def test_adjacent_interval_is_not_deduped(self, client, db):
        # Contiguous intervals share the boundary value — that is NOT overlap.
        _post(client, {"vehicle": "EV1", "timestamp": _utc_ts(30),
                       "meter_start": 1000.0, "meter_end": 1002.0})
        r = _post(client, {"vehicle": "EV1", "timestamp": _utc_ts(15),
                           "meter_start": 1002.0, "meter_end": 1004.0})
        assert r.status_code == 200 and r.get_json()["ok"]
        assert len(_readings(db)) == 2

    def test_overlap_across_vehicles_is_deduped(self, client, db):
        # ONE physical wallbox meter for all cars: a resend that arrives after
        # the input_select moved to the next car must still be caught.
        _post(client, {"vehicle": "EV1", "timestamp": _utc_ts(30),
                       "meter_start": 1000.0, "meter_end": 1002.0})
        r = _post(client, {"vehicle": "EV2", "timestamp": _utc_ts(15),
                           "meter_start": 1000.0, "meter_end": 1002.0})
        assert r.get_json().get("deduped") is True
        assert len(_readings(db)) == 1

    def test_grown_resend_span_past_150_is_clipped_not_rejected(self, client, db):
        # Responses kept getting lost while charging continued: the resend span
        # grows past the 150-kWh plausibility cap. The cap must judge the
        # CLIPPED tail, not the raw span — otherwise the anchor deadlocks and
        # no energy is ever recorded again.
        _post(client, {"vehicle": "EV1", "timestamp": _utc_ts(60),
                       "meter_start": 1000.0, "meter_end": 1010.0})
        r = _post(client, {"vehicle": "EV1", "timestamp": _utc_ts(15),
                           "meter_start": 1000.0, "meter_end": 1155.0})
        body = r.get_json()
        assert r.status_code == 200 and body["ok"]
        assert "skipped" not in body
        rows = _readings(db)
        assert len(rows) == 2
        assert rows[1]["meter_start"] == 1010.0
        assert rows[1]["kwh"] == 145.0

    def test_meter_reset_phantom_still_rejected(self, client, db):
        # No overlap to clip: a stale-anchor phantom span stays implausible.
        r = _post(client, {"vehicle": "EV1", "timestamp": _utc_ts(15),
                           "meter_start": 10.0, "meter_end": 200.0})
        body = r.get_json()
        assert body["ok"] and body.get("skipped") is True
        assert body["reason"] == "kwh_implausible"
        assert len(_readings(db)) == 0

    def test_clipped_tail_beyond_150_still_rejected(self, client, db):
        _post(client, {"vehicle": "EV1", "timestamp": _utc_ts(60),
                       "meter_start": 1000.0, "meter_end": 1010.0})
        r = _post(client, {"vehicle": "EV1", "timestamp": _utc_ts(15),
                           "meter_start": 1000.0, "meter_end": 1161.5})
        body = r.get_json()
        assert body.get("skipped") is True and body["reason"] == "kwh_implausible"
        assert len(_readings(db)) == 1

    def test_old_rows_outside_window_do_not_block(self, client, db):
        # Meter swap/reset: the new meter re-uses old span numbers. Rows older
        # than the recency window must not swallow the new reading.
        old_ts = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
        db.execute(
            "INSERT INTO charge_readings (timestamp, vehicle_plate, meter_start, "
            "meter_end, kwh) VALUES (?, 'EV1', 1000.0, 1002.0, 2.0)", (old_ts,))
        db.commit()
        r = _post(client, {"vehicle": "EV1", "timestamp": _utc_ts(15),
                           "meter_start": 1000.0, "meter_end": 1002.0})
        assert r.status_code == 200 and r.get_json()["ok"]
        assert "deduped" not in r.get_json()
        assert len(_readings(db)) == 2
