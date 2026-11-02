# Plan: overnight pilot (apply-only dry run with morning review)

## Context
Sean wants the real apply success rate before enabling auto_submit. Builds on the pending branch
`daily-run-and-refresh-button` (flock lock in `autojob/pipeline/lock.py`, `run_scan(mode=...)`,
`runs.mode`, `deploy/autojob-scan.timer`, `deploy/install-timer.sh`). That branch must be merged first.

## Decisions made for you
1. Mode: add `"pilot"` to `RUN_MODES` in `autojob/pipeline/run.py`. It skips scan, filter and research and
   runs only the apply phase. No second apply loop.
2. auto_submit is forced `False` in pilot mode, ignoring the setting. Enforced in code, not by a flag the caller passes.
3. Cap: default 20 jobs, setting `pilot_cap` (editable on the pilot page, 1 to 100). Implemented as a `limit`
   parameter on `_run_apply_phase` (stops after `limit` jobs attempted). The per-source daily cap is not applied to a pilot,
   because nothing is sent. The email channel only stores drafts with auto_submit off, so nothing leaves the machine.
4. Overnight: second systemd timer `autojob-pilot.timer` at 02:00 Asia/Jerusalem plus a dashboard toggle
   `pilot_tonight` (setting). The 02:00 service runs `python -m autojob.pipeline.run --pilot`, which does nothing
   unless the toggle is on, and clears the toggle when it starts (one night per click, never repeats by surprise).
   Chosen over teaching the 08:00 timer: 08:00 is not overnight, and a second tiny timer is the simplest thing that works.
5. "Run pilot now" button on the pilot page uses the same thread plus `run_lock` pattern as `/runs/refresh`.
6. Verdicts: new columns on `applications`: `run_id INTEGER` (the run that attempted it) and `pilot_verdict TEXT`
   (`'right'`, `'wrong'` or NULL, CHECK-free, validated in code). Additive `ALTER TABLE` in the migration block of `db.py`
   next to the `runs.mode` one. Verdict is keyed on the application, not on a channel list, so new channels and
   statuses from the later agent-apply feature appear with no code change.
7. Success rate per channel = right / (right + wrong) among reviewed rows, shown with counts, plus an unreviewed count.
   Channel grouping key: the channel string with the domain part dropped (`generic:foo.com` groups as `generic`,
   `email:x.com` as `email`). Statuses are shown raw, never matched against a fixed list.
8. Empty state: a fresh account with nothing queued or no pilot yet shows the pilot page with the Run button, the tonight
   toggle, and text "No pilot has run yet. It tries up to 20 queued jobs without submitting anything." The page is in the
   sidebar nav, so it is discoverable. If the queue is empty the button is disabled with the reason stated.
9. Nothing external is imported or picked, so no browse or search UI is needed.
10. No visual effects. Reuse existing card, badge and button classes in `style.css`; verdict buttons use the existing
    success and danger button styles, selected verdict shown as a badge.

## Design
- Pilot run scope: `run_scan(trigger, mode="pilot")` creates the run row (mode stored), `_run_scan_locked` branches:
  `if mode == "pilot": apply only, auto_submit=False, limit=pilot_cap`, skipping the scan loop (put the early branch before
  source searching). `apply_to_job(..., run_id=run_id)` must record `applications.run_id` (extend `db.create_application`
  and its call in `autojob/apply/apply.py` to take `run_id`).
- Filled-field summary: derived from `answers_json` (screening answers) plus `notes` (reason, "Tried ..." lines),
  rendered as "N answers filled" with a collapsible list of question and value. No new capture code.
- Review page `GET /pilot`: lists applications from the latest pilot run (`?run=<id>` for older ones; select of past
  pilot runs). Each row: job title and company, channel, status, reason (`notes`), filled summary, screenshot thumbnails
  (`proofs` kind `screenshot`, linked to `/applications/{id}/proofs/{proof_id}`, no new file route), link to
  `/applications/{id}`, verdict buttons. Top: summary table per channel group.
- `POST /pilot/{application_id}/verdict` (form: `verdict` in right/wrong/clear, csrf, hx-post swap of the row partial
  `partials/pilot_row.html`; falls back to redirect without htmx). 400 on any other value.
