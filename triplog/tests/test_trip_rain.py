"""trip_rain.py: rain on a trip from the weather archive (L-003)."""
import sqlite3
from datetime import datetime, timezone, timedelta

import config
import trip_rain
import wind

UTC = timezone.utc


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE trips (id INTEGER PRIMARY KEY, start_time TEXT, end_time TEXT, start_lat REAL, "
               "start_lon REAL, end_lat REAL, end_lon REAL, rain_mmh REAL, rain_level INTEGER, rain_checked INTEGER)")
    return db


def test_level_edges():
    assert trip_rain.level(None) is None
    assert trip_rain.level(0.0) == 0
    assert trip_rain.level(0.09) == 0
    assert trip_rain.level(0.1) == 1
    assert trip_rain.level(0.99) == 1
    assert trip_rain.level(1.0) == 2
    assert trip_rain.level(5.0) == 3
    assert trip_rain.level(12) == 3


def test_trip_mmh_hours_and_points():
    h1 = {"2026-10-01T10": 0.0, "2026-10-01T11": 2.0}
    h2 = {"2026-10-01T10": 1.0, "2026-10-01T11": 3.0}
    t0 = datetime(2026, 10, 1, 10, 20, tzinfo=UTC)
    t1 = datetime(2026, 10, 1, 11, 5, tzinfo=UTC)
    assert trip_rain.trip_mmh([h1, h2], t0, t1) == 1.5
    assert trip_rain.trip_mmh([h1], t0, t0) == 0.0          # one hour only
    assert trip_rain.trip_mmh([{}], t0, t1) is None
    assert trip_rain.trip_mmh([h1], None, t1) is None


def test_run_fills_only_archive_window_and_raster_points(monkeypatch):
    monkeypatch.setattr(config, "WIND_SOURCE", "open-meteo")
    db = _db()
    now = datetime(2026, 11, 17, 12, tzinfo=UTC)
    old = (now - timedelta(days=10)).replace(tzinfo=None)
    db.execute("INSERT INTO trips VALUES (1, ?, ?, 52.5, 13.4, 52.6, 13.6, NULL, NULL, NULL)",
               (old.isoformat(), (old + timedelta(minutes=50)).isoformat()))
    fresh = (now - timedelta(days=2)).replace(tzinfo=None)                       # archive not there yet
    db.execute("INSERT INTO trips VALUES (2, ?, ?, 52.5, 13.4, NULL, NULL, NULL, NULL, NULL)",
               (fresh.isoformat(), fresh.isoformat()))
    db.execute("INSERT INTO trips VALUES (3, ?, ?, 52.5, 13.4, NULL, NULL, 0.0, 0, 1)",   # already looked at
               (old.isoformat(), old.isoformat()))
    asked = []

    def fake(point, d0, d1):
        asked.append(point)
        k = old.strftime("%Y-%m-%dT%H")
        return {k: 6.0}
    n = trip_rain.run(db, UTC, now=now, fetch=fake, pause=0)
    assert n == 1
    r = db.execute("SELECT rain_mmh, rain_level FROM trips WHERE id = 1").fetchone()
    assert r["rain_mmh"] == 6.0 and r["rain_level"] == 3
    assert db.execute("SELECT rain_mmh FROM trips WHERE id = 2").fetchone()[0] is None
    # only raster points, never the trip's own position
    grid_pts = {wind.grid(52.5, 13.4, 0.0)[0], wind.grid(52.6, 13.6, 0.0)[0]}
    assert set(asked) <= grid_pts and (52.5, 13.4) not in asked


def test_run_off_without_weather(monkeypatch):
    monkeypatch.setattr(config, "WIND_SOURCE", "")
    assert trip_rain.run(_db(), UTC, fetch=lambda *a: {}, pause=0) == 0


def test_run_keeps_trip_open_when_archive_fails(monkeypatch):
    monkeypatch.setattr(config, "WIND_SOURCE", "open-meteo")
    db = _db()
    now = datetime(2026, 11, 17, 12, tzinfo=UTC)
    old = (now - timedelta(days=10)).replace(tzinfo=None)
    db.execute("INSERT INTO trips VALUES (1, ?, ?, 52.5, 13.4, NULL, NULL, NULL, NULL, NULL)",
               (old.isoformat(), old.isoformat()))

    def boom(*a):
        raise OSError("down")
    assert trip_rain.run(db, UTC, now=now, fetch=boom, pause=0) == 0
    assert db.execute("SELECT rain_mmh FROM trips").fetchone()[0] is None


def test_wind_at_carries_rain():
    d = {"t": [0], "kmh": [10], "dir": [90], "gust": [20], "temp": [5], "rain": [0.4]}
    assert wind.at(d, 100)["rain"] == 0.4
    old = {"t": [0], "kmh": [10], "dir": [90], "gust": [20], "temp": [5]}          # stored before 1.2.343
    assert wind.at(old, 100)["rain"] is None


def _trip(db, tid, start, lat=52.5, lon=13.4):
    db.execute("INSERT INTO trips VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL)",
               (tid, start.isoformat(), (start + timedelta(minutes=30)).isoformat(), lat, lon))


def test_backfill_once_then_only_new_trips(monkeypatch):
    """First run: the whole period at once, in chunks; trips without a value
    are marked too. Later runs ask nothing for them again."""
    monkeypatch.setattr(config, "WIND_SOURCE", "open-meteo")
    db = _db()
    now = datetime(2026, 11, 17, 12, tzinfo=UTC)
    for k in range(40):                                  # 40 trips over ~10 months, one raster point
        _trip(db, k + 1, (now - timedelta(days=10 + 7 * k)).replace(tzinfo=None))
    asked = []

    def fake(point, d0, d1):
        asked.append((d0, d1))
        return {}                                        # archive knows nothing: still marked
    trip_rain.run(db, UTC, now=now, fetch=fake, pause=0)
    assert len(asked) >= 3 and all((datetime.fromisoformat(b) - datetime.fromisoformat(a)).days < 31
                                   for a, b in asked)
    assert db.execute("SELECT COUNT(*) FROM trips WHERE rain_checked IS NULL").fetchone()[0] == 0
    asked.clear()
    trip_rain.run(db, UTC, now=now + timedelta(days=1), fetch=fake, pause=0)
    assert asked == []                                   # nothing asked twice
    _trip(db, 99, (now - timedelta(days=6)).replace(tzinfo=None))   # becomes old enough two days later
    trip_rain.run(db, UTC, now=now + timedelta(days=2), fetch=fake, pause=0)
    assert len(asked) == 1


def test_backfill_period_is_configurable(monkeypatch):
    monkeypatch.setattr(config, "WIND_SOURCE", "open-meteo")
    db = _db()
    now = datetime(2026, 11, 17, 12, tzinfo=UTC)
    _trip(db, 1, (now - timedelta(days=20)).replace(tzinfo=None))
    _trip(db, 2, (now - timedelta(days=200)).replace(tzinfo=None))
    trip_rain.run(db, UTC, now=now, fetch=lambda *a: {}, pause=0, days=60)
    assert [r[0] for r in db.execute("SELECT id FROM trips WHERE rain_checked IS NOT NULL")] == [1]
