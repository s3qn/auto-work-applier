# Stack

Python 3.12, managed with uv (`~/.local/bin/uv`, `pyproject.toml`). FastAPI +
uvicorn for the web app, Jinja2 server-rendered templates with htmx (vendored
into `autojob/web/static/`, never loaded from a CDN), SQLite through the
stdlib `sqlite3` module, argon2-cffi for the password hash, Starlette
`SessionMiddleware` for sessions, Playwright (Chromium) for browser
automation.

Commands:
- `~/.local/bin/uv sync` installs dependencies.
- `~/.local/bin/uv run playwright install chromium` installs the Chromium
  build Playwright drives. Only needs the browser binary, not
  `--with-deps`: this machine already has the system libraries headless
  Chromium needs.
- `./run.sh` starts the dashboard on `127.0.0.1:8040`. Never bind `0.0.0.0`:
  this machine has a public IP, and the dashboard reaches the outside world
  only through the Cloudflare tunnel at https://autojob.sean.build.
- `./run-tests.sh` runs the pytest suite.
- `uv run python -m autojob.set_password` sets the single login password.
- `uv run python -m autojob.browser.login <linkedin|alljobs|jobmaster>` logs
  into a site once by hand (see Browser automation below).

Data lives under `AUTOJOB_DATA_DIR` (default `<repo>/data`, gitignored):
the SQLite database, `auth.json`, the session `secret_key`, uploaded CV
files under `data/cv/`, a throwaway `data/llm-work/` used as the `cwd` for
every claude CLI call (see below), the persistent Chromium profile at
`data/chrome-profile/`, and per-application proof files under
`data/applications/<application_id>/`. Tests always point
`AUTOJOB_DATA_DIR` at a temp directory and never touch the real one, and
never launch a real browser or call the real `claude` CLI either: every
Playwright object and every `run_claude` call in a test is a fake.

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
- `alljobs.py`: the only source driven through a real browser
  (`autojob.browser.context.browser_page`), not httpx. AllJobs' free-text
  search box is a React widget that posts to an undocumented `freetxt`
  query param (not `freetext`, `keyword`, `q`, or any other name tried
  first) and only renders results after its own JavaScript runs: confirmed
  live that a plain `httpx.get(..., params={"freetxt": ...})` still returns
  a card-less page, while loading the same URL in a real (even headless)
  browser and waiting for `div.job-content-top` renders the actual filtered
  results. `_looks_relevant` stays as a safety net in case the frontend's
  search breaks again.

Every source module returns normalized job dicts (`source`, `url`, `title`,
`company`, `location`, `description`, `posted_at`, `external_id`) and
raises `autojob.sources.SourceError` on a blocked or broken request, never
a bare exception, so `pipeline/run.py` can log it and keep going with the
other sources.

## Browser automation

`autojob/browser/context.py` holds `browser_page(config, headless=True)`, a
context manager that yields a Playwright `Page` from a persistent Chromium
profile at `data/chrome-profile/`, so a login on one site survives across
scans and applies. Only one caller may hold the browser at a time
(`BrowserBusyError` if another call is already in progress): Playwright's
sync API isn't safe to share across threads against the same profile.

`autojob/browser/login.py` (`uv run python -m autojob.browser.login <site>`
for `linkedin`, `alljobs`, or `jobmaster`) opens that site's login page
headed, on the same persistent profile, so Sean logs in once by hand and
every later headless run reuses the cookies. This machine has no display by
default: with no `DISPLAY` set, the helper re-launches itself under
`xvfb-run` (a virtual framebuffer) so Chromium still runs headed as far as
Playwright is concerned. If `xvfb-run` isn't installed it prints
`sudo apt-get install -y xvfb` instead of failing silently.

## Apply pipeline

