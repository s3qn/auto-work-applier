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
the SQLite database, `auth.json`, the session `secret_key`, uploaded CV
files under `data/cv/`, and a throwaway `data/llm-work/` used as the `cwd`
for every claude CLI call (see below). Tests always point
`AUTOJOB_DATA_DIR` at a temp directory and never touch the real one.

The agents in `.claude/agents/` and the feature-dev scripts still describe
the shadowing-app stack (Expo client, FastAPI backend on :8020, phone QR
tunnel) and need adapting to match auto-job.

## Sources

`autojob/sources/` scrapes job postings with httpx + selectolax, no
accounts or logins:
- `linkedin.py`: the public guest job search
  (`jobs-guest/jobs/api/seeMoreJobPostings/search`) and detail
  (`jobs-guest/jobs/api/jobPosting/<id>`) endpoints. Confirmed working with a
  desktop User-Agent and a 2 to 4s delay between requests.
- `jobmaster.py`: the guest listing at `/jobs/q-<slugified keyword>/`
  (**not** a `?freetext=` query string, which the site ignores). Anonymous
  users only see the first page.
- `alljobs.py`: attempts `SearchResultsGuest.aspx?freetext=...`, but the
  site's free-text search actually runs through an ASP.NET postback the
  guest page ignores over a plain GET (every plausible query param name was
  tried and confirmed ignored). This source checks whether the results look
  related to the keyword and raises `SourceError` (logged, run continues)
  when they clearly don't.

Every source module returns normalized job dicts (`source`, `url`, `title`,
`company`, `location`, `description`, `posted_at`, `external_id`) and
raises `autojob.sources.SourceError` on a blocked or broken request, never
a bare exception, so `pipeline/run.py` can log it and keep going with the
other sources.

## claude CLI usage

Filtering (`autojob/llm/filter.py`, haiku) and company research
(`autojob/llm/research.py`, sonnet, with `WebSearch`/`WebFetch` allowed) both
call the local `claude` CLI through `autojob/llm/cli.py`, no API key:
`claude -p --model <model> --output-format json`, prompt on stdin, JSON
parsed out of the response's `"result"` field.

**Every call sets `cwd` to `data/llm-work/`** (created on demand), never
this repo. Running from the repo root would load this repo's own
`CLAUDE.md`, `AGENTS.md`, and `.claude/` hooks into the CLI's context,
which would corrupt the job-screening and company-research prompts. Any
new claude CLI caller must follow the same rule.

On a timeout, non-JSON output, or malformed response, both callers fail
soft instead of raising past the pipeline: `filter_jobs` marks the job
status `"new"` with reason `"filter failed: ..."`, and `research_company`
returns a `tldr` of `"unknown (...)"`.

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