- `POST /pilot/run` (button), `POST /pilot/tonight` (toggle), `POST /pilot/cap`.
- `schedule.py`: add `PILOT_RUN_TIME = time(2, 0)` with a comment that `deploy/autojob-pilot.timer` must match.

## Touches
- `autojob/pipeline/run.py`: `RUN_MODES`, `_run_scan_locked` (pilot branch), `_run_apply_phase` (`limit` param), `main` (`--pilot` flag).
- `autojob/pipeline/schedule.py`: pilot time constant.
- `autojob/db.py`: migration for `applications.run_id`, `applications.pilot_verdict`; helpers `list_pilot_applications(conn, run_id)`,
  `set_pilot_verdict`, `latest_pilot_run_id`, `pilot_summary(conn, run_id)`; `create_application` gets `run_id`.
- `autojob/apply/apply.py`: pass `run_id` into `create_application` (near line 230).
- `autojob/web/routes.py`: new pilot routes after the `/runs/refresh` block (reuse `_run_scan_and_release`, `run_lock`).
- `autojob/web/templates/pilot.html`, `partials/pilot_row.html` (new); `base.html` nav entry; `style.css` small additions.
- `deploy/autojob-pilot.service`, `deploy/autojob-pilot.timer` (new), `deploy/install-timer.sh` (copy and enable both).
- `AGENTS.md` pilot paragraph is for the scribe, not a task.

## Tasks
### Task 1 (Bob A): backend, pilot mode, timer
Files: `run.py`, `schedule.py`, `db.py`, `apply.py`, `deploy/*`.
Behaviour: as in Design. Pilot never calls scan, filter or research; `apply_to_job` always gets `auto_submit=False`.
`main(["--pilot"])` returns 0 without a run when `pilot_tonight` is off, and returns 0 with the "skipping" message on `RunInProgress`.
Tests (`tests/test_pipeline.py`, `tests/test_db.py`; fakes for `apply_to_job` and sources, temp `AUTOJOB_DATA_DIR`):
- pilot with setting `auto_submit=True` still calls `apply_to_job(auto_submit=False)`;
- scan/filter/research fakes asserted never called;
- cap of 3 with 5 queued attempts exactly 3;
- `runs.mode == "pilot"`; `applications.run_id` set; migration idempotent on an old DB;
- `main(["--pilot"])` no-op with toggle off, runs and clears toggle when on; second concurrent run raises/skips via lock.
Verify: `npx tsc` not applicable (Python only); `./run-tests.sh`.
### Task 2 (Bob B, after Task 1 merges into its branch): review page
Files: `routes.py`, templates, `style.css`, `base.html`, tests in `tests/test_pages.py`.
Tests: `/pilot` empty state renders; seeded pilot run with three applications (comeet, `generic:x.com`, `email:y.com`) shows
rows, proof links point to the existing route; verdict POST sets and clears, bad value 400, missing csrf 403; summary
math (2 right, 1 wrong gives 67 percent for the group, unreviewed counted); run button refuses with `busy` when the lock is held;
unauthenticated GET redirects to login; tonight toggle and cap persist.
Verify: `./run-tests.sh`; `node ~/.claude/skills/slop-check/scripts/scan.js` clean.

## Verification on the phone
1. Open the dashboard, tap Pilot in the nav: empty state text, cap 20, toggle off.
2. Tap "Run pilot now": the live view shows only the apply phase; nothing is submitted (statuses end ready_to_submit, needs_help or failed).
3. Reload /pilot: each application shows channel, status, reason, screenshots that open full size; tap looks right or wrong and the summary updates.
4. Turn on "Run pilot tonight", then confirm `systemctl --user list-timers` shows autojob-pilot.timer at 02:00; next morning the toggle is off and a pilot run exists.
5. Settings: auto_submit still off or unchanged after the pilot.

## Risks
Jobs the pilot fills leave `queued`, so the 08:00 full run will not retry them; the pilot page should say so, and a later "requeue" is out of scope.
The pilot uses the shared Chromium profile and holds the run lock for its whole duration, so an 08:00 run overlapping a long pilot skips via the lock.
