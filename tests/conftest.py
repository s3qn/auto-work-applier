"""Shared test fixtures. All tests run against a throwaway data directory."""

from __future__ import annotations

import pytest

from autojob import auth
from autojob.config import Config


@pytest.fixture
def config(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("AUTOJOB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AUTOJOB_SECURE_COOKIES", "false")
    return Config(data_dir=data_dir, secure_cookies=False)


@pytest.fixture
def app(config):
    from autojob.web.app import create_app

    return create_app(config)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture
def password():
    return "correct horse battery staple"


@pytest.fixture
def with_password(config, password):
    auth.set_password(config, password)
    return password


def get_csrf_token(client, path: str = "/login") -> str:
    """Fetch a page and pull the csrf_token hidden field out of it."""
    response = client.get(path)
    marker = 'name="csrf_token" value="'
    start = response.text.index(marker) + len(marker)
    end = response.text.index('"', start)
    return response.text[start:end]


def login(client, password: str) -> None:
    token = get_csrf_token(client, "/login")
    response = client.post("/login", data={"password": password, "csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303, response.text
