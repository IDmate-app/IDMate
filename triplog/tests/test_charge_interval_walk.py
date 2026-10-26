"""Tests for _interval_walk — the reference model behind the print's monthly
kWh/100km and EUR/100km charts.

Core contract: home numbers are REAL consumption. A fill-up around a road trip
must not inflate the short home→home segment it lands in — the SoC correction
scales the metered energy to the share that was actually driven down since the
previous charge. Months without external charges keep the bars-identical
invariant (incl. external == home), and with external charges the incl. value
sits above home (external energy costs more per kWh).
"""

import os

import pytest

pytest.importorskip("flask", reason="needs the full app dependencies")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-suite")
import app  # noqa: F401  — imported first to break the blueprint circular import
from blueprints.charges import _interval_walk


def _s(ym, odo, home, kwh, cost, soc_start=None, soc_end=None):
    return {'ym': ym, 'odometer': odo, 'is_home_charge': 1 if home else 0,
            'total_kwh': kwh, 'session_cost': cost,
            'soc_start': soc_start, 'soc_end': soc_end}


class TestSocCorrection:
    def test_trip_fillup_books_only_driven_share(self):
        # Real 08/2026 shape: previous charge stopped at 52.6 %, 99 km driven
        # down to 31.4 %, then a fill-up to 80 %. Booked energy must be the
        # driven share 21.2/48.6 of the metered 44.14 kWh — not all of it.
        cons, _ = _interval_walk([
            _s('2026-07', 25213, True, 9.25, 3.15, soc_start=42.0, soc_end=52.6),
            _s('2026-08', 25312, True, 44.14, 15.01, soc_start=31.4, soc_end=80.0),
        ])
        seg = cons['2026-08']
        assert seg['dist'] == 99
        assert seg['kwh'] == pytest.approx(44.14 * (52.6 - 31.4) / (80.0 - 31.4))
        assert seg['kwh'] / seg['dist'] * 100 == pytest.approx(19.4, abs=0.1)
        assert seg['cost'] == pytest.approx(15.01 * (52.6 - 31.4) / (80.0 - 31.4))

    def test_missing_soc_falls_back_to_metered_value(self):
        cons, _ = _interval_walk([
            _s('2026-08', 1000, True, 10.0, 3.4, soc_start=40.0, soc_end=None),
            _s('2026-08', 1100, True, 30.0, 10.2, soc_start=30.0, soc_end=80.0),
        ])
        assert cons['2026-08']['kwh'] == 30.0

    def test_implausible_ratio_falls_back(self):
        # soc_end(prev) barely above soc_start(cur) → ratio < 0.2 → raw value.
        cons, _ = _interval_walk([
            _s('2026-08', 1000, True, 10.0, 3.4, soc_start=40.0, soc_end=33.0),
            _s('2026-08', 1100, True, 30.0, 10.2, soc_start=30.0, soc_end=80.0),
        ])
        assert cons['2026-08']['kwh'] == 30.0

    def test_small_charge_lift_falls_back(self):
        # Charge lift < 5 points → division noise → raw value.
        cons, _ = _interval_walk([
            _s('2026-08', 1000, True, 10.0, 3.4, soc_start=40.0, soc_end=76.0),
            _s('2026-08', 1100, True, 30.0, 10.2, soc_start=76.0, soc_end=80.0),
        ])
        assert cons['2026-08']['kwh'] == 30.0

    def test_partial_charge_scales_up(self):
        # Driven 40 points but only recharged 20 → this charge replenishes
        # half of what the km consumed; the booked share doubles (deficit was
        # pre-paid by an earlier surplus charge).
        cons, _ = _interval_walk([
            _s('2026-08', 1000, True, 10.0, 3.4, soc_start=40.0, soc_end=80.0),
            _s('2026-08', 1100, True, 15.0, 5.1, soc_start=40.0, soc_end=60.0),
        ])
        assert cons['2026-08']['kwh'] == pytest.approx(30.0)


