"""Accounts: signup validation, login, per-email throttling, YNAB settings."""
from tests.test_app_flow import EMAIL, PASSWORD, connect_ynab, get_csrf, login, signup


def logout(client, token):
    response = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303


def test_signup_logs_in_and_login_works_after_logout(app_client):
    token = signup(app_client)
    assert app_client.get("/conversions").status_code == 200

    logout(app_client, token)
    assert app_client.get("/conversions", follow_redirects=False).status_code == 303

    token = get_csrf(app_client)
    response = app_client.post(
        "/login",
        data={"email": EMAIL, "password": PASSWORD, "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert app_client.get("/conversions").status_code == 200


def test_privacy_page_is_public(app_client):
    response = app_client.get("/privacy")
    assert response.status_code == 200
    assert "Privacy Policy" in response.text
    # Explains handling of YNAB-API data, per the OAuth App Review.
    assert "YNAB API" in response.text
    # Data-deletion/contact requests need an actually reachable address.
    assert 'href="mailto:' in response.text


def test_footer_has_trademark_disclaimer(app_client):
    response = app_client.get("/privacy")
    # Normalize whitespace so HTML line-wrapping doesn't break the match — the
    # exact YNAB-required wording matters, not how it's wrapped in the template.
    text = " ".join(response.text.split())
    assert "not affiliated, associated, or in any way officially connected" in text
    assert "registered trademarks of YNAB" in text
    assert 'href="/privacy"' in response.text


def test_signup_validation(app_client):
    token = get_csrf(app_client)

    def try_signup(email, password):
        return app_client.post(
            "/signup", data={"email": email, "password": password, "csrf_token": token}
        )

    assert try_signup("not-an-email", PASSWORD).status_code == 400
    response = try_signup(EMAIL, "short")
    assert response.status_code == 400
    assert "at least 8 characters" in response.text


def test_signup_duplicate_email_rejected_case_insensitively(app_client):
    token = signup(app_client)
    logout(app_client, token)
    token = get_csrf(app_client)
    response = app_client.post(
        "/signup",
        data={"email": EMAIL.upper(), "password": "other-password", "csrf_token": token},
    )
    assert response.status_code == 409
    assert "already registered" in response.text


def test_wrong_password_and_unknown_email_rejected(app_client):
    token = signup(app_client)
    logout(app_client, token)
    token = get_csrf(app_client)
    response = app_client.post(
        "/login", data={"email": EMAIL, "password": "wrong-password", "csrf_token": token}
    )
    assert response.status_code == 401
    assert "Incorrect email or password" in response.text
    # unknown email gets the same answer (no account-probing oracle)
    response = app_client.post(
        "/login", data={"email": "nobody@example.com", "password": PASSWORD, "csrf_token": token}
    )
    assert response.status_code == 401
    assert "Incorrect email or password" in response.text


def test_non_ascii_password_rejected_cleanly(app_client):
    token = signup(app_client)
    logout(app_client, token)
    token = get_csrf(app_client)
    response = app_client.post(
        "/login", data={"email": EMAIL, "password": "pässwörd", "csrf_token": token}
    )
    assert response.status_code == 401


def test_throttle_delay_never_overflows():
    import time as time_mod

    import app.auth as auth

    auth._reset_throttle()
    auth._throttle["x@example.com"] = {"failures": 5000, "locked_until": 0.0}
    auth._record_login_failure("x@example.com")  # must not raise OverflowError
    remaining = auth._throttle["x@example.com"]["locked_until"] - time_mod.monotonic()
    assert remaining <= auth.LOCKOUT_MAX_SECONDS + 1
    auth._reset_throttle()


def test_login_brute_force_throttled_per_email(app_client):
    import app.auth as auth

    token = signup(app_client)
    logout(app_client, token)
    token = get_csrf(app_client)
    for _ in range(auth.LOCKOUT_THRESHOLD):
        response = app_client.post(
            "/login", data={"email": EMAIL, "password": "nope", "csrf_token": token}
        )
        assert response.status_code == 401
    # locked out now — even the correct password is refused until the delay passes
    response = app_client.post(
        "/login", data={"email": EMAIL, "password": PASSWORD, "csrf_token": token}
    )
    assert response.status_code == 429
    assert "Too many failed attempts" in response.text
    # other emails are unaffected by this email's lockout
    response = app_client.post(
        "/login", data={"email": "other@example.com", "password": "nope", "csrf_token": token}
    )
    assert response.status_code == 401
    # once the lockout expires, the correct password works and resets the counter
    auth._throttle[EMAIL]["locked_until"] = 0.0
    response = app_client.post(
        "/login",
        data={"email": EMAIL, "password": PASSWORD, "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert EMAIL not in auth._throttle


def test_settings_connect_and_disconnect(app_client):
    token = signup(app_client)

    page = app_client.get("/settings")
    assert page.status_code == 200
    assert "Not connected" in page.text
    # PAT entry is gone, and OAuth isn't configured in tests, so nothing to click.
    assert "personal access token" not in page.text.lower()
    assert "/oauth/ynab/start" not in page.text

    connect_ynab(app_client, token)  # seeds an OAuth connection directly
    page = app_client.get("/settings?ok=connected")
    assert "YNAB connected." in page.text
    assert "via OAuth" in page.text
    # the token itself is never rendered back
    assert "test-token" not in page.text

    response = app_client.post(
        "/settings/ynab/disconnect", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    page = app_client.get("/settings?ok=disconnected")
    assert "Not connected" in page.text
    assert "YNAB disconnected" in page.text


def test_settings_shows_legacy_connection_needs_reauth(app_client):
    """A pre-OAuth-only connection (no refresh_token) must not claim 'via OAuth'."""
    from app.config import get_settings
    from app.connections import ConnectionStore
    from app.users import UserStore, normalize_email

    token = signup(app_client)
    data_dir = get_settings().data_dir
    user = UserStore(data_dir).get_by_email(normalize_email(EMAIL))
    ConnectionStore(data_dir)._upsert(user.id, "pat", "legacy-token", None, None)

    page = app_client.get("/settings")
    assert "Connected to YNAB" in page.text
    assert "via OAuth" not in page.text
    assert "needs to be re-authorized" in page.text
    # disconnect still works for a legacy row
    response = app_client.post(
        "/settings/ynab/disconnect", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303


def test_login_page_redirects_when_already_logged_in(app_client):
    login(app_client)
    for path in ("/login", "/signup"):
        response = app_client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/conversions"


def test_stale_session_user_is_logged_out(app_client, tmp_path):
    """A session pointing at a deleted user must fall back to /login."""
    signup(app_client)
    from app import db

    conn = db.connect(tmp_path)
    try:
        conn.execute("DELETE FROM users")
        conn.commit()
    finally:
        conn.close()
    response = app_client.get("/conversions", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_https_only_deployment_sends_hsts_and_secure_cookie(app_client_factory):
    from fastapi.testclient import TestClient

    with app_client_factory(SESSION_HTTPS_ONLY="true") as client:
        # HSTS is sent on the HTTPS deployment
        resp = client.get("/login")
        assert resp.headers["Strict-Transport-Security"].startswith("max-age=")

        # Over an actual https request the session cookie is marked Secure
        # (Starlette only emits the cookie over TLS when https_only is set).
        https = TestClient(client.app, base_url="https://testserver")
        resp = https.post(
            "/signup",
            data={"email": "s@example.com", "password": "password123",
                  "csrf_token": get_csrf(https)},
            follow_redirects=False,
        )
        assert any("secure" in c.lower() for c in resp.headers.get_list("set-cookie"))


def test_http_dev_has_no_hsts(app_client):
    assert "Strict-Transport-Security" not in app_client.get("/login").headers
