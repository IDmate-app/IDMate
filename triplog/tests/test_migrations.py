"""Tests for the versioned schema-migration runner (migrations.py).

These run in the lightweight tier — they need only pytest + sqlite3 (the
``influxdb_client`` stub in conftest covers detector's import). flask is not
required: a fresh DB has no rows, so the one data-migration that pulls
``get_bat_kwh`` from app (m006) returns before importing it.
"""

import os
import sqlite3
import logging

import migrations
from migrations import run_migrations, SCHEMA_VERSION, MIGRATIONS

_TRIPLOG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCHEMA_SQL = os.path.join(_TRIPLOG_ROOT, "schema.sql")
_log = logging.getLogger("test_migrations")


def _build(path):
    """Create a DB exactly as get_db does: schema.sql then run_migrations."""
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys=ON")
    with open(_SCHEMA_SQL) as f:
        db.executescript(f.read())
    run_migrations(db, _log)
    return db


def _has_col(db, table, col):
    return col in {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _has_table(db, name):
    return name in {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def test_registry_is_contiguous():
    versions = [v for v, _, _ in MIGRATIONS]
    assert versions == list(range(1, len(MIGRATIONS) + 1)), "version numbers must be 1..N gap-free"
    assert SCHEMA_VERSION == len(MIGRATIONS)
    names = [n for _, n, _ in MIGRATIONS]
    assert len(names) == len(set(names)), "migration names must be unique"


def test_fresh_db_reaches_head(tmp_path):
    db = _build(str(tmp_path / "fresh.db"))
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        db.close()


def test_fresh_db_has_migrated_schema(tmp_path):
    db = _build(str(tmp_path / "fresh.db"))
    try:
        # columns added by ALTER-TABLE migrations across several tables
        assert _has_col(db, "trips", "is_manual")
        assert _has_col(db, "trips", "kw_start")
        assert _has_col(db, "trips", "start_geo_lat")
        assert _has_col(db, "users", "avatar_filename")
        assert _has_col(db, "charge_sessions", "soc_start")
        assert _has_col(db, "charge_readings", "soc")
        assert _has_col(db, "charge_locations", "polygon_coords")
        assert _has_col(db, "charge_locations", "operator_id")
        assert _has_col(db, "locations", "icon_color")
        # tables created by migrations
        assert _has_table(db, "operators")
        assert _has_table(db, "user_notes")
        # FIXES 15.1 — trip-tag visibility
        assert _has_col(db, "purpose_meta", "created_by")
        assert _has_col(db, "preset_values", "created_by")
        assert _has_col(db, "route_rules", "created_by")
        assert _has_col(db, "users", "language")
        assert _has_table(db, "purpose_visibility")
        assert _has_table(db, "preset_value_visibility")
        assert _has_table(db, "route_rule_visibility")
    finally:
        db.close()


def test_tag_visibility_semantics(tmp_path):
    """FIXES 15.1: a tag is visible to user X if (a) a visibility row
    (tag, X) exists OR (b) the tag has no visibility rows at all
    (legacy/global). No duplicates: one catalog entry, multiple assignments."""
    db = _build(str(tmp_path / "vis.db"))
    try:
        db.execute("INSERT INTO users (username, password_hash, is_admin) VALUES ('a', 'x', 1)")
        db.execute("INSERT INTO users (username, password_hash, is_admin) VALUES ('b', 'x', 0)")
        ua, ub = [r[0] for r in db.execute("SELECT id FROM users ORDER BY id")][:2]
        db.execute("INSERT INTO purpose_meta (name) VALUES ('Legacy')")               # no vis -> all
        db.execute("INSERT INTO purpose_meta (name, created_by) VALUES ('Arbeitsweg', ?)", (ua,))
        db.execute("INSERT INTO purpose_visibility (name, user_id) VALUES ('Arbeitsweg', ?)", (ua,))
        q = ("SELECT name FROM purpose_meta WHERE name IN "
             "(SELECT name FROM purpose_visibility WHERE user_id = ?) "
             "OR name NOT IN (SELECT DISTINCT name FROM purpose_visibility) ORDER BY name")
        assert [r[0] for r in db.execute(q, (ua,))] == ["Arbeitsweg", "Legacy"]
        assert [r[0] for r in db.execute(q, (ub,))] == ["Legacy"]
        # Admin shares 'Arbeitsweg' with B -> B sees it, NO second catalog entry
        db.execute("INSERT INTO purpose_visibility (name, user_id) VALUES ('Arbeitsweg', ?)", (ub,))
        assert [r[0] for r in db.execute(q, (ub,))] == ["Arbeitsweg", "Legacy"]
        assert db.execute("SELECT COUNT(*) FROM purpose_meta WHERE name='Arbeitsweg'").fetchone()[0] == 1
    finally:
        db.close()


def test_rerun_is_noop(tmp_path):
    path = str(tmp_path / "rerun.db")
    db = _build(path)
    db.close()
    # Reopen (simulating a restart) and migrate again — must stay at head.
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys=ON")
    try:
        run_migrations(db, _log)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        db.close()


def test_legacy_unversioned_db_rewalks_safely(tmp_path):
    """A pre-versioning DB has the full schema but user_version 0. The runner
    must re-walk every (guarded) migration without crashing and climb to head."""
    path = str(tmp_path / "legacy.db")
    db = _build(path)
    db.execute("PRAGMA user_version = 0")
    db.commit()
    try:
        run_migrations(db, _log)  # guards make every migration a no-op
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        # schema is intact, not duplicated/dropped
        assert _has_table(db, "operators")
        assert _has_col(db, "trips", "is_manual")
    finally:
        db.close()


def test_route_rules_rebuild_makes_from_nullable(tmp_path):
    """m017 rebuilds route_rules so from/to become nullable (wildcard rules)."""
    db = _build(str(tmp_path / "rr.db"))
    try:
        notnull = {r[1]: r[3] for r in db.execute("PRAGMA table_info(route_rules)").fetchall()}
        assert notnull.get("from_location_id") == 0
        assert notnull.get("to_location_id") == 0
    finally:
        db.close()
