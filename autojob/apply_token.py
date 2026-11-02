"""The secret the PC apply session presents on `/api/apply-queue`,
`/api/apply-result` and `/api/cv/*`. Separate from the worker token.

Only a sha256 of the token is stored (`data/apply_token.json`, 0600), so a
leaked database or data folder does not leak a usable token. The token
itself is printed once by `autojob.set_apply_token`."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat

from autojob import db
from autojob.config import Config


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def apply_configured(config: Config) -> bool:
    return config.apply_token_path.exists()


def set_apply_token(config: Config, token: str, *, rotate: bool = False) -> None:
    """Store the hash of `token` 0600. Refuses to overwrite unless `rotate`."""
    config.ensure_dirs()
    path = config.apply_token_path
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if rotate else os.O_EXCL)
    fd = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as handle:
        json.dump({"sha256": _hash(token), "created_at": db.now_iso()}, handle)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def verify_apply_token(config: Config, presented: str) -> bool:
    try:
        stored = json.loads(config.apply_token_path.read_text())["sha256"]
    except (OSError, ValueError, KeyError):
        return False
    return hmac.compare_digest(stored, _hash(presented or ""))
