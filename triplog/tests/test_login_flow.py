"""The sign-in must not stumble over a page that was open for a while or an
app that was sent to the background:

* a form token older than Flask-WTF's hour used to end in a bare "Bad
  Request" page — the login page comes back with a fresh form instead, JSON
  callers get a readable error;
* the installed app (start_url "/") restarts when the user switches to the
  authenticator app: with the password already accepted it returns to the
  code step, not to the password;
* that accepted password is good for 10 min only;
* a code typed or pasted as "123 456" counts.

The browser side (passkey options fetched ahead, renewed before they expire)
is exercised in tests/browser_login.py."""

import re
import time

import pytest

pyotp = pytest.importorskip("pyotp")

SECRET = pyotp.random_base32()


@pytest.fixture
def client(app_module, db, monkeypatch):
    from werkzeug.security import generate_password_hash
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, totp_secret, totp_enabled) "
               "VALUES (1, 'ingo', ?, 1, ?, 1)", (generate_password_hash("Sup3r!secret"), SECRET))
    db.commit()
    monkeypatch.setitem(app_module.app.config, "TESTING", True)
    monkeypatch.setitem(app_module.app.config, "WTF_CSRF_ENABLED", False)
    app_module._login_attempts.clear()
    app_module._login_attempts_user.clear()
    with app_module.app.test_client() as c:
        yield c


def _token(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def test_stale_login_page_comes_back_with_a_fresh_form(client, app_module, monkeypatch):
    monkeypatch.setitem(app_module.app.config, "WTF_CSRF_ENABLED", True)
    old = _token(client.get("/login").get_data(as_text=True))
    monkeypatch.setitem(app_module.app.config, "WTF_CSRF_TIME_LIMIT", -1)   # every token is too old
    r = client.post("/login", data={"csrf_token": old, "username": "ingo", "password": "Sup3r!secret"})
    html = r.get_data(as_text=True)
    assert r.status_code == 400
    assert 'id="login-form"' in html and 'value="ingo"' in html and "zu lange offen" in html
    with client.session_transaction() as s:
        assert "pending_user_id" not in s          # a stale form signs nobody in
    # the form that came back works
    monkeypatch.setitem(app_module.app.config, "WTF_CSRF_TIME_LIMIT", 3600)
    r = client.post("/login", data={"csrf_token": _token(html), "username": "ingo", "password": "Sup3r!secret"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/login/totp")


def test_stale_token_answers_json_callers_readably(client, app_module, monkeypatch):
    monkeypatch.setitem(app_module.app.config, "WTF_CSRF_ENABLED", True)
    old = _token(client.get("/login").get_data(as_text=True))
    monkeypatch.setitem(app_module.app.config, "WTF_CSRF_TIME_LIMIT", -1)
    r = client.post("/login/passkey/begin", json={}, headers={"X-CSRFToken": old})
    assert r.status_code == 400 and "zu lange offen" in r.get_json()["error"]


def test_stale_code_page_stays_in_the_code_step(client, app_module, monkeypatch):
    monkeypatch.setitem(app_module.app.config, "WTF_CSRF_ENABLED", True)
    with client.session_transaction() as s:
        s["pending_user_id"], s["pending_at"] = 1, time.time()
    old = _token(client.get("/login/totp").get_data(as_text=True))
    monkeypatch.setitem(app_module.app.config, "WTF_CSRF_TIME_LIMIT", -1)
    r = client.post("/login/totp", data={"csrf_token": old, "code": pyotp.TOTP(SECRET).now()})
    html = r.get_data(as_text=True)
    assert r.status_code == 400 and 'autocomplete="one-time-code"' in html and "zu lange offen" in html


def test_restarted_app_returns_to_the_code_step(client):
    r = client.post("/login?next=/trips", data={"username": "ingo", "password": "Sup3r!secret"})
    assert r.headers["Location"].endswith("/login/totp")
    # the app restarts at "/" and is sent to the login
    r = client.get("/login?next=/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login/totp")
    r = client.post("/login/totp", data={"code": pyotp.TOTP(SECRET).now()})
    assert r.status_code == 302 and r.headers["Location"].endswith("/trips")


def test_back_to_login_starts_over(client):
    client.post("/login", data={"username": "ingo", "password": "Sup3r!secret"})
    html = client.get("/login/totp").get_data(as_text=True)
    assert 'href="/login?new=1"' in html
    r = client.get("/login?new=1")
    assert r.status_code == 200 and 'type="password"' in r.get_data(as_text=True)
    assert client.get("/login/totp").status_code == 302          # nothing pending any more


def test_accepted_password_is_good_for_ten_minutes(client):
    client.post("/login", data={"username": "ingo", "password": "Sup3r!secret"})
    with client.session_transaction() as s:
        s["pending_at"] = time.time() - 601
    r = client.get("/login")
    assert r.status_code == 200 and 'type="password"' in r.get_data(as_text=True)
    with client.session_transaction() as s:
        s["pending_user_id"], s["pending_at"] = 1, time.time() - 601
    r = client.post("/login/totp", data={"code": pyotp.TOTP(SECRET).now()})
    html = r.get_data(as_text=True)
    assert 'type="password"' in html and "zu lange gedauert" in html
    assert client.get("/").status_code == 302                    # not signed in


def test_code_with_a_space_counts(client):
    client.post("/login", data={"username": "ingo", "password": "Sup3r!secret"})
    code = pyotp.TOTP(SECRET).now()
    r = client.post("/login/totp", data={"code": code[:3] + " " + code[3:]})
    assert r.status_code == 302 and not r.headers["Location"].endswith("/login/totp")
    assert client.get("/api/passkeys").status_code == 200        # signed in


def test_debug_pages_carry_the_script_nonce(client, app_module, monkeypatch):
    """The CSP only runs inline scripts with the nonce of the request. The
    MQTT monitor had none: it stayed at "Connecting…" for good."""
    import config
    monkeypatch.setattr(config, "ENABLE_DEBUG", True)
    with client.session_transaction() as s:
        s["_user_id"] = "1"
    r = client.get("/mqtt-monitor")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "refresh()" in html
    nonce = re.search(r"'nonce-([^']+)'", r.headers["Content-Security-Policy"]).group(1)
    scripts = re.findall(r"<script[^>]*>", html)
    assert scripts and all(f'nonce="{nonce}"' in tag for tag in scripts)
