"""Passkeys: registration needs a fresh sign-in (or the password again), the
login page offers passkeys (button + user name autofill), the ceremony state
is one-time, foreign origins are refused, and the list/delete API only sees
the user's own passkeys. The cryptographic checks themselves come from the
webauthn library (exercised end to end in a browser with a virtual
authenticator, not here)."""

import time

import pytest

pytest.importorskip("webauthn")


@pytest.fixture
def client(app_module, db):
    from werkzeug.security import generate_password_hash
    db.execute("INSERT INTO users (id, username, password_hash, is_admin) VALUES (1, 'ingo', ?, 1)",
               (generate_password_hash("Sup3r!secret"),))
    db.execute("INSERT INTO users (id, username, password_hash, is_admin) VALUES (2, 'gast', 'x', 0)")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


BASE = "http://localhost"


def test_register_needs_fresh_sign_in_or_password(client):
    r = client.post("/api/passkeys/register/begin", json={}, base_url=BASE)
    assert r.status_code == 403 and r.get_json()["reauth"]
    r = client.post("/api/passkeys/register/begin", json={"password": "wrong"}, base_url=BASE)
    assert r.status_code == 403
    r = client.post("/api/passkeys/register/begin", json={"password": "Sup3r!secret"}, base_url=BASE)
    pk = r.get_json()["publicKey"]
    assert pk["rp"]["id"] == "localhost" and pk["user"]["name"] == "ingo"
    assert pk["authenticatorSelection"]["userVerification"] == "required"
    assert pk["authenticatorSelection"]["residentKey"] == "required"
    # now fresh: a second begin needs no password
    assert client.post("/api/passkeys/register/begin", json={}, base_url=BASE).status_code == 200


def test_register_finish_is_one_time_and_rejects_garbage(client):
    with client.session_transaction() as s:
        s["auth_at"] = int(time.time())
    client.post("/api/passkeys/register/begin", json={}, base_url=BASE)
    r = client.post("/api/passkeys/register/finish", json={"credential": {"id": "x"}}, base_url=BASE)
    assert r.status_code == 400
    r = client.post("/api/passkeys/register/finish", json={"credential": {"id": "x"}}, base_url=BASE)
    assert r.status_code == 400 and "abgelaufen" in r.get_json()["error"]


def test_foreign_origin_refused(client):
    r = client.post("/login/passkey/begin", json={}, base_url=BASE,
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 400


def test_login_begin_is_public_and_discoverable(app_module, db):
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        r = c.post("/login/passkey/begin", json={}, base_url=BASE, headers={"Origin": BASE})
        assert r.status_code == 200
        pk = r.get_json()["publicKey"]
        assert pk["rpId"] == "localhost" and pk["userVerification"] == "required"
        assert not pk.get("allowCredentials")
        r = c.post("/login/passkey/finish", json={"credential": {"id": "nope", "response": {}}},
                   base_url=BASE, headers={"Origin": BASE})
        assert r.status_code == 401
        html = c.get("/login", base_url=BASE).get_data(as_text=True)
        assert 'autocomplete="username webauthn"' in html and 'id="pkBtn"' in html


def test_list_and_delete_only_own(client, db):
    db.execute("INSERT INTO passkeys (id, user_id, rp_id, public_key, label) VALUES ('a', 1, 'localhost', x'00', 'iPhone')")
    db.execute("INSERT INTO passkeys (id, user_id, rp_id, public_key, label) VALUES ('b', 2, 'localhost', x'00', 'Gast')")
    db.commit()
    d = client.get("/api/passkeys", base_url=BASE).get_json()
    assert [p["label"] for p in d["passkeys"]] == ["iPhone"] and d["rp_id"] == "localhost"
    assert client.delete("/api/passkeys/b", base_url=BASE).get_json()["ok"] is False
    assert client.delete("/api/passkeys/a", base_url=BASE).get_json()["ok"] is True
    assert db.execute("SELECT COUNT(*) FROM passkeys").fetchone()[0] == 1
