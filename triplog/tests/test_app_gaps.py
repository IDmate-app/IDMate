"""Tests for app._find_odo_gaps — the odometer-gap detector behind the
admin "Fill gaps" view.

It walks a vehicle's auto-detected (non-manual) trips in odometer order and
reports the missing stretches between them, annotated with how much manual
mileage already covers each gap. Pure read-only logic over the trips table.
"""

import pytest


def _vehicle(db, plate, device):
    db.execute("INSERT INTO vehicles (plate, device) VALUES (?, ?)", (plate, device))


def _trip(db, device, odo_start, odo_end, start, end,
          is_manual=0, distance_km=None):
    db.execute(
        """INSERT INTO trips
           (device, start_time, end_time, odo_start, odo_end, distance_km, is_manual)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (device, start, end, odo_start, odo_end, distance_km, is_manual),
    )


class TestFindOdoGaps:
    def test_gap_between_two_trips(self, app_module, db):
        _vehicle(db, "TEST", "dev1")
        _trip(db, "dev1", 0, 110, "2024-06-01T08:00:00", "2024-06-01T08:30:00")
        _trip(db, "dev1", 130, 140, "2024-06-02T08:00:00", "2024-06-02T08:30:00")
        db.commit()

        gaps = app_module._find_odo_gaps(db, "TEST")
        assert len(gaps) == 1
        g = gaps[0]
        assert g["from_km"] == 110
        assert g["to_km"] == 130
        assert g["km"] == 20
        assert g["assigned_km"] == 0
        assert g["assigned"] is False

    def test_initial_gap_when_first_odometer_positive(self, app_module, db):
        _vehicle(db, "T2", "dev2")
        _trip(db, "dev2", 50, 60, "2024-06-01T08:00:00", "2024-06-01T08:30:00")
        db.commit()

        gaps = app_module._find_odo_gaps(db, "T2")
        assert len(gaps) == 1
        assert gaps[0]["from_km"] == 0
        assert gaps[0]["to_km"] == 50
        assert gaps[0]["km"] == 50
        assert gaps[0]["prev_trip_id"] is None

    def test_micro_gap_below_min_km_is_ignored(self, app_module, db):
        _vehicle(db, "T3", "dev3")
        _trip(db, "dev3", 0, 100, "2024-06-01T08:00:00", "2024-06-01T08:30:00")
        _trip(db, "dev3", 101, 110, "2024-06-02T08:00:00", "2024-06-02T08:30:00")  # +1 km
        db.commit()

        assert app_module._find_odo_gaps(db, "T3") == []

    def test_manual_trip_marks_gap_assigned(self, app_module, db):
        _vehicle(db, "T4", "dev4")
        _trip(db, "dev4", 0, 100, "2024-06-01T08:00:00", "2024-06-01T08:30:00")
        _trip(db, "dev4", 120, 130, "2024-06-03T08:00:00", "2024-06-03T08:30:00")
        # A manual split that exactly fills the 100→120 gap.
        _trip(db, "dev4", 100, 120, "2024-06-02T08:00:00", "2024-06-02T09:00:00",
              is_manual=1, distance_km=20)
        db.commit()

        gaps = app_module._find_odo_gaps(db, "T4")
        assert len(gaps) == 1
        assert gaps[0]["km"] == 20
        assert gaps[0]["assigned_km"] == 20
        assert gaps[0]["assigned"] is True

    def test_unknown_plate_returns_empty(self, app_module, db):
        assert app_module._find_odo_gaps(db, "DOES-NOT-EXIST") == []

    def test_min_km_threshold_is_respected(self, app_module, db):
        _vehicle(db, "T5", "dev5")
        _trip(db, "dev5", 0, 100, "2024-06-01T08:00:00", "2024-06-01T08:30:00")
        _trip(db, "dev5", 105, 110, "2024-06-02T08:00:00", "2024-06-02T08:30:00")  # +5 km
        db.commit()

        # Default min_km=2 → the 5 km gap shows; min_km=10 → it's filtered out.
        assert len(app_module._find_odo_gaps(db, "T5")) == 1
        assert app_module._find_odo_gaps(db, "T5", min_km=10) == []
