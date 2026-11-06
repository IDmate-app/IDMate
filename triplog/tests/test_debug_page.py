"""Debug page: selection and filters work under the CSP (1.2.262).

With a nonce in script-src the browser ignores 'unsafe-inline', so inline
handlers (onchange=…) and a <script> without the nonce never ran: the
selection stayed at "0 selected", select-all did nothing. The page now has
no inline handlers and its script carries the request's nonce."""

import os
import re
from datetime import datetime, timedelta, timezone

import pytest


class _Rec:
    def __init__(self, values):
        self.values = values


class _Table:
    def __init__(self, recs):
        self.records = recs


class _Influx:
    def __init__(self, rows):
        self._rows = rows

    def query_api(self):
        return self

    def query(self, q, org=None):
        return [_Table([_Rec(r) for r in self._rows])]

    def close(self):
        pass


@pytest.fixture
def page_html(app_module, db, monkeypatch):
    import config
    import detector
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'dev1')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) "
               "VALUES (1, 'admin', 'x', 1, 1)")
    db.commit()
    t0 = datetime(2026, 11, 6, 13, 0, tzinfo=timezone.utc)
    rows = [{"_time": t0 + timedelta(minutes=i), "v": float(i % 3), "s": 60.0 - i,
             "la": 53.5 + i * 0.001, "lo": 10.0} for i in range(6)]
    monkeypatch.setattr(detector, "get_influx", lambda: _Influx(rows))
    monkeypatch.setattr(config, "ENABLE_DEBUG", True)
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        r = c.get("/debug?device=dev1&from=2026-11-06T14:00&to=2026-11-06T15:00")
        assert r.status_code == 200
        return r.get_data(as_text=True), r.headers["Content-Security-Policy"]


def test_no_inline_handlers_and_script_has_the_nonce(page_html):
    html, csp = page_html
    assert not re.search(r"\son(change|click|input|submit)=", html)
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    scripts = re.findall(r"<script([^>]*)>", html)
    assert scripts and all(f'nonce="{nonce}"' in s for s in scripts)


def test_selection_works_in_a_browser_with_the_csp(page_html, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    html, csp = page_html
    with pw.sync_playwright() as p:
        try:
            exe = os.environ.get("PW_CHROMIUM")   # e.g. a system Chromium
            b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        except Exception as e:   # no browser in this environment
            pytest.skip(f"chromium not available: {e}")
        pg = b.new_page()
        errors = []
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.route("http://idmate.test/debug", lambda route: route.fulfill(
            status=200, body=html,
            headers={"Content-Type": "text/html", "Content-Security-Policy": csp}))
        pg.goto("http://idmate.test/debug")
        count = pg.locator("#selCount")
        assert count.inner_text() == "0 selected"
        boxes = pg.locator(".rowsel")
        boxes.nth(1).check()
        boxes.nth(3).check()
        assert count.inner_text() == "2 selected"
        pg.locator("#selAll").check()
        assert count.inner_text() == "6 selected"
        pg.locator("#selAll").uncheck()
        assert count.inner_text() == "0 selected"
        # filter column v (checkbox, trip, _time, la, lo, s, v) to "=1.00", then select all visible
        pg.locator('.filter-row input[data-col="6"]').fill("=1.00")
        assert "visible" in count.inner_text()
        pg.locator("#selAll").check()
        n_vis = pg.locator("#dtable tbody tr:visible").count()
        assert count.inner_text().startswith(f"{n_vis} selected")
        b.close()
        assert not [e for e in errors if "Content Security Policy" in e], errors
