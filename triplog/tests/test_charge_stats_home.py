"""/api/charge/stats (analysis charts) must use the same HOME rule as the
charge print: a charge at a location flagged is_home is a home charge even
when it is stored is_external = 1 (reconstructed from trips/SoC before the
wallbox existed), and without a stored cost it is valued at the flat rate —
not at 0 €."""

import pytest


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'dev1')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) "
               "VALUES (1, 'admin', 'x', 1, 1)")
    db.execute("INSERT INTO charge_tariffs (valid_from, pauschale_kwh) VALUES ('2024-01-01', 0.34)")
    db.execute("INSERT INTO charge_locations (name, lat, lon, is_home) VALUES ('Zuhause', 52.8, 13.37, 1)")
    db.execute("INSERT INTO charge_locations (name, lat, lon, is_home) VALUES ('SuC Linstow', 53.6, 12.3, 0)")
    # wallbox session (flat rate stored by the rebuild)
    db.execute("INSERT INTO charge_sessions (vehicle_plate, start_time, is_external, total_kwh, cost_pauschale, location_name) "
               "VALUES ('EV1', '2024-06-02T18:00:00', 0, 10.0, 3.4, 'Zuhause')")
    # reconstructed home charge: is_external = 1, at home, no cost stored
    db.execute("INSERT INTO charge_sessions (vehicle_plate, start_time, is_external, total_kwh, location_name) "
               "VALUES ('EV1', '2024-06-03T18:00:00', 1, 20.0, 'Zuhause')")
    # real external charge with receipt
    db.execute("INSERT INTO charge_sessions (vehicle_plate, start_time, is_external, total_kwh, cost_total, location_name) "
               "VALUES ('EV1', '2024-06-04T12:00:00', 1, 20.0, 14.0, 'SuC Linstow')")
    db.commit()
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def test_reconstructed_home_charge_counts_as_home_at_flat_rate(client):
    d = client.get("/api/charge/stats").get_json()
    assert d["monthly_cost"]["2024-06"] == pytest.approx(3.4 + 20.0 * 0.34 + 14.0)
    assert d["home_vs_ext"] == {"home": 2, "external": 1}
    # energy book: the reconstructed charge is battery-side (ΔSoC × capacity),
    # the sums are on the grid side → ÷ efficiency (default 0.90)
    assert d["monthly_kwh_home"]["2024-06"] == pytest.approx(10.0 + 20.0 / 0.90)
    assert d["monthly_kwh_ext"]["2024-06"] == pytest.approx(20.0)
    assert d["totals"]["cost"] == pytest.approx(24.2)


def test_cost_split_home_flat_and_external_receipts(client):
    d = client.get("/api/charge/stats").get_json()
    assert d["monthly_cost_home"]["2024-06"] == pytest.approx(3.4 + 20.0 * 0.34)
    assert d["monthly_cost_ext"]["2024-06"] == pytest.approx(14.0)
    assert d["monthly_cost"]["2024-06"] == pytest.approx(
        d["monthly_cost_home"]["2024-06"] + d["monthly_cost_ext"]["2024-06"])


def test_ladetracker_footer_matches_analysis(client):
    html = client.get("/charges?from=2024-06-01&to=2024-06-30").get_data(as_text=True)
    # flat-rate sum incl. the reconstructed home charge, receipts, total
    assert "10.20 €" in html        # 3.40 + 6.80
    assert "14.00 €" in html        # external receipt
    assert "24.20 €" in html        # total = analysis monthly_cost
    assert "6.80" in html           # reconstructed home charge valued in the flat-rate column
