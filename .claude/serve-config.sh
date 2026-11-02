#!/usr/bin/env bash
# FastAPI auto-job dashboard dev server configuration

# Start command - run uvicorn with AUTOJOB_DATA_DIR pointing to data-test
# The script already does: cd $WORKTREE, export PORT, and cd is guaranteed
# We just need to specify the uvicorn command
DEV_START_CMD="/home/sean/.local/bin/uv run uvicorn autojob.web.app:create_app --factory --host 127.0.0.1 --port \$PORT"

# Pattern to match when server is ready
DEV_READY_RE="Uvicorn running on http://127.0.0.1"

# Env var to pass the proxy URL to the dev server (not needed for FastAPI)
DEV_PROXY_ENV=""

# Scheme for scan/dashboard URL
DEV_SCAN_SCHEME="http"

# Export the data dir for the FastAPI app to use
export AUTOJOB_DATA_DIR="data-test"
