#!/usr/bin/env bash
# Starts the auto-job dashboard on 127.0.0.1:8040.
# Never bind 0.0.0.0: this machine has a public IP.
set -euo pipefail
cd "$(dirname "$0")"

UV="${UV:-$HOME/.local/bin/uv}"

exec "$UV" run uvicorn autojob.web.app:create_app --factory --host 127.0.0.1 --port 8040
