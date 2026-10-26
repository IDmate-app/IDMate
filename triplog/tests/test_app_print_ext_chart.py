"""Render test for the print's "incl. external charges" chart: the stacked
bar needs the home EUR/100km series next to the incl. series (same month
order) and the two legend labels — all wired through Jinja, so a broken
template/translation key would 500 here."""

import pytest


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin) "
               "VALUES (1, 'admin', 'x', 1)")
    db.commit()
    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def _seed(db):
    db.execute("INSERT INTO vehicles (plate, device) VALUES ('EV1', '')")
    rows = [  # (start, ext, kwh, cost_total, odo, soc_start, soc_end)
        ('2024-06-01T10:00:00', 0, 9.25, None, 1000, 42.0, 52.6),
        ('2024-06-03T10:00:00', 0, 44.0, None, 1100, 31.4, 80.0),
        ('2024-06-05T14:00:00', 1, 44.0, 30.80, 1500, None, None),
        ('2024-06-06T10:00:00', 0, 36.0, None, 1700, 4.4, 48.6),
    ]
    for st, ext, kwh, ct, odo, ss, se in rows:
        db.execute(
            "INSERT INTO charge_sessions (vehicle_plate, start_time, end_time, "
            "is_external, total_kwh, cost_total, cost_pauschale, odometer, "
            "soc_start, soc_end) VALUES ('EV1', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (st, st, ext, kwh, ct, None if ext else round(kwh * 0.34, 4), odo, ss, se))
    db.commit()


def test_print_ext_chart_renders_stacked_series(client, db, app_module):
    _seed(db)
    with client.session_transaction() as s:
        s["vehicle"] = "EV1"
    r = client.get("/charges/print?year=2024&month=06&stats=1&ext_stats=1")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'id="chartCostExt"' in html
    assert "const dataCostHome = [" in html
    # consumption over all km and all charges, next to the cost chart
    assert 'id="chartConsExt"' in html
    assert "const dataConsExt = [" in html
    assert "Verbrauch pro 100 km (inkl. externe Ladungen)" in html
    assert "const dataExtSurcharge" in html
    assert "stacked: true" in html
    # legend labels come from the translation files (DE default)
    assert "Heimladung" in html
    assert "Aufschlag durch externe Ladungen" in html
    # iOS home-screen apps cannot print: the page makes a PDF to share instead
    # (idmate.js printOrShare) — hint and library are wired, hint hidden until
    # JS sees the app running standalone
    assert 'id="printPdfHint" hidden' in html and "vendor/html2pdf.bundle.min.js" in html
    assert 'id="printRoot"' in html and "printOrShare(" in html


def test_month_without_home_charge_has_no_energy_value(client, db, app_module):
    _seed(db)
    # July: only an external charge → home energy/consumption must be null
    # (the chart draws an X), never a literal 0.
    db.execute(
        "INSERT INTO charge_sessions (vehicle_plate, start_time, end_time, "
        "is_external, total_kwh, cost_total, odometer) "
        "VALUES ('EV1', '2024-07-02T12:00:00', '2024-07-02T12:30:00', 1, 30.0, 9.90, 1850)")
    db.commit()
    with client.session_transaction() as s:
        s["vehicle"] = "EV1"
    r = client.get("/charges/print?year=2024&month=07&stats=1&ext_stats=1")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    kwh_line = next(l for l in html.splitlines() if "const dataKwh = [" in l)
    assert kwh_line.rstrip().endswith("null];")
    cons_line = next(l for l in html.splitlines() if "const dataCons = [" in l)
    assert cons_line.rstrip().endswith("null];")
    assert "const dataExtSaving" in html
    assert "dlExclude: true" in html
    assert "Ersparnis durch externe Ladungen" in html


def test_print_ext_table_carries_the_odometer_plus_provider_offset(client, db, app_module):
    """The external-charges table shows the odometer like the wallbox table —
    plus the provider offset (the card system records the charge ~5 km later
    than the dashboard), so the print matches the statement it is checked
    against. The wallbox table keeps the raw stamp."""
    _seed(db)
    with client.session_transaction() as s:
        s["vehicle"] = "EV1"
    html = client.get("/charges/print?year=2024&month=06&stats=1&ext_stats=1").get_data(as_text=True)
    ext_table = html.split("Externe Ladungen im Monat", 1)[1].split("</table>", 1)[0]
    assert "km-Stand" in ext_table
    assert "<td class=\"num\">1505</td>" in ext_table
    assert "<td class=\"num\">1500</td>" not in ext_table
    home_table = html.split("Externe Ladungen im Monat", 1)[0]
    assert "<td class=\"num\">1100</td>" in home_table   # raw stamp, no offset


def test_print_page_disables_ios_text_autosizing(client, db, app_module):
    """iOS Safari inflates small text once the page is wider than the display,
    which happens while the in-app PDF is rendered at A4 width: date block,
    hints and tariff comparison came out huge. text-size-adjust must be 100%
    on the print page and in the shared stylesheet (analysis PDF)."""
    _seed(db)
    with client.session_transaction() as s:
        s["vehicle"] = "EV1"
    html = client.get("/charges/print?year=2024&month=06").get_data(as_text=True)
    assert "-webkit-text-size-adjust:100%" in html
    # toolbar link back to the charge overview — without ?vehicle=, which
    # would outrank the header's vehicle pick on the overview
    assert 'class="btn-back" href="/charges"' in html
    assert "Zurück zur Ladeübersicht" in html
    css = client.get("/static/idmate.css").get_data(as_text=True)
    assert "html{-webkit-text-size-adjust:100%;text-size-adjust:100%}" in css


def test_print_charts_span_the_full_width(client, db, app_module):
    """The chart column used to be capped at 556 px, so in the A4 PDF the
    charts were narrower than the tiles and tables above them."""
    _seed(db)
    with client.session_transaction() as s:
        s["vehicle"] = "EV1"
    html = client.get("/charges/print?year=2024&month=06&stats=1").get_data(as_text=True)
    grid_css = html.split(".chart-grid{", 1)[1].split("}", 1)[0]
    assert "max-width" not in grid_css


def test_yearly_estimate_sheet_is_visible_while_pdf_renders(client, db, app_module):
    """The yearly-estimate print sheet (#yePrint) is display:none outside
    @media print. The in-app PDF (html2pdf) is not a print context, so the
    sheet rendered blank (3 KB PDF) until html.pdf-rendering shows it."""
    with client.session_transaction() as s:
        s["vehicle"] = "EV1"
    html = client.get("/analysis").get_data(as_text=True)
    assert "html.pdf-rendering #yePrint{display:block" in html
    # the sheet's own styles must not be locked inside @media print
    before_print = html.split("@media print{", 1)[0]
    assert "#yePrint .ye-table td{color:#222" in before_print
    # the customise panel offers the print switches (stored per vehicle)
    for key in ("hide_dest", "hide_estimate", "hide_pct", "hide_bar"):
        assert f"optBox('{key}'" in html
