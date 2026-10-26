"""Password managers fill the one-time code only when they can tie the code
step to the saved account: the code step is the login page itself, the user
name stays in a visible read-only username field (Apple Passwords ignores a
hidden one), the password field is gone and the code field is
"one-time-code". The 2FA setup offers the otpauth link, which opens Apple
Passwords on an iPhone so the code joins the existing IDmate entry."""

import pytest

pyotp = pytest.importorskip("pyotp")


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, totp_secret) "
               "VALUES (1, 'ingo', 'x', 1, ?)", (pyotp.random_base32(),))
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        yield c


def test_code_page_names_the_account_for_password_managers(client):
    with client.session_transaction() as s:
        s["pending_user_id"] = 1
    html = client.get("/login/totp").get_data(as_text=True)
    assert 'name="username" value="ingo" readonly autocomplete="username"' in html
    assert 'autocomplete="one-time-code"' in html
    assert 'type="password"' not in html
    username_field = html.split('id="username"', 1)[1].split(">", 1)[0]
    assert "hidden" not in username_field


def test_setup_offers_the_otpauth_link(client):
    with client.session_transaction() as s:
        s["_user_id"] = "1"
    html = client.get("/setup/2fa").get_data(as_text=True)
    assert 'class="otp-link" href="otpauth://totp/IDmate:ingo?' in html
