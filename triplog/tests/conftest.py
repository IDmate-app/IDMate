"""Pytest bootstrap and shared fixtures for the IDMate test suite.

Two tiers of tests live here:

* **detector tests** (test_detector_*.py) — pure logic, need nothing but
  pytest. The detector imports ``influxdb_client`` at module load but only
  *uses* it at runtime, so when the real package is absent we inject a minimal
  stub and the tests still run.
* **app tests** (test_app_*.py) — exercise real functions in app.py against an
  in-process SQLite DB. These need the full app dependencies (flask, …); when
  those are missing the ``app_module`` fixture skips them via importorskip.

We also put the ``triplog/`` package root on ``sys.path`` so the flat module
layout (``import config`` / ``import detector`` / ``import app``) resolves.
"""

import os
import sys
import tempfile
import types

import pytest

# triplog/ is the parent of this tests/ directory — make config/detector/app importable.
_TRIPLOG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRIPLOG_ROOT not in sys.path:
    sys.path.insert(0, _TRIPLOG_ROOT)

# Set BEFORE anything can import config: it reads both at import time, and
# importing app with an empty SECRET_KEY opens config.DB_PATH — the container's
# /data/triplog.db, which does not exist on a CI runner or a dev machine.
# A test module setting this itself is too late: collection may already have
# pulled config in through another module (e.g. one importing migrations), and
# the module object is then cached with the empty value. Hence: here, at the
# top of conftest, which pytest imports before any test module.
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-suite")
os.environ.setdefault("DB_PATH", os.path.join(tempfile.gettempdir(), "idmate-tests.db"))

# Stub influxdb_client ONLY when the real package is unavailable, so the
# lightweight detector tests run with just pytest installed. When the full
# dependencies are present (app tests), the real package is used instead.
try:
    import influxdb_client  # noqa: F401
except ImportError:
    _stub = types.ModuleType("influxdb_client")

    class InfluxDBClient:  # noqa: N801 - mirror the real class name
        def __init__(self, *args, **kwargs):
            pass

    _stub.InfluxDBClient = InfluxDBClient
    sys.modules["influxdb_client"] = _stub


@pytest.fixture(scope="session")
def app_module():
    """Import app.py once for the whole session, or skip app tests if its
    dependencies (flask, …) aren't installed.

    A dummy SECRET_KEY short-circuits the DB-touching key generation that runs
    at import time, and the CWD is pinned to triplog/ so get_db() finds
    schema.sql regardless of where pytest was launched.
    """
    pytest.importorskip("flask", reason="app tests need the full app dependencies")
    os.environ.setdefault("SECRET_KEY", "test-secret-key-for-suite")
    os.chdir(_TRIPLOG_ROOT)
    import config
    config.DB_PATH = ":memory:"  # never used (SECRET_KEY set), but keep it harmless
    import app
    return app


@pytest.fixture
def db(app_module, tmp_path):
    """A fresh, fully-migrated SQLite database per test.

    Uses app.get_db() on a throwaway temp file so the schema AND all the
    ALTER-TABLE migrations (is_manual, charge_sessions.soc_*, …) are applied
    exactly as in production — schema.sql alone would miss the migrated columns.
    Resetting the _schema_initialized flag forces a full init on the new file.
    """
    import config
    config.DB_PATH = str(tmp_path / "test.db")
    app_module._schema_initialized = False
    conn = app_module.get_db()
    try:
        yield conn
    finally:
        conn.close()
