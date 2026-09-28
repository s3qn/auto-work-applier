#!/usr/bin/env bash
# Runs the test suite. Tests always use a temp data directory, never the real one.
set -euo pipefail
cd "$(dirname "$0")"

UV="${UV:-$HOME/.local/bin/uv}"

exec "$UV" run pytest "$@"
