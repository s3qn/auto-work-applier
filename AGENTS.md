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
every later headless run reuses the cookies. This machine has no display,
so the helper starts the managed display (`autojob.browser.display`, `uv run
python -m autojob.browser.display <start|stop|status>`) and Sean logs in
through the live view at `/browser`. A missing binary prints an `apt-get`
hint instead of failing silently.

`/browser` (dashboard page) shows that display: Xvfb with `-nolisten tcp`,
and x11vnc with `-localhost` on port 5999 using a 0600 password under
`data/vnc/` (read through `-passwdfile`, never passed on a command line).
`/browser/ws` relays bytes from a WebSocket to x11vnc, and checks the
session cookie and the Origin header on the upgrade: the Origin must be
present and its host must equal the request's Host (cloudflared forwards
`autojob.sean.build` unchanged) or be listed in `AUTOJOB_PUBLIC_HOST`
(comma-separated, default `autojob.sean.build`). noVNC is
vendored under `autojob/web/static/novnc/` and never loaded from a CDN. The
page also has the login buttons and the `browser_visible_mode` setting: when
on, apply runs open Chromium on this display instead of headless, and fall
back to headless if the display cannot start.

## Apply pipeline

`autojob/apply/` turns a queued job into a filled (and maybe submitted)
application:
- `router.py` picks a handler by whichever application URL the channel chain
  below (`apply.py`) hands it for the attempt in progress: Greenhouse
  (`boards.greenhouse.io` / `job-boards.greenhouse.io`), Lever
  (`jobs.lever.co`), or Comeet (`comeet.com`, or embedded on a company's own
  domain, detected by the `comeet.co` iframe it injects). `pick_handler`
  never returns `None`: anything none of those three recognize falls back to
  `handlers/generic.py` rather than escalating to `needs_help` without an
  attempt. `pick_handler_by_url` stays the narrower "is this a known ATS"
  check (used by `pick_handler` and by the generic handler itself, to notice
  when following a redirect landed on a domain a specific handler covers).
- `handlers/greenhouse.py`, `handlers/lever.py`, `handlers/comeet.py`: each
  exposes `matches(url)` and `apply(page, answers, cv_path, auto_submit)`,
  built and confirmed against real live postings on 2026-09-28 (see each
  module's docstring for the exact selectors). `fields.py` maps a form
  field's label to an honest answer; a label it doesn't recognize (a
  tool-specific proficiency question, a yes/no eligibility question) is left
  unmapped, and a required one it can't answer escalates to `needs_help`
  instead of guessing. `is_required_label` also recognizes the Hebrew
  "חובה" (mandatory) some company career pages use.
- `handlers/generic.py` is the fallback for every job on an ATS this build
  has no dedicated handler for: company career pages (Cato Networks, Akamai,
  Top Soft, Malam, Matrix, lema.ai, Snatch-Up and others), AllJobs'
  logged-in "send CV" flow (`alljobs.co.il/Search/UploadSingle.aspx`), and
  LinkedIn `/jobs/view` listings. Same 4-argument contract as the
  ATS-specific handlers, so `apply.py`'s screenshot capture, help item
  creation, and status update work unchanged for it too. It:
  1. Detects an apply affordance (a button or link whose text or aria-label
     matches "Apply", "Apply now", "Submit application", or Hebrew phrases
     like "הגש מועמדות" or "שלח קורות חיים") and clicks it if the current
     page looks like a listing rather than a form, following it to the same
     page, a modal, an injected iframe, or a new tab.
  2. If clicking the affordance (or the AllJobs/LinkedIn URL itself) lands
     on a domain Greenhouse, Lever, or Comeet already covers, hands off to
     that handler instead of harvesting generically.
  3. Otherwise harvests every visible input, textarea, select, and
     radio/checkbox group with its label (`<label for=>`, `aria-label`,
     `placeholder`, or the nearest wrapping `<label>`), and sends that field
     list plus Sean's honest answer set (never the CV's inflated wording)
     and the job/company context to `claude -p --model sonnet` (throwaway
     cwd, same rule as the rest of the claude CLI usage) to map each field
     to a value, a skip, or an escalation. A broken claude CLI call falls
     back to the same conservative `fields.match_answer` mapper the
     ATS-specific handlers use, so a bad LLM call never blocks the fill.
  4. Fills what it can, uploads the newest CV PDF into a file input (or the
     generated cover letter, for one whose label mentions a cover letter),
     and, if anything escalated, returns `needs_help` with the specific
     questions while everything else stays filled in.
  5. Returns specific `needs_help` reasons for the failure modes an
     LLM-driven flow actually hits, never a blanket "no handler": "CAPTCHA
     on form" (a `recaptcha`/`hcaptcha`/`captcha`/`turnstile` iframe or
     element), "Account signup required before applying" (two visible
     password fields), "AllJobs login required" / "LinkedIn login required"
     (a visible sign-in affordance on the persistent browser profile), or
     "Could not locate an application form on this page."
  LinkedIn gets its own path inside the same module: Easy Apply is never
  automated. An external apply link (an `APPLY_TEXT_PATTERNS` match whose
  text and aria-label lack "easy apply") is followed and re-detected the
  same way as a company career page; a listing that only offers Easy Apply
  returns `needs_help` with "Apply by hand: <listing url>". A logged-out
  profile escalates "LinkedIn login required" up front. A page counts as a
  form only with at least 3 harvested fields (2 when one is a file input),
  and `harvest_fields` skips fields inside a footer, nav, header, aside or
  search/newsletter/subscribe form, so a newsletter box is never filled.
