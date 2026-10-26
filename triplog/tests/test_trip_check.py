"""Plausibility of trips filled in by hand or corrected in the gap editor
(red asterisk in the trips list) and the GPS paste format of the editor."""

import trip_check


def _t(**kw):
    base = {"start_time": "2026-10-01T08:00:00", "end_time": "2026-10-01T09:00:00",
            "distance_km": 50.0, "energy_kwh": None, "consumption": None, "is_manual": 1}
    base.update(kw)
    return base


class TestParseGps:
    def test_google_maps_copy(self):
        assert trip_check.parse_gps("53.60308082178861, 9.888429999859472") == (53.60308082178861, 9.888429999859472)

    def test_other_separators(self):
        assert trip_check.parse_gps("53.6;9.9") == (53.6, 9.9)
        assert trip_check.parse_gps(" (53.6 9.9) ") == (53.6, 9.9)

    def test_invalid(self):
        assert trip_check.parse_gps("") is None
        assert trip_check.parse_gps("Hamburg") is None
        assert trip_check.parse_gps("153.6, 9.9") is None


class TestCheckTrip:
    def test_plausible(self):
        c = trip_check.check_trip(_t(energy_kwh=10.0))
        assert c["time"] == "ok" and c["speed"] == 50.0
        assert c["cons_state"] == "ok" and c["cons"] == 20.0 and c["ok"]

    def test_too_fast(self):
        c = trip_check.check_trip(_t(end_time="2026-10-01T08:10:00"))
        assert c["time"] == "fast" and not c["ok"]

    def test_too_slow_only_for_real_distance(self):
        assert trip_check.check_trip(_t(end_time="2026-10-02T08:00:00"))["time"] == "slow"
        assert trip_check.check_trip(_t(end_time="2026-10-02T08:00:00", distance_km=2.0))["time"] == "ok"

    def test_order_and_missing(self):
        assert trip_check.check_trip(_t(end_time="2026-10-01T07:00:00"))["time"] == "order"
        assert trip_check.check_trip(_t(end_time=None))["time"] == "missing"

    def test_overlap(self):
        assert trip_check.check_trip(_t(), overlaps=True)["time"] == "overlap"

    def test_consumption(self):
        assert trip_check.check_trip(_t(consumption=55.0))["cons_state"] == "bad"
        assert trip_check.check_trip(_t())["cons_state"] == "none"

    def test_title_lines(self):
        t = {"trips_flag_manual": "M", "trips_flag_corrected": "C",
             "trips_check_time_ok": "T ok {0}", "trips_check_time_missing": "",
             "trips_check_time_order": "", "trips_check_time_overlap": "",
             "trips_check_time_fast": "", "trips_check_time_slow": "",
             "trips_check_cons_ok": "", "trips_check_cons_bad": "",
             "trips_check_cons_none": "no cons"}
        trip = _t()
        assert trip_check.flag_title(trip, trip_check.check_trip(trip), t) == "M\nT ok 50\nno cons"
