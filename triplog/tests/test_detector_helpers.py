"""Unit tests for the pure helper functions in detector.py.

These are deterministic, I/O-free, and pin down the building blocks that the
trip/charge detection relies on: SoC repair, distance maths, the zero-safe
rounding, local-time formatting, delta-decompression and GPS-jump filtering.
"""

import math

import pytest

import detector
from factory import at, reading


# ── sanitize_soc ──────────────────────────────────────────────────────────
class TestSanitizeSoc:
    def test_passthrough_normal_value(self):
        assert detector.sanitize_soc(50.5) == 50.5

    def test_none_stays_none(self):
        assert detector.sanitize_soc(None) is None

    def test_numeric_string_is_parsed(self):
        assert detector.sanitize_soc("75") == 75.0

    def test_non_numeric_string_is_none(self):
        assert detector.sanitize_soc("n/a") is None

    def test_nan_and_inf_are_none(self):
        assert detector.sanitize_soc(float("nan")) is None
        assert detector.sanitize_soc(float("inf")) is None

    def test_clamps_above_100(self):
        assert detector.sanitize_soc(105) == 100.0

    def test_clamps_below_0(self):
        assert detector.sanitize_soc(-10) == 0.0

    def test_u16_underflow_recovers_to_zero(self):
        # Firmware packed a slightly-negative BMS reading as uint16 → ~6553 %.
        # Recovery reinterprets it as signed (-0.1 %) and clamps to 0.
        assert detector.sanitize_soc(6553.5) == 0.0

    def test_u16_underflow_minus_three_percent(self):
        # BMS reported -3 % → (uint16)(-30) = 65506 → /10 = 6550.6 over the wire.
        assert detector.sanitize_soc(6550.6) == 0.0


# ── haversine_m ───────────────────────────────────────────────────────────
class TestHaversine:
    def test_zero_distance(self):
        assert detector.haversine_m(52.5, 13.4, 52.5, 13.4) == 0.0

    def test_one_degree_latitude_is_about_111km(self):
        # 1° of latitude ≈ 111.2 km everywhere.
        d = detector.haversine_m(0.0, 0.0, 1.0, 0.0)
        assert d == pytest.approx(111195, rel=0, abs=50)

    def test_symmetric(self):
        a = detector.haversine_m(52.5, 13.4, 52.51, 13.41)
        b = detector.haversine_m(52.51, 13.41, 52.5, 13.4)
        assert a == pytest.approx(b)


# ── _round_nonneg ─────────────────────────────────────────────────────────
class TestRoundNonneg:
    def test_real_zero_is_preserved(self):
        # The whole point: a real 0 must NOT become None (regression fix 1.2).
        assert detector._round_nonneg(0.0, 1) == 0.0

    def test_negative_clamped_to_zero(self):
        assert detector._round_nonneg(-1.5, 1) == 0.0

    def test_rounds_to_digits(self):
        assert detector._round_nonneg(12.345, 1) == 12.3

    def test_none_passes_through(self):
        assert detector._round_nonneg(None, 2) is None


# ── _local_iso ────────────────────────────────────────────────────────────
class TestLocalIso:
    def test_summer_is_utc_plus_two(self):
        # 2024-06-01 → CEST (UTC+2).
        assert detector._local_iso(at(0)) == "2024-06-01T12:00:00"

    def test_winter_is_utc_plus_one(self):
        from datetime import datetime, timezone
        dt = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        assert detector._local_iso(dt) == "2024-01-01T11:00:00"


# ── expand_rows (delta decompression) ─────────────────────────────────────
class TestExpandRows:
    def test_eq_copies_from_previous_na_sets_none(self):
        # bit 0 = s, bit 4 = v.
        rows = [
            reading(at(0), s=80, v=30),
            {"_time": at(1), "_eq": 1 << 0, "_na": 1 << 4},
        ]
        out = detector.expand_rows(rows)
        assert out[0]["s"] == 80 and out[0]["v"] == 30
        assert out[1]["s"] == 80      # copied from predecessor
        assert out[1]["v"] is None    # explicitly marked not-available

    def test_explicit_value_is_not_overwritten(self):
        rows = [
            reading(at(0), s=80),
            {"_time": at(1), "s": 60, "_eq": 1 << 0},
        ]
        out = detector.expand_rows(rows)
        assert out[1]["s"] == 60


# ── _merge_rows (same-timestamp pivot merge) ──────────────────────────────
class TestMergeRows:
    def test_merges_same_timestamp_rows(self):
        rows = [
            reading(at(0), la=52.5, lo=13.4),
            reading(at(0), v=30),
            reading(at(1), v=40),
        ]
        out = detector._merge_rows(rows)
        assert len(out) == 2
        assert out[0]["la"] == 52.5 and out[0]["v"] == 30
        assert out[1]["v"] == 40

    def test_empty_input(self):
        assert detector._merge_rows([]) == []


# ── _filter_gps_jumps ─────────────────────────────────────────────────────
class TestFilterGpsJumps:
    def test_drops_implausible_jump(self):
        # p1 sits ~830 km from p0 one minute later → way over GPS_MAX_KMH.
        pts = [
            reading(at(0), la=52.5, lo=13.4),
            reading(at(1), la=60.0, lo=13.4),       # impossible jump
            reading(at(2), la=52.5001, lo=13.4),    # back near p0 → plausible
        ]
        out = detector._filter_gps_jumps(pts)
        assert len(out) == 2
        assert out[0]["la"] == 52.5 and out[1]["la"] == 52.5001

    def test_keeps_points_without_gps(self):
        pts = [reading(at(0), v=10), reading(at(1), v=20)]
        assert detector._filter_gps_jumps(pts) == pts
