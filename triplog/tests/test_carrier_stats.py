"""Telemetry carriers: brand colours, own colours, range parsing and the
long-term statistics (time per carrier, signal bands, countries, changes)."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import carrier_stats
from plmn import plmn_info

TZ = ZoneInfo("Europe/Berlin")


def test_brand_colours_tell_the_carriers_apart():
    want = {26201: "#e20074", 26203: "#0019a5", 24001: "#990ae3", 24008: "#00c8ff",
            24007: "#141515", 24405: "#006aed", 24002: "#ff8400", 23806: "#ff8400"}
    for plmn, color in want.items():
        assert plmn_info(plmn)[1] == color
    # the eight carriers of the Scandinavia trip: one colour per brand
    names = {plmn_info(p)[0]: plmn_info(p)[1] for p in want}
    assert len(set(names.values())) == len(names) - 1   # 3 (Tre) and 3 (Hi3G) share orange


def test_parse_range_default_all_and_order():
    today = date(2026, 10, 24)
    s, e, days = carrier_stats.parse_range(None, None, TZ, today=today)
    assert days == 30 and s.astimezone(TZ).date() == date(2026, 9, 25)
    s, _, days = carrier_stats.parse_range("", "2026-10-24", TZ, today=today)
    assert s.astimezone(TZ).date() == date(2020, 1, 1) and days > 2000
    s, e, days = carrier_stats.parse_range("2026-10-10", "2026-10-01", TZ, today=today)
    assert days == 10 and s.astimezone(TZ).date() == date(2026, 10, 1)
    assert carrier_stats.window_s(30) == 60 and carrier_stats.window_s(365) == 300
    assert carrier_stats.window_s(30, fine=True) == 30


def _t(minute):
    return datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc) + timedelta(minutes=minute)


def test_summarize_time_signal_countries_and_changes():
    samples = [(_t(i), 26201, 22, 80) for i in range(30)]           # DE, strong, driving
    samples += [(_t(30 + i), 24001, 8, 0) for i in range(10)]       # SE Telia, weak, parked
    samples += [(_t(40 + i), 24008, 99, 50) for i in range(5)]      # SE Telenor, no signal
    samples += [(_t(600), 24001, 15, 0)]                            # long gap: no change counted
    out = carrier_stats.summarize(samples, 60, 30, TZ, {"Telia": "#123456"})
    by = {c["name"]: c for c in out["carriers"]}
    assert [c["name"] for c in out["carriers"]] == ["Telekom", "Telia", "Telenor"]
    assert by["Telekom"]["minutes"] == 30 and by["Telekom"]["drive_minutes"] == 30
    assert by["Telekom"]["strong_pct"] == 100 and by["Telekom"]["avg_csq"] == 22
    assert by["Telia"]["weak_pct"] == 91 and by["Telia"]["color"] == "#123456"
    assert by["Telia"]["custom_color"] and by["Telia"]["brand_color"] == "#990ae3"
    assert by["Telenor"]["avg_csq"] is None and out["no_signal_minutes"] == 5
    assert [k["cc"] for k in out["countries"]] == ["DE", "SE"]
    assert out["switches"] == 2          # Telekom→Telia, Telia→Telenor
    assert out["bucket"] == "day" and out["buckets"][0]["minutes"]["Telekom"] == 30
    assert carrier_stats.summarize(samples, 300, 365, TZ)["bucket"] == "month"


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin) VALUES (1, 'admin', 'x', 1)")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def test_own_colour_saved_and_reset(client, db):
    r = client.post("/api/admin/stats/carrier-color", json={"name": "Tele2", "color": "#AABBCC"})
    assert r.status_code == 200
    assert carrier_stats.load_colors(db) == {"Tele2": "#aabbcc"}
    assert client.post("/api/admin/stats/carrier-color",
                       json={"name": "Tele2", "color": "red"}).status_code == 400
    client.post("/api/admin/stats/carrier-color", json={"name": "Tele2", "color": ""})
    assert carrier_stats.load_colors(db) == {}


def test_telemetry_tab_has_range_picker_and_stats(client):
    html = client.get("/admin").get_data(as_text=True)
    assert 'data-local="carrierRangeApply"' in html
    assert 'id="carrierStatsTable"' in html and 'id="carrierHistChart"' in html


def test_analysis_heatmap_hint_follows_date_filter(client):
    html = client.get("/analysis").get_data(as_text=True)
    assert "Heatmap zeigt den gewählten Zeitraum" in html or "Heatmap shows the selected range" in html
    assert html.count("Datumsfilter wirkt hier nicht") + html.count("date filter does not apply here") <= 1
