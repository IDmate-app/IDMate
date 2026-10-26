"""Golden-scenario tests for detect_trips() and build_trip().

These exercise the actual trip-segmentation rules (start on movement; end on
data gap, prolonged standstill, or a charging SoC-jump) and the per-trip
aggregation (distance, SoC, energy, consumption) on hand-built reading
sequences with analytically known expected results.

Timestamps come from factory.at() (anchored in 2024), so the now()-based
"withhold a still-fresh trip" guard in detect_trips never fires here.
"""

import detector
from factory import at, reading

# Fixed capacity so energy maths is exact and independent of config defaults.
BAT = 80.0


# ── detect_trips: segmentation ────────────────────────────────────────────
class TestDetectTrips:
    def test_single_clean_trip(self):
        rows = [
            reading(at(0), v=30, s=80, od=1000.0, la=52.5000, lo=13.4000),
            reading(at(1), v=50, s=78, od=1000.5, la=52.5010, lo=13.4010),
            reading(at(2), v=40, s=74, od=1001.2, la=52.5020, lo=13.4020),
            reading(at(3), v=20, s=70, od=1001.5, la=52.5030, lo=13.4030),
            reading(at(10), v=0, s=70, od=1001.5, la=52.5030, lo=13.4030),  # gap → end
        ]
        trips = detector.detect_trips(rows, bat_kwh=BAT)
        assert len(trips) == 1
        t = trips[0]
        assert t["start_time"] == "2024-06-01T12:00:00"
        assert t["end_time"] == "2024-06-01T12:03:00"
        assert t["odo_start"] == 1000.0
        assert t["odo_end"] == 1001.5
        assert t["distance_km"] == 1.5
        assert t["soc_start"] == 80.0
        assert t["soc_end"] == 70.0
        assert t["energy_kwh"] == 8.0          # (80-70)/100 * 80
        assert t["consumption"] is None        # distance < 10 km

    def test_data_gap_splits_into_two_trips(self):
        rows = [
            reading(at(0), v=30, s=80, od=1000.0, la=52.50, lo=13.40),
            reading(at(1), v=30, s=79, od=1000.6, la=52.51, lo=13.40),
            reading(at(30), v=30, s=60, od=1010.0, la=52.60, lo=13.40),  # gap → new trip
            reading(at(31), v=30, s=59, od=1010.5, la=52.61, lo=13.40),
            reading(at(60), v=0, s=59, od=1010.5, la=52.61, lo=13.40),   # gap → end
        ]
        trips = detector.detect_trips(rows, bat_kwh=BAT)
        assert len(trips) == 2
        assert trips[0]["distance_km"] == 0.6
        assert trips[1]["distance_km"] == 0.5

    def test_soc_jump_while_standing_ends_trip(self):
        # SoC rises +6 % while the car is (on average) stationary → charging →
        # the trip is split even though no 5-min gap/standstill has elapsed.
        rows = [
            reading(at(0, 0), v=30, s=50, od=1000.0, la=52.50, lo=13.40),
            reading(at(1, 0), v=20, s=49, od=1000.6, la=52.51, lo=13.40),
            reading(at(2, 0), v=0, s=49, od=1000.6, la=52.51, lo=13.40),
            reading(at(3, 0), v=0, s=55, od=1000.6, la=52.51, lo=13.40),  # SoC jump
        ]
        trips = detector.detect_trips(rows, bat_kwh=BAT)
        assert len(trips) == 1
        assert trips[0]["end_time"] == "2024-06-01T12:02:00"  # last point before jump

    def test_trip_below_min_distance_is_discarded(self):
        rows = [
            reading(at(0), v=30, s=80, od=1000.0, la=52.50, lo=13.40),
            reading(at(1), v=30, s=80, od=1000.2, la=52.5005, lo=13.40),  # only 0.2 km
            reading(at(10), v=0, s=80, od=1000.2, la=52.5005, lo=13.40),  # gap → end
        ]
        trips = detector.detect_trips(rows, bat_kwh=BAT)
        assert trips == []

    def test_no_movement_no_trip(self):
        rows = [
            reading(at(0), v=0, s=80, od=1000.0),
            reading(at(5), v=0, s=80, od=1000.0),
            reading(at(10), v=0, s=80, od=1000.0),
        ]
        assert detector.detect_trips(rows, bat_kwh=BAT) == []


