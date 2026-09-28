"""Runtime configuration for auto-job.

Everything reads from environment variables so tests can point at a
throwaway data directory instead of the real one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    data_dir: Path
    secure_cookies: bool

    @property
    def db_path(self) -> Path:
        return self.data_dir / "autojob.sqlite3"

    @property
    def auth_path(self) -> Path:
        return self.data_dir / "auth.json"

    @property
    def secret_key_path(self) -> Path:
        return self.data_dir / "secret_key"

    @property
    def cv_dir(self) -> Path:
        return self.data_dir / "cv"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cv_dir.mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    data_dir = Path(os.environ.get("AUTOJOB_DATA_DIR", str(REPO_ROOT / "data"))).resolve()
    secure_env = os.environ.get("AUTOJOB_SECURE_COOKIES")
    secure_cookies = True if secure_env is None else secure_env.lower() not in ("0", "false", "no")
    return Config(data_dir=data_dir, secure_cookies=secure_cookies)
