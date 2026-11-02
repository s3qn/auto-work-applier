"""Create the token the PC worker uses to talk to this server.

Usage: uv run python -m autojob.set_worker_token [--rotate]

The token is printed once and only its hash is stored."""

from __future__ import annotations

import argparse
import sys

from autojob import worker_token
from autojob.config import load_config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Create the PC worker token.")
    parser.add_argument("--rotate", action="store_true", help="replace an existing token")
    args = parser.parse_args(argv)
    config = load_config()
    token = worker_token.generate_token()
    try:
        worker_token.set_worker_token(config, token, rotate=args.rotate)
    except FileExistsError:
        print(f"A worker token already exists at {config.worker_token_path}. Use --rotate to replace it.")
        return 1
    print(f"Worker token (shown once, put it in the worker's config.json):\n{token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
