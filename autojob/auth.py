"""Password auth, session secret, login lockout and CSRF helpers."""

from __future__ import annotations

import json
import os
import secrets
import stat
import time
from collections import defaultdict
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from starlette.requests import Request

from autojob.config import Config

_hasher = PasswordHasher()

LOCKOUT_MAX_ATTEMPTS = 5
LOCKOUT_WINDOW_SECONDS = 15 * 60


def password_is_set(config: Config) -> bool:
    return config.auth_path.exists()


def set_password(config: Config, password: str) -> None:
    config.ensure_dirs()
    password_hash = _hasher.hash(password)
    config.auth_path.write_text(json.dumps({"password_hash": password_hash}))
    os.chmod(config.auth_path, stat.S_IRUSR | stat.S_IWUSR)


def verify_password(config: Config, password: str) -> bool:
    if not password_is_set(config):
        return False
    data = json.loads(config.auth_path.read_text())
    try:
        _hasher.verify(data["password_hash"], password)
    except VerifyMismatchError:
        return False
    return True


def load_or_create_secret_key(config: Config) -> str:
    config.ensure_dirs()
    path = config.secret_key_path
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_hex(32)
    path.write_text(key)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return key


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("CF-Connecting-IP")
    if forwarded:
        return forwarded
    if request.client:
        return request.client.host
    return "unknown"


@dataclass
class LoginLimiter:
    """Tracks failed login attempts per client IP in memory."""

    _attempts: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    def _prune(self, ip: str) -> list[float]:
        cutoff = time.time() - LOCKOUT_WINDOW_SECONDS
        recent = [t for t in self._attempts[ip] if t >= cutoff]
        self._attempts[ip] = recent
        return recent

    def is_locked(self, ip: str) -> bool:
        return len(self._prune(ip)) >= LOCKOUT_MAX_ATTEMPTS

    def record_failure(self, ip: str) -> None:
        self._prune(ip)
        self._attempts[ip].append(time.time())

    def reset(self, ip: str) -> None:
        self._attempts.pop(ip, None)


def generate_csrf_token() -> str:
    return secrets.token_hex(16)


def csrf_token_for_session(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = generate_csrf_token()
        request.session["csrf_token"] = token
    return token


def verify_csrf(request: Request, submitted_token: str | None) -> bool:
    expected = request.session.get("csrf_token")
    if not expected or not submitted_token:
        return False
    return secrets.compare_digest(expected, submitted_token)