# ── Car carried (ferry, car train, tow truck) ─────────────────────────────
def _at_sea(start_min, minutes=4, lat=55.0, lon=13.0):
    """Tracker woken by the ship's motion while the car is off: GPS fixes
    only (no CAN → no speed, odometer, SoC), one every 10 s at ~37 km/h."""
    return [reading(at(start_min, s), la=lat + 0.0006 * (s // 10), lo=lon + 0.0008 * (s // 10))
            for s in range(0, minutes * 60, 10)]


class TestCarriedCar:
    def test_gps_only_movement_is_not_a_trip(self):
        rows = _at_sea(0) + _at_sea(30) + [reading(at(60), la=55.5, lo=13.5)]
        assert detector.detect_trips(rows, bat_kwh=BAT) == []

    def test_ferry_crossing_leaves_two_real_trips(self):
        drive_on = [   # to the terminal and onto the ferry (Travemünde)
            reading(at(0), v=40, s=80, od=5000.0, la=53.95, lo=10.87),
            reading(at(3), v=20, s=79, od=5002.0, la=53.96, lo=10.88),
            reading(at(4), v=0, s=79, od=5002.1, la=53.961, lo=10.881),
        ]
        crossing = _at_sea(30) + _at_sea(300, lat=57.0, lon=18.0)
        drive_off = [   # a day later, off the ferry in Helsinki
            reading(at(1800), v=15, s=76, od=5002.1, la=60.15, lo=24.92),
            reading(at(1803), v=45, s=75, od=5004.0, la=60.16, lo=24.94),
            reading(at(1806), v=0, s=75, od=5005.5, la=60.17, lo=24.95),
        ]
        trips = detector.detect_trips(drive_on + crossing + drive_off + [reading(at(1830), v=0)],
                                      bat_kwh=BAT)
        assert len(trips) == 2
        assert (trips[0]["distance_km"], trips[0]["end_lat"]) == (2.1, 53.961)
        assert (trips[1]["distance_km"], trips[1]["start_lat"]) == (3.4, 60.15)

    def test_odometer_without_speed_still_counts(self):
        # A source without speed but with an odometer: GPS progress + odometer
        # increase is real driving.
        rows = [reading(at(0, s), od=1000.0 + s / 60, la=52.50 + 0.0008 * (s // 10), lo=13.40)
                for s in range(0, 240, 10)] + [reading(at(30), la=52.60, lo=13.40)]
        trips = detector.detect_trips(rows, bat_kwh=BAT)
        # starts at the 2nd fix (the 1st has no predecessor to measure progress)
        assert len(trips) == 1 and trips[0]["distance_km"] == 3.7


# ── build_trip: aggregation ───────────────────────────────────────────────
class TestBuildTrip:
    def test_zero_soc_end_is_preserved(self):
        # Regression for the falsy-bug (fix 1.2): a real 0 % SoC at trip end
        # must be stored as 0.0, not dropped to None — and still drive energy.
        points = [
            reading(at(0), s=5, od=1000.0, la=52.50, lo=13.40),
            reading(at(5), s=0, od=1002.0, la=52.51, lo=13.41),
        ]
        trip = detector.build_trip(points[0], points[-1], points, bat_kwh=BAT)
        assert trip["soc_end"] == 0.0
        assert trip["soc_start"] == 5.0
        assert trip["energy_kwh"] == 4.0      # (5-0)/100 * 80
        assert trip["distance_km"] == 2.0

    def test_gps_distance_fallback_without_odometer(self):
        # No odometer → distance falls back to summed GPS legs.
        points = [
            reading(at(0), s=80, la=52.50, lo=13.40),
            reading(at(5), s=79, la=52.51, lo=13.40),   # ~1.11 km north
        ]
        trip = detector.build_trip(points[0], points[-1], points, bat_kwh=BAT)
        assert trip["odo_start"] is None
        assert trip["distance_km"] == 1.1

    def test_consumption_computed_for_long_trip(self):
        points = [
            reading(at(0), s=50, od=1000.0, la=52.50, lo=13.40),
            reading(at(20), s=40, od=1015.0, la=52.60, lo=13.40),  # 15 km
        ]
        trip = detector.build_trip(points[0], points[-1], points, bat_kwh=BAT)
        assert trip["distance_km"] == 15.0
        assert trip["energy_kwh"] == 8.0       # (50-40)/100 * 80
        assert trip["consumption"] == 53.3     # 8.0 / 15 * 100

    def test_own_capacity_wins_over_influx_ca(self):
        # The BMS estimate (ca) is only a comparison: the own capacity is the
        # reference, as in the recalculation. ca only when there is none.
        points = [
            reading(at(0), s=50, od=1000.0, la=52.50, lo=13.40, ca=60.0),
            reading(at(5), s=40, od=1002.0, la=52.51, lo=13.41, ca=60.0),
        ]
        trip = detector.build_trip(points[0], points[-1], points, bat_kwh=BAT)
        assert trip["energy_kwh"] == round(10 / 100 * BAT, 2)   # not * 60
        trip = detector.build_trip(points[0], points[-1], points, bat_kwh=lambda ts: None)
        assert trip["energy_kwh"] == 6.0

    def test_callable_bat_kwh_is_invoked_with_start_time(self):
        seen = {}

        def cap_at(ts):
            seen["ts"] = ts
            return 70.0

        points = [
            reading(at(0), s=50, od=1000.0, la=52.50, lo=13.40),
            reading(at(5), s=40, od=1002.0, la=52.51, lo=13.41),
        ]
        trip = detector.build_trip(points[0], points[-1], points, bat_kwh=cap_at)
        assert trip["energy_kwh"] == 7.0       # (50-40)/100 * 70
        assert seen["ts"] == at(0)

    def test_empty_points_returns_none(self):
        assert detector.build_trip({}, {}, [], bat_kwh=BAT) is None