class TestInvariants:
    def test_month_without_ext_has_identical_bars(self):
        cons, incl = _interval_walk([
            _s('2026-05', 1000, True, 9.0, 3.06, soc_start=40.0, soc_end=52.0),
            _s('2026-05', 1100, True, 22.0, 7.48, soc_start=30.0, soc_end=80.0),
            _s('2026-05', 1200, True, 21.0, 7.14, soc_start=35.0, soc_end=79.0),
        ])
        assert cons['2026-05']['cost'] == pytest.approx(incl['2026-05']['cost'])
        assert cons['2026-05']['dist'] == incl['2026-05']['dist']

    def test_ext_month_incl_sits_above_home(self):
        # Home flat rate 0.34 €/kWh, external receipt 0.70 €/kWh: the incl.
        # EUR/100km must end up ABOVE the (corrected) home EUR/100km.
        cons, incl = _interval_walk([
            # previous charge stopped at 52.6 %
            _s('2026-08', 1000, True, 9.25, 3.15, soc_start=42.0, soc_end=52.6),
            # fill-up before the trip: only 21.2 of 48.6 points were driven
            _s('2026-08', 1100, True, 44.0, 14.96, soc_start=31.4, soc_end=80.0),
            # DC stop on the road (400 trip km), 44 kWh à 0.70
            _s('2026-08', 1500, False, 44.0, 30.80),
            # arrival charge at home
            _s('2026-08', 1700, True, 36.0, 12.24, soc_start=4.4, soc_end=48.6),
        ])
        home = cons['2026-08']
        both = incl['2026-08']
        home_per100 = home['cost'] / home['dist'] * 100
        incl_per100 = both['cost'] / both['dist'] * 100
        assert incl_per100 > home_per100

    def test_low_kwh_floor_still_drops_untracked_ext(self):
        # 5 kWh for 200 km (< 8/100) → untracked external charge in between.
        cons, _ = _interval_walk([
            _s('2026-06', 1000, True, 20.0, 6.8),
            _s('2026-06', 1200, True, 5.0, 1.7),
        ])
        assert '2026-06' not in cons

    def test_ext_restart_at_same_stop_books_cost_without_km(self):
        _, incl = _interval_walk([
            _s('2026-08', 1500, False, 20.0, 14.0),
            _s('2026-08', 1500, False, 10.0, 7.0),
        ])
        assert incl['2026-08']['cost'] == pytest.approx(7.0)
        assert incl['2026-08']['dist'] == 0

    def test_ext_segment_books_into_ext_month_not_home_month(self):
        # Trip ends on the 31st, home charge follows on the 1st: the ext→home
        # segment must stay in the EXT month (bars-identical guard for the
        # ext-free next month).
        _, incl = _interval_walk([
            _s('2026-07', 900, True, 20.0, 6.8),
            _s('2026-07', 1000, False, 30.0, 21.0),
            _s('2026-08', 1200, True, 40.0, 13.6, soc_start=10.0, soc_end=80.0),
        ])
        assert '2026-08' not in incl
        assert incl['2026-07']['dist'] == 300