- `apply.py`'s `requeue_stale_no_handler_jobs(conn)` is a one-time,
  idempotent maintenance function: before `handlers/generic.py` existed, a
  job on an unrecognized ATS was escalated to `needs_help` with reason "No
  apply handler recognizes this ATS..." without ever being attempted. It
  resets those jobs (and only those, matched on that exact reason) back to
  `queued` and deletes the now-stale help items, so the next apply pass
  gives them the attempt they never got. It does not run the apply pipeline
  itself: `auto_submit` still gates whether anything actually gets sent.
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
- `apply.py` (`apply_to_job(job, *, auto_submit)`) is company-site-first:
  `plan_channels(job, conn)` builds an ordered list of channels to try for
  one job, (a) `company_job` (`jobs.company_site_url`, the posting research
  found on the company's own site or ATS), (b) `careers`
  (`companies.careers_url`, only when it looks like a specific posting
  rather than a bare `/careers` listing), (c) `general_cv`
  (`companies.general_cv_url`, a general "send us your CV" form), (d)
  `email` (`companies.cv_email`, dropped when already sent to this job or
  the daily cap is reached), (e) `job_url` (the job's own board URL, the
  only channel a fresh account with no research ever sees, today's
  behaviour). One browser page is opened for the whole chain; a
  `needs_help`/`failed` result on a non-final channel falls through to the
  next one only when the reason looks like the channel itself was unusable
  (CAPTCHA, no form found, a signup wall, a crash); a required question the
  handler can't honestly answer, or a login requirement, escalates
  immediately instead of trying a lesser channel. Every attempt records
  `applications.channel`/`apply_url` as it starts, so the column always
  reflects whichever channel ended the chain; a fallen-through attempt
  appends a `Tried <kind> <target>: <reason>` line to `notes` and keeps its
  own screenshot (`attempt-<n>-<kind>.png`) instead of the terminal
  `form.png`. An application filled by the generic handler records its
  `channel` as `generic:<domain>` (e.g. `generic:catonetworks.com`), not one
  of the fixed ATS names; the `applications.channel` CHECK constraint allows
  that plus `email:<domain>`.
  `release_application(application_id)` is what a "Release" click on the
  dashboard uses to go back and actually submit an already-filled
  `ready_to_submit` application; for a held email it calls
  `mailer.send_draft` directly (no browser, no new LLM call) instead of
  refilling a page.
- `mailer.py` is the email channel: no second LLM call, the body reuses the
  cover letter already generated for the job plus a contact block from
  `profile.answers`. `smtp.gmail.com:587` STARTTLS with a Google App
  Password, credentials in `data/smtp.json` (0600, written by
  `uv run python -m autojob.set_smtp`), never in the database or in code.
  With `auto_submit` off the draft is stored and the application waits as
  `ready_to_submit` for Sean to review and release; with it on, `send_draft`
  sends immediately. A missing or failing SMTP config leaves the
  application `ready_to_submit` with a help item instead of falling through
  to the job's board URL: the email channel is always terminal, whatever
  its outcome. `send_draft` saves the exact bytes sent (`sent.eml`) and a
  readable header-plus-body copy (`sent_email.txt`) as `sent_email` proofs;
  the daily cap counts distinct applications with a `sent_email` proof since
  local midnight.

## PC worker

Jobs that need a logged-in browser are applied to by a Claude agent on
Sean's PC (`worker/`, self-contained, no `autojob` import, copied to the PC;
see `worker/README.md`). The server stays the brain: scan, filter, research,
queue, proofs, dashboard and the email channel.

