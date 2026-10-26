"""Passkeys (WebAuthn): sign in without a password.

User verification (Face ID / Touch ID / device PIN) is required, so a passkey
counts as both factors and skips the TOTP step — same rule as the Stunden app.
Passkeys are discoverable: the login page needs no user name, the browser
offers the passkeys it holds for this host (also in the user name autofill).

The relying party is the host the page was served from (behind the reverse
proxy via ProxyFix): a passkey belongs to exactly that host. Adding a passkey
needs a fresh sign-in (15 min) or the password again, so a hijacked session
cannot plant its own key.
"""

import json
import secrets
import time

from flask import Blueprint, jsonify, request, session
from flask_login import current_user, login_required, login_user

from app import (_TOTP_AVAILABLE, _is_rate_limited, _login_attempts, _record_attempt,
                 _safe_next, _translations, _user_from_row, check_password_hash, get_db,
                 get_language, log, pyotp)

try:
    from webauthn import (generate_authentication_options, generate_registration_options,
                          options_to_json, verify_authentication_response,
                          verify_registration_response)
    from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
    from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                          PublicKeyCredentialDescriptor,
                                          ResidentKeyRequirement,
                                          UserVerificationRequirement)
    WEBAUTHN_AVAILABLE = True
except ImportError:  # optional dependency
    WEBAUTHN_AVAILABLE = False

passkeys_bp = Blueprint("passkeys", __name__)

CEREMONY_SECONDS = 300      # a begun registration / login is valid 5 min
FRESH_LOGIN_SECONDS = 900   # adding a passkey without the password again
LABEL_MAX = 60


def _t():
    return _translations.get(get_language(), _translations["DE"])


def mark_fresh_login():
    """Called on every completed sign-in (password + 2FA, or passkey)."""
    session["auth_at"] = int(time.time())


def _rp():
    """(rp_id, origin) of this request, or (None, None) when the browser's
    Origin does not match the host the page was served from."""
    origin = f"{request.scheme}://{request.host}"
    sent = request.headers.get("Origin")
    if sent and sent.rstrip("/") != origin:
        return None, None
    return request.host.split(":")[0], origin


def _handle(db, uid):
    row = db.execute("SELECT webauthn_handle FROM users WHERE id = ?", (uid,)).fetchone()
    if row and row["webauthn_handle"]:
        return row["webauthn_handle"]
    handle = bytes_to_base64url(secrets.token_bytes(32))
    db.execute("UPDATE users SET webauthn_handle = ? WHERE id = ?", (handle, uid))
    db.commit()
    return handle


def _take(key):
    """One-time ceremony state from the session (challenge, rp, expiry)."""
    st = session.pop(key, None)
    if not st or st.get("exp", 0) < time.time():
        return None
    return st


def _fail(msg, code=400, **extra):
    return jsonify({"error": msg, **extra}), code


# ── Registration (profile) ────────────────────────────────────

@passkeys_bp.route("/api/passkeys")
@login_required
def list_passkeys():
    db = get_db()
    rows = db.execute(
        "SELECT id, rp_id, label, created_at, last_used FROM passkeys WHERE user_id = ? ORDER BY created_at",
        (current_user.id,)).fetchall()
    db.close()
    return jsonify({"available": WEBAUTHN_AVAILABLE, "rp_id": _rp()[0],
                    "passkeys": [dict(r) for r in rows]})


@passkeys_bp.route("/api/passkeys/register/begin", methods=["POST"])
@login_required
def register_begin():
    t = _t()
    if not WEBAUTHN_AVAILABLE:
        return _fail(t["passkey_unavailable"], 501)
    rp_id, origin = _rp()
    if not rp_id:
        return _fail(t["passkey_wrong_origin"])
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        row = db.execute("SELECT * FROM users WHERE id = ?", (current_user.id,)).fetchone()
        if time.time() - session.get("auth_at", 0) > FRESH_LOGIN_SECONDS:
            pw, code = data.get("password") or "", (data.get("code") or "").strip()
            ok = bool(pw) and check_password_hash(row["password_hash"], pw)
            if not ok and code and _TOTP_AVAILABLE and row["totp_secret"]:
                ok = pyotp.TOTP(row["totp_secret"]).verify(code, valid_window=1)
            if not ok:
                return _fail(t["passkey_reauth"], 403, reauth=True)
            mark_fresh_login()
        handle = _handle(db, current_user.id)
        existing = db.execute("SELECT id FROM passkeys WHERE user_id = ? AND rp_id = ?",
                              (current_user.id, rp_id)).fetchall()
    finally:
        db.close()
    opts = generate_registration_options(
        rp_id=rp_id, rp_name="IDmate",
        user_id=base64url_to_bytes(handle), user_name=current_user.username,
        user_display_name=current_user.username,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["id"])) for r in existing],
    )
    session["pk_reg"] = {"challenge": bytes_to_base64url(opts.challenge), "rp": rp_id,
                         "origin": origin, "uid": current_user.id,
                         "exp": time.time() + CEREMONY_SECONDS}
    return jsonify({"publicKey": json.loads(options_to_json(opts))})


