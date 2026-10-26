"""Builders for synthetic telemetry readings used across the detector tests.

A "reading" is one pivoted InfluxDB row: a dict with a UTC-aware ``_time`` and
any subset of the vehicle fields (v=speed, s=SoC, od=odometer, la/lo=GPS,
kw=cumulative kWh, ca=capacity, …). These helpers keep the test scenarios
readable and the timestamps deterministic (fixed in the past, so the
``datetime.now()``-based "withhold fresh trip" logic never fires).
"""

from datetime import datetime, timedelta, timezone

# All scenarios are anchored well in the past so detect_trips never withholds a
# trip as "too fresh". June = Europe/Berlin summer time (CEST, UTC+2).
BASE = datetime(2024, 6, 1, 10, 0, 0, tzinfo=timezone.utc)


def at(minutes=0, seconds=0):
    """A UTC timestamp ``minutes``/``seconds`` after the fixed BASE anchor."""
    return BASE + timedelta(minutes=minutes, seconds=seconds)


def reading(t, **fields):
    """One telemetry row at time ``t`` with the given fields (None values kept)."""
    row = {"_time": t}
    row.update(fields)
    return row
