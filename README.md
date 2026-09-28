# auto-job

A personal bot that scans job sites twice a day, researches companies,
applies automatically, and shows everything on a password-protected
dashboard at https://autojob.sean.build.

This is the foundation step: database, dashboard, login, and a run
skeleton. Scraping and applying are not built yet; "Run now" records a run
with the log "No sources connected yet."

## Setup

```bash
~/.local/bin/uv sync
uv run python -m autojob.set_password   # sets the login password
./run.sh                                 # starts the dashboard on 127.0.0.1:8040
```

Reach the dashboard through the Cloudflare tunnel at
https://autojob.sean.build, never by binding a public interface directly.

## Tests

```bash
./run-tests.sh
```

Tests always run against a temporary `AUTOJOB_DATA_DIR` and never touch the
real data directory.

## Data

Everything lives under `AUTOJOB_DATA_DIR` (default `<repo>/data`, already
gitignored): the SQLite database, `auth.json` (the password hash), the
session `secret_key`, and uploaded CV files under `data/cv/`.

## Deployment files

`deploy/` holds systemd user unit files for running the dashboard and the
twice-daily scan as services. They are not installed by anything in this
repo; copy them into `~/.config/systemd/user/` and enable them by hand when
ready.
