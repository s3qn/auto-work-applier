"""Login, lockout and CSRF tests."""

from __future__ import annotations

from tests.conftest import get_csrf_token, login


def test_login_required_redirects_to_login(client, with_password):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_healthz_and_login_do_not_require_auth(client, with_password):
    assert client.get("/healthz").status_code == 200
    assert client.get("/login").status_code == 200


def test_login_without_password_set_shows_message(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "set_password" in response.text


def test_correct_login_works(client, with_password):
    login(client, with_password)
    response = client.get("/")
    assert response.status_code == 200
    assert "Applications" in response.text


def test_wrong_password_lockout_after_five_tries(client, with_password):
    for _ in range(5):
        token = get_csrf_token(client, "/login")
        response = client.post(
            "/login", data={"password": "not-the-password", "csrf_token": token}, follow_redirects=False
        )
        assert response.status_code == 401

    token = get_csrf_token(client, "/login")
    response = client.post(
        "/login", data={"password": with_password, "csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 429


def test_csrf_rejected_with_bad_token(client, with_password):
    response = client.post(
        "/login", data={"password": with_password, "csrf_token": "wrong-token"}, follow_redirects=False
    )
    assert response.status_code == 403
