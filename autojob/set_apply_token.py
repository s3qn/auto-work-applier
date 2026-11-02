"""Create the token the PC apply session uses to pull the daily queue from to this server.

Usage: uv run python -m autojob.set_apply_token [--rotate]

The token is printed once and only its hash is stored."""

from __future__ import annotations

import argparse
import sys

from autojob import apply_token
from autojob.config import load_config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Create the PC apply token.")
    parser.add_argument("--rotate", action="store_true", help="replace an existing token")
    args = parser.parse_args(argv)
    config = load_config()
    token = apply_token.generate_token()
    try:
        apply_token.set_apply_token(config, token, rotate=args.rotate)
    except FileExistsError:
        print(f"An apply token already exists at {config.apply_token_path}. Use --rotate to replace it.")
        return 1
    print(f"Apply token (shown once, give it to the PC apply session):\n{token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