- **Token.** `uv run python -m autojob.set_worker_token [--rotate]` prints a
  token once and stores only its sha256 in `data/worker_token.json` (0600).
  `autojob/worker_token.py` has `worker_configured` and `verify_worker_token`.
  With no token file nothing below applies: no partition, no routes in use.
- **Partition.** A queued job is a worker job when any page channel from
  `plan_channels` is not a known ATS (`pick_handler_by_url` is None), or it
  waits for a submit session. With a token, `_run_apply_phase` skips worker
  jobs ("N job(s) left for the PC worker") and keeps ATS-only and email-only
  jobs. `remote.is_worker_job` is the one test.
- **Claim and lease.** `POST /api/worker/claim` (`Authorization: Bearer
  <token>`, `autojob/web/worker_routes.py`, outside the login middleware)
  expires stale leases, picks a submit-phase job first and then a fill job
  (per-source daily cap respected), builds the answers and cover letter like
  `apply_to_job`, creates a placeholder application and takes the job with a
  conditional `UPDATE ... WHERE status='queued'` (`db.claim_job`), so the
  scheduled run, the dashboard and the worker never double-take. 204 when
  nothing is claimable. The lease (`jobs.worker_lease_until`,
  `worker_lease_token`, `worker_phase`, `worker_application_id`) lasts 15
  minutes and is extended by `POST /jobs/{id}/heartbeat`. An expired lease
  returns the job to `queued` and fails a fill placeholder ("PC worker lease
  expired without a result."); `requeue_interrupted_applying_jobs` leaves a
  live-leased job alone.
- **Routes.** `GET /api/worker/cv`; `POST /jobs/{id}/attempts` (multipart:
  `lease_token`, `payload` JSON, `screenshots[]` PNG up to 8 MB, 10 files,
  saved as proofs under `data/applications/<id>/`); `POST /jobs/{id}/release`
  (job back to `queued`, an empty placeholder is deleted, otherwise failed).
  The attempt payload carries `index`, `kind`, `target`, `session`, `status`
  (`filled|submitted|needs_help|failed`), `reason_code`, `reason`,
  `final_url`, `fields_filled`, `unanswered_required`, `cv_uploaded`,
  `denied_tool_calls`, `tool_calls`. `remote.REASON_TEXT` turns a
  `reason_code` into the canonical reason text so `FALL_THROUGH_MARKERS` stays
  as is; `unanswered_required` forces `unanswerable`. `filled` becomes
  `ready_to_submit`, `submitted` becomes `sent`.
- **Fall-through and email.** The worker reports one attempt per page
  channel; `remote.record_attempt` uses `_should_fall_through` and answers
  `{"next_index": n}` or `{"done": true, "status": ...}`. For worker jobs
  email is tried last, after every page channel including `job_url`, by the
  server (`_attempt_email_channel`). The channel label is
  `agent:<domain of the final url>`.
- **Release.** Releasing an `agent:` application queues a submit session
  (`worker_phase='submit'`); the next claim returns `phase: submit` with
  `submit_allowed: true` and the one saved channel. The detail page shows
  "Queued for the PC worker." The run log header shows "PC worker last seen"
  or "PC worker: never connected" (setting `worker_last_seen`) once a token
  exists.
- **Pilot.** With a token, a pilot ("Run pilot now", or the 02:00
  `pilot_tonight` claim) applies nothing on the server: `run_scan` creates
  the `runs` row (`mode='pilot'`), and `remote.open_pilot` stores the window
  in setting `worker_pilot` (`run_id`, `cap`, `until` = next 07:00
  Asia/Jerusalem) and leaves the run open. While it is open, a claim hands
  out fills only (any queued job, ATS ones included, no daily cap, no
  submit sessions), at most `pilot_cap` applications, each created with
  `run_id` set and `submit_allowed: false`, so `/pilot` lists them with
  their screenshots. The run finishes (`remote.active_pilot`, checked on
  every claim, attempt, `/pilot` view and run start) once the cap is used
  and nothing is in flight, or at the first check after 07:00. The
  interrupted-run reaper leaves an open window alone. Without a token the
  pilot runs on the server as before.
- **Schedule.** The worker's Task Scheduler task (`install-task.ps1`) runs
  `worker.py --once` daily at 02:05, five minutes after the 02:00 pilot
  timer opens the window, and `max_minutes` 300 ends it near 07:00. Keep
  `max_jobs` at or above the pilot cap (the example config uses 20).

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

`research_company` asks for `website`, `tldr`, `careers_url`,
`job_url_on_company_site`, `general_cv_url`, `cv_email`, and
`cv_email_source_url`, and validates every one before it reaches the
database: a URL is kept only if it's `http(s)://` and its host isn't a job
board (LinkedIn, AllJobs, JobMaster, Drushim, Indeed, Glassdoor); an email
is kept only if it matches a basic address shape and its `source_url`
passed that same URL check, since `cv_email` must be an address literally
seen on a fetched page, never guessed from a pattern like `jobs@domain`.
`autojob.pipeline.research.research_pending` runs this one company at a
time for queued jobs whose company has no `channels_researched_at` stamp
yet, so re-research is bounded and existing companies pick up the new
fields hands-off.

**Every call sets `cwd` to `data/llm-work/`** (created on demand), never
this repo. Running from the repo root would load this repo's own
`CLAUDE.md`, `AGENTS.md`, and `.claude/` hooks into the CLI's context,
which would corrupt the job-screening and company-research prompts. Any
new claude CLI caller must follow the same rule.

The PC worker's claude runs on the PC through the Claude Agent SDK, with the same throwaway-cwd rule (its own `llm-work` folder, never a repo).

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

## Schedule and Refresh

`deploy/autojob-scan.timer` fires `deploy/autojob-scan.service` every day at
08:00 Asia/Jerusalem (`python -m autojob.pipeline.run`, trigger `scheduled`).
`deploy/install-timer.sh` copies both units to `~/.config/systemd/user` and
enables the timer; it is not run automatically. `pipeline/schedule.py` holds
`DAILY_RUN_TIME`, and a test ties it to the timer file. The sidebar's "Next
scan" is computed from it (`describe_next_run`); there is no Targets field.

`data/run.lock` (`pipeline/lock.py`, `flock`) is the one guard shared by the
timer and the dashboard. `run_scan` holds it for the whole run and raises
`RunInProgress` otherwise; the kernel frees it if a process dies. Run, Refresh,
Release and Research buttons check `run_active(config)`. A scheduled run that
finds the lock held exits 0. A login session (the `/browser` "Open login"
buttons and the `autojob.browser.login` CLI) holds the same lock for its whole
length, since it opens the same Chromium profile: a login is refused while a
run is active, and a run started during a login finds the lock held. The
`/browser` page's Stop button is refused while the lock is held too.

`run_scan(mode=)` is `full` (scan, filter, research, apply) or `refresh` (scan
and filter only, the Jobs page "Refresh jobs" button, stored in `runs.mode`).
At the start of every run, unfinished run rows are closed as interrupted, and
jobs stuck in `applying` with no `ready_to_submit`/`sent`/`replied`/`interview`
application go back to `queued`. Scheduling never changes `auto_submit`.

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
