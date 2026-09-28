"""Set the single login password.

Usage: uv run python -m autojob.set_password
"""

from __future__ import annotations

import getpass
import sys

from autojob import auth
from autojob.config import load_config


def main() -> int:
    config = load_config()
    password = getpass.getpass("New password: ")
    if not password:
        print("Password cannot be empty.")
        return 1
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords did not match.")
        return 1
    auth.set_password(config, password)
    print(f"Password saved to {config.auth_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