`autojob/apply/` turns a queued job into a filled (and maybe submitted)
application:
- `router.py` picks a handler by the application URL (`companies.careers_url`
  from research when present, otherwise the job's own URL): Greenhouse
  (`boards.greenhouse.io` / `job-boards.greenhouse.io`), Lever
  (`jobs.lever.co`), or Comeet (`comeet.com`, or embedded on a company's own
  domain, detected by the `comeet.co` iframe it injects). Anything else
  becomes a `needs_help` item rather than a guess: email applications,
  Google Forms, Workday, and a generic LLM-driven filler for unknown ATS
  forms are deliberately left for a later build.
- `handlers/greenhouse.py`, `handlers/lever.py`, `handlers/comeet.py`: each
  exposes `matches(url)` and `apply(page, answers, cv_path, auto_submit)`,
  built and confirmed against real live postings on 2026-09-28 (see each
  module's docstring for the exact selectors). `fields.py` maps a form
  field's label to an honest answer; a label it doesn't recognize (a
  tool-specific proficiency question, a yes/no eligibility question) is left
  unmapped, and a required one it can't answer escalates to `needs_help`
  instead of guessing.
- `answers.py` builds the answer set: profile facts from settings key
  `profile.answers`, the reusable `answer_bank` setting for recurring
  questions (years of experience, visa sponsorship, relocation, salary),
  and a per-job cover letter plus screening answers from `claude -p --model
  sonnet` (throwaway cwd, same rule as the rest of the claude CLI usage).
  Every prompt carries Sean's honest profile (JUNIOR penetration tester:
  HTB CPTS, TCM PNPT, eJPT, 100+ HTB/THM machines, a home AD lab, close to
  no professional pentest experience) and an explicit instruction not to
  fabricate experience. `years_experience` in the answer bank always says 0
  to 1 years: this is a hard rule, not a suggestion to the model.
- `proof.py` saves a full-page screenshot before any submit click, and,
  after a real submit, a confirmation screenshot, its HTML, and a PDF
  (best-effort) under `data/applications/<application_id>/`, with one
  `proofs` row per file. Proof files are served only through the
  authenticated route `/applications/<id>/proofs/<proof_id>`, never a
  static mount.
- `apply.py` (`apply_to_job(job, *, auto_submit)`) ties it together, and
  `release_application(application_id)` is what a "Release" click on the
  dashboard uses to go back and actually submit an already-filled
  `ready_to_submit` application.

**Safety default: `auto_submit` (settings key, default `false`).** When
off, a handler fills the whole form, takes its screenshot, and stops before
the final submit click; the application waits as `ready_to_submit` for Sean
to release or discard from the dashboard. When on, it submits and captures
the confirmation. This is the one toggle (on the Targets page) that turns
on fully hands-off applying later. `pipeline/run.py`'s apply phase and
every "Apply now" button read this same setting, so it's the single place
that controls it.

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

## Live view

`run_scan` (`autojob/pipeline/run.py`) runs in a background thread; the
dashboard streams what it is doing over Server-Sent Events, which need an
async route. The two sides never share state directly, only a `run_events`
table (`autojob/db.py`): one row per granular action (`run_id`, `ts`,
`phase` one of scan/filter/research/apply/system, `kind` one of
info/score/progress/error, `message`, `detail` JSON). `autojob/pipeline/
events.py` holds `emit(conn, run_id, phase, kind, message, detail=None)`,
called from `run.py` and from inside `llm/filter.py` and `apply/apply.py`
(both take optional `conn`/`run_id` parameters, `None` outside a pipeline
run, in which case they stay silent). `emit()` with `kind="progress"` also
persists `detail` onto the run row (`runs.progress`, a JSON snapshot of
every phase's done/total plus the running found/filtered/applied counts),
so a reload with no new events still shows the latest bars.
`events.bump_progress()` is the read-modify-write helper every phase uses
to update that snapshot and emit the `progress` event in one call.

`/runs/{id}/live` is a dedicated dark terminal page (not built on the
normal sidebar layout), connecting to `/runs/{id}/stream`
(`StreamingResponse`, `text/event-stream`) which replays every existing
`run_events` row for that run and then polls for new ones every ~500ms
until `finished_at` is set, at which point it sends a final synthetic
`"finished"` event and closes. `/runs/{id}/events?since=<id>` is the same
replay-since-a-cursor query as plain JSON, used as a 1s-poll fallback if
`EventSource` errors. Both routes sit behind the same login requirement as
every other page. The run detail page's stat tiles and per-phase progress
bars (`partials/run_progress_panel.html`) update the same way the run log
already did: htmx polling `/runs/{id}/progress` every 2s, reading
`runs.progress`, no SSE needed there.

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
