"""Admin diagnostics: checks, log ring and the ZIP export (data, derived
figures, model parameters) without users, passkeys or secrets."""

import io
import json
import logging
import zipfile

import pytest


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id, totp_secret) "
               "VALUES (1, 'admin', 'pbkdf2:secret-hash', 1, 1, 'TOTPSECRET')")
    db.execute("INSERT INTO vehicles (id, plate, device, vin, battery_capacity_kwh) "
               "VALUES (1, 'EV1', 'dev1', 'WVWZZZE1ZPP012345', 77)")
    db.execute("INSERT INTO settings (key, value) VALUES ('tibber_token', 'abc'), ('map_style', 'dark')")
    rows = [("2026-10-01T08:00:00", "2026-10-01T09:00:00", 1000, 1050, 50, 80, 70, 7.7, 15.4),
            ("2026-10-02T08:00:00", "2026-10-02T08:30:00", 1050, 1070, 20, 70, 74, None, None),
            ("2026-10-03T08:00:00", "2026-10-03T08:20:00", 1060, 1080, 20, 74, 60, 9.2, 46.0),
            ("2026-10-04T08:00:00", "2026-10-04T08:20:00", 1080, 1090, 10, None, None, None, None)]
    for st, et, o1, o2, km, s1, s2, e, c in rows:
        db.execute("INSERT INTO trips (device, start_time, end_time, odo_start, odo_end, distance_km, "
                   "soc_start, soc_end, energy_kwh, consumption, start_lat, start_lon, start_address, is_manual) "
                   "VALUES ('dev1', ?, ?, ?, ?, ?, ?, ?, ?, ?, 53.55123, 9.99321, 'Musterweg 1', 0)",
                   (st, et, o1, o2, km, s1, s2, e, c))
    db.execute("INSERT INTO charge_sessions (vehicle_plate, start_time, end_time, total_kwh, soc_start, soc_end, odometer) "
               "VALUES ('EV1', '2026-10-02T12:00:00', '2026-10-02T14:00:00', 30, 40, 80, 1060)")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def test_checks_find_the_broken_trips(client):
    d = client.get("/api/admin/diagnostics/checks").get_json()
    by = {(c["key"], c["vehicle"]): c for c in d["checks"]}
    assert by[("trips_no_energy", "EV1")]["value"] == 1
    assert by[("trips_consumption_outlier", "EV1")]["value"] == 1
    assert by[("trips_soc_rises", "EV1")]["value"] == 1
    assert by[("trips_energy_mismatch", "EV1")]["value"] == 0
    assert by[("odometer_backwards", "EV1")]["level"] == "error"
    assert d["env"]["version"] and d["env"]["migration"] == d["env"]["migration_latest"]


def test_log_ring_keeps_warnings(client):
    logging.getLogger("triplog.test").warning("diagnose ring test %d", 42)
    lines = client.get("/api/admin/diagnostics/log?level=WARNING").get_json()["lines"]
    assert any("diagnose ring test 42" in l["msg"] for l in lines)


def _zip(client, q=""):
    r = client.get("/api/admin/diagnostics/export?from=2026-09-01&to=2026-10-31&telemetry=0" + q)
    assert r.status_code == 200 and r.mimetype == "application/zip"
    return zipfile.ZipFile(io.BytesIO(r.data))


def test_export_contents_without_secrets(client):
    z = _zip(client)
    names = set(z.namelist())
    for f in ("manifest.json", "README.txt", "checks.json", "model.json", "config.json",
              "settings.json", "data/trips.csv", "data/charge_sessions.csv", "data/vehicles.csv",
              "calc/trips_derived.csv", "calc/real_consumption_EV1.json", "logs/recent.jsonl"):
        assert f in names, f
    assert not any(n in names for n in ("data/users.csv", "data/passkeys.csv", "data/journey_shares.csv"))
    blob = b"".join(z.read(n) for n in names)
    assert b"TOTPSECRET" not in blob and b"secret-hash" not in blob and b"WVWZZZE1ZPP012345" not in blob
    assert json.loads(z.read("settings.json"))["tibber_token"] == "[redacted]"
    man = json.loads(z.read("manifest.json"))
    assert man["influx_fields"]["s"] == "soc" and man["range"]["from"] == "2026-09-01"
    model = json.loads(z.read("model.json"))
    assert model["vehicles"]["EV1"]["capacity_anchor_kwh"] == 77
    derived = z.read("calc/trips_derived.csv").decode()
    assert "consumption_from_soc" in derived and derived.count("\n") == 5
    assert any(n.startswith("calc/api/") for n in names)
    snap = json.loads(z.read("calc/api/api_stats.json"))
    assert snap["status"] == 200