@passkeys_bp.route("/api/passkeys/register/finish", methods=["POST"])
@login_required
def register_finish():
    t = _t()
    st = _take("pk_reg")
    rp_id, origin = _rp()
    if not st or st["uid"] != current_user.id or st["rp"] != rp_id:
        return _fail(t["passkey_expired"])
    data = request.get_json(silent=True) or {}
    try:
        v = verify_registration_response(
            credential=data.get("credential"), expected_challenge=base64url_to_bytes(st["challenge"]),
            expected_origin=origin, expected_rp_id=rp_id, require_user_verification=True)
    except Exception as e:
        log.info("passkey registration rejected for user %s: %s", current_user.id, e)
        return _fail(t["passkey_rejected"])
    cred_id = bytes_to_base64url(v.credential_id)
    label = (data.get("label") or "").strip()[:LABEL_MAX] or "Passkey"
    transports = (data.get("credential") or {}).get("response", {}).get("transports") or []
    db = get_db()
    try:
        db.execute(
            """INSERT INTO passkeys (id, user_id, rp_id, public_key, sign_count, transports, label)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cred_id, current_user.id, rp_id, v.credential_public_key, v.sign_count,
             json.dumps(transports), label))
        db.commit()
    except Exception:
        return _fail(t["passkey_exists"], 409)
    finally:
        db.close()
    log.info("passkey added for user %s (%s, %s)", current_user.id, label, rp_id)
    return jsonify({"ok": True, "id": cred_id, "label": label})


@passkeys_bp.route("/api/passkeys/<path:cred_id>", methods=["DELETE"])
@login_required
def delete_passkey(cred_id):
    db = get_db()
    n = db.execute("DELETE FROM passkeys WHERE id = ? AND user_id = ?",
                   (cred_id, current_user.id)).rowcount
    db.commit()
    db.close()
    if n:
        log.info("passkey removed for user %s", current_user.id)
    return jsonify({"ok": bool(n)})


# ── Sign-in ───────────────────────────────────────────────────

@passkeys_bp.route("/login/passkey/begin", methods=["POST"])
def login_begin():
    t = _t()
    if not WEBAUTHN_AVAILABLE:
        return _fail(t["passkey_unavailable"], 501)
    rp_id, origin = _rp()
    if not rp_id:
        return _fail(t["passkey_wrong_origin"])
    if _is_rate_limited(request.remote_addr):
        return _fail(t["login_too_many_attempts"].format(s=60), 429)
    # Discoverable passkeys: no user name, the browser lists what it holds.
    opts = generate_authentication_options(
        rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED)
    session["pk_auth"] = {"challenge": bytes_to_base64url(opts.challenge), "rp": rp_id,
                          "origin": origin, "exp": time.time() + CEREMONY_SECONDS}
    return jsonify({"publicKey": json.loads(options_to_json(opts))})


@passkeys_bp.route("/login/passkey/finish", methods=["POST"])
def login_finish():
    t = _t()
    ip = request.remote_addr
    if _is_rate_limited(ip):
        return _fail(t["login_too_many_attempts"].format(s=60), 429)
    st = _take("pk_auth")
    rp_id, origin = _rp()
    if not st or st["rp"] != rp_id:
        return _fail(t["passkey_expired"])
    data = request.get_json(silent=True) or {}
    cred = data.get("credential") or {}
    db = get_db()
    try:
        pk = db.execute("SELECT * FROM passkeys WHERE id = ? AND rp_id = ?",
                        (cred.get("id") or "", rp_id)).fetchone()
        user = db.execute("SELECT * FROM users WHERE id = ?", (pk["user_id"],)).fetchone() if pk else None
        handle = (cred.get("response") or {}).get("userHandle")
        if not pk or not user or (handle and handle != user["webauthn_handle"]):
            _record_attempt(ip)
            return _fail(t["passkey_unknown"], 401)
        try:
            v = verify_authentication_response(
                credential=cred, expected_challenge=base64url_to_bytes(st["challenge"]),
                expected_rp_id=rp_id, expected_origin=origin,
                credential_public_key=pk["public_key"], credential_current_sign_count=pk["sign_count"],
                require_user_verification=True)
        except Exception as e:
            log.info("passkey sign-in rejected for user %s: %s", user["id"], e)
            _record_attempt(ip)
            return _fail(t["passkey_rejected"], 401)
        db.execute("UPDATE passkeys SET sign_count = ?, last_used = datetime('now') WHERE id = ?",
                   (v.new_sign_count, pk["id"]))
        db.commit()
    finally:
        db.close()
    _login_attempts.pop(ip, None)
    # Passkey with user verification = both factors: no TOTP step.
    session.pop("pending_user_id", None)
    login_user(_user_from_row(user), remember=True)
    mark_fresh_login()
    log.info("passkey sign-in: user %s", user["id"])
    return jsonify({"ok": True, "next": _safe_next(data.get("next") or "/")})
