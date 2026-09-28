# Stack

Python 3.12, managed with uv (`~/.local/bin/uv`, `pyproject.toml`). FastAPI +
uvicorn for the web app, Jinja2 server-rendered templates with htmx (vendored
into `autojob/web/static/`, never loaded from a CDN), SQLite through the
stdlib `sqlite3` module, argon2-cffi for the password hash, Starlette
`SessionMiddleware` for sessions.

Commands:
- `~/.local/bin/uv sync` installs dependencies.
- `./run.sh` starts the dashboard on `127.0.0.1:8040`. Never bind `0.0.0.0`:
  this machine has a public IP, and the dashboard reaches the outside world
  only through the Cloudflare tunnel at https://autojob.sean.build.
- `./run-tests.sh` runs the pytest suite.
- `uv run python -m autojob.set_password` sets the single login password.

Data lives under `AUTOJOB_DATA_DIR` (default `<repo>/data`, gitignored):
the SQLite database, `auth.json`, the session `secret_key`, and uploaded CV
files under `data/cv/`. Tests always point `AUTOJOB_DATA_DIR` at a temp
directory and never touch the real one.

The agents in `.claude/agents/` and the feature-dev scripts still describe
the shadowing-app stack (Expo client, FastAPI backend on :8020, phone QR
tunnel) and need adapting to match auto-job.

# Writing style

Applies to everything a human reads: UI copy, code comments, JSDoc, docs and
commit messages. The `slop-check` skill holds the full rules and a scanner.

- **Never use an em dash.** Use the punctuation the sentence wants: a period for
  two independent clauses, a comma for a dependent one, a colon before a
  definition or list, parentheses around an aside. Never a bare hyphen instead.
<!-- slop-check: disable (the rule below has to quote the phrases it bans) -->
- **No hype filler in user-facing copy.** Cut the openers ("Nice!", "Perfect."),
  the empty enthusiasm ("Let's make it happen", "You're in good company") and the
  teaser asides ("One thing worth knowing…"). State what happens.
<!-- slop-check: enable -->
- **Deliberate exceptions, do not "fix" these:** en dashes in numeric ranges
  (`30–90s`, `0–10`) are correct typography, and trailing ellipses on progress
  labels (`Loading…`) are standard UI.
- **Before finishing a feature**, run the scanner and get a clean exit:
  ```bash
  node ~/.claude/skills/slop-check/scripts/scan.js
  ```