def test_export_anonymised_positions(client):
    z = _zip(client, "&anon=1&calc=0")
    trips = z.read("data/trips.csv").decode()
    assert "53.55" in trips and "53.55123" not in trips and "Musterweg" not in trips
    assert "calc/trips_derived.csv" not in z.namelist()


def test_admin_has_diagnostics_tab(client):
    html = client.get("/admin").get_data(as_text=True)
    assert 'id="tab-diagnostics"' in html and "initDiagTab" in html


def test_export_readings_all_and_hourly_telemetry(client, db, monkeypatch):
    import detector
    from datetime import datetime, timezone
    db.execute("INSERT INTO charge_readings (timestamp, vehicle_plate, kwh) VALUES "
               "('2025-01-05T10:00:00', 'EV1', 3.1), ('2026-10-02T12:00:00', 'EV1', 4.2)")
    db.commit()

    class Rec:
        def __init__(self, t, vals): self._t, self.values = t, vals
        def get_time(self): return self._t

    class Api:
        def query(self, q, org=None):
            assert "aggregateWindow(every: 1h" in q
            return [type("T", (), {"records": [
                Rec(datetime(2024, 5, 1, 8, tzinfo=timezone.utc), {"_time": 1, "s": 55.25, "ca": 52.1, "la": 53.5, "d": "dev1"}),
                Rec(datetime(2024, 5, 1, 9, tzinfo=timezone.utc), {"s": 50.0, "et": 12.0})]})()]

    class Client:
        def query_api(self): return Api()
        def close(self): pass
    monkeypatch.setattr(detector, "get_influx", lambda timeout_ms=None: Client())
    monkeypatch.setattr("blueprints.diagnostics.detector.get_influx", lambda timeout_ms=None: Client())
    z = _zip(client, "&calc=0&telemetry=0")
    readings = z.read("data/charge_readings.csv").decode()
    assert "2025-01-05" in readings and "2026-10-02" in readings
    hourly = z.read("telemetry_hourly/dev1.csv").decode().splitlines()
    assert hourly[0].startswith("hour,s,od") and hourly[1].startswith("2024-05-01T08:00Z,55.25")
    assert "53.5" not in "\n".join(hourly)                       # no positions
    z = _zip(client, "&calc=0&telemetry=0&longterm=0&readings_all=0")
    assert "2025-01-05" not in z.read("data/charge_readings.csv").decode()
    assert not any(n.startswith("telemetry_hourly/") for n in z.namelist())


def test_export_job_runs_in_background(client, monkeypatch, tmp_path):
    import time
    r = client.post("/api/admin/diagnostics/export/start",
                    json={"from": "2026-09-01", "to": "2026-10-31", "telemetry": "0", "longterm": "0"})
    assert r.status_code == 200 and r.get_json()["state"] in ("running", "done")
    for _ in range(100):
        j = client.get("/api/admin/diagnostics/export/status").get_json()
        if j["state"] != "running":
            break
        time.sleep(0.1)
    assert j["state"] == "done" and j["size"] > 1000 and "path" not in j
    f = client.get("/api/admin/diagnostics/export/file")
    assert f.status_code == 200 and f.mimetype == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(f.data))
    assert "data/trips.csv" in z.namelist() and "calc/api/api_stats.json" in z.namelist()
    f.close()


def test_short_waitress_queue_is_no_finding(client):
    import diagnose
    diagnose.LOG_RING.clear()
    for d in (1, 2, 2, 1):
        logging.getLogger("waitress.queue").warning("Task queue depth is %d", d)
    assert diagnose.log_check()["level"] == "ok"
    logging.getLogger("waitress.queue").warning("Task queue depth is %d", 12)
    assert diagnose.log_check()["level"] == "warn"
    diagnose.LOG_RING.clear()