class TestMoneyConservation:
    """The correction moves home spend out of a fill-up segment; the incl.
    walk must carry it into the trip that consumed it — every EUR paid shows
    up exactly once in the incl. pot."""

    TRIP = [
        _s('2026-08', 1000, True, 9.25, 3.15, soc_start=42.0, soc_end=52.6),
        # fill-up to 80 %: 14.96 paid, only ~44 % of it driven before the trip
        _s('2026-08', 1100, True, 44.0, 14.96, soc_start=31.4, soc_end=80.0),
        _s('2026-08', 1500, False, 44.0, 30.80),
        _s('2026-08', 1700, True, 36.0, 12.24, soc_start=4.4, soc_end=48.6),
    ]

    def test_trip_month_incl_equals_all_money_over_all_km(self):
        _, incl = _interval_walk(self.TRIP)
        paid = 14.96 + 30.80 + 12.24  # every booked segment's real cost
        assert incl['2026-08']['cost'] == pytest.approx(paid)
        assert incl['2026-08']['dist'] == 700

    def test_home_stays_corrected_while_incl_conserves(self):
        cons, incl = _interval_walk(self.TRIP)
        home = cons['2026-08']
        assert home['cost'] == pytest.approx(14.96 * (52.6 - 31.4) / (80.0 - 31.4))
        assert incl['2026-08']['cost'] > home['cost']

    def test_surplus_built_last_month_lands_in_trip_month(self):
        # Fill-up on the 30th, trip on the 2nd: the unbooked surplus follows
        # the energy into the trip month, not the ext-free month before.
        _, incl = _interval_walk([
            _s('2026-07', 1000, True, 9.25, 3.15, soc_start=42.0, soc_end=52.6),
            _s('2026-07', 1100, True, 44.0, 14.96, soc_start=31.4, soc_end=80.0),
            _s('2026-08', 1500, False, 44.0, 30.80),
        ])
        booked_july = 14.96 * (52.6 - 31.4) / (80.0 - 31.4)
        assert incl['2026-07']['cost'] == pytest.approx(booked_july)
        assert incl['2026-08']['cost'] == pytest.approx(30.80 + (14.96 - booked_july))

    def test_partial_charge_after_surplus_reduces_carry(self):
        # Surplus 10 EUR, then a partial charge that books 5 EUR MORE than
        # paid (it ran on the surplus) → only 5 EUR reach the trip segment.
        _, incl = _interval_walk([
            _s('2026-08', 1000, True, 10.0, 3.4, soc_start=30.0, soc_end=80.0),
            # driven 40 of 80→40, charged 40→60 (20): ratio 2 → books 2x paid
            _s('2026-08', 1100, True, 10.0, 3.4, soc_start=40.0, soc_end=60.0),
            _s('2026-08', 1400, False, 30.0, 21.0),
        ])
        # segment 1 has no prev → nothing; segment 2: paid 3.4, booked 6.8 → carry -3.4
        assert incl['2026-08']['cost'] == pytest.approx(6.8 + 21.0 - 3.4)

    def test_no_ext_month_never_releases_carry(self):
        cons, incl = _interval_walk([
            _s('2026-05', 1000, True, 9.0, 3.06, soc_start=40.0, soc_end=52.0),
            _s('2026-05', 1100, True, 22.0, 7.48, soc_start=30.0, soc_end=80.0),
        ])
        assert incl['2026-05']['cost'] == pytest.approx(cons['2026-05']['cost'])


class TestEnergyInclExternal:
    """kWh follow the same conservation rules as EUR, so the print can show
    consumption over ALL km and ALL charges next to the home-only chart."""

    def test_month_without_ext_has_identical_kwh(self):
        cons, incl = _interval_walk([
            _s('2026-05', 1000, True, 10.0, 3.4, soc_start=40.0, soc_end=60.0),
            _s('2026-05', 1050, True, 10.0, 3.4, soc_start=40.0, soc_end=60.0),
            _s('2026-05', 1100, True, 10.0, 3.4, soc_start=40.0, soc_end=60.0),
        ])
        assert incl['2026-05']['kwh'] == pytest.approx(cons['2026-05']['kwh'])

    def test_trip_month_incl_equals_all_kwh_over_all_km(self):
        _, incl = _interval_walk(TestMoneyConservation.TRIP)
        assert incl['2026-08']['kwh'] == pytest.approx(44.0 + 44.0 + 36.0)
        assert incl['2026-08']['dist'] == 700

    def test_restart_at_same_stop_books_kwh_without_km(self):
        _, incl = _interval_walk([
            _s('2026-08', 1000, True, 10.0, 3.4),
            _s('2026-08', 1300, False, 30.0, 21.0),
            _s('2026-08', 1300, False, 10.0, 7.0),
        ])
        assert incl['2026-08']['kwh'] == pytest.approx(40.0)
