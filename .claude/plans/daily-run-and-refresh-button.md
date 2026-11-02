# Daily run and Refresh jobs button

## Context
Sean wants a quick "Refresh jobs" (scan + filter only) and a daily full run at 08:00 Asia/Jerusalem so applying becomes a habit, with `auto_submit` untouched (filled applications wait as `ready_to_submit`). Today nothing schedules anything: `deploy/autojob-scan.timer` (09:00 and 16:00) was never installed, the sidebar "Next scan" reads a `scan_times` Targets field nothing honours, and `/runs/now` guards with an in-process `threading.Lock` plus `db.run_in_progress`, which a second process cannot see and a dead run row blocks forever. The real DB (read-only check, 2026-11-02) has no unfinished run row but 3 jobs stuck in `applying` (ids 41, 87, 184), each with only a `needs_help` application. `loginctl show-user sean -p Linger` is `Linger=yes`, so a user timer fires without a login session. systemd 255.

## Design

### Run lock (new `autojob/pipeline/lock.py`)
One file lock shared by every process: `fcntl.flock(LOCK_EX | LOCK_NB)` on `config.run_lock_path` (new `Config` property, `data_dir / "run.lock"`). flock is per open file description, so two threads in the web app that each `open()` the file conflict correctly, and the OS releases it when a process dies (no stale lock ever).
- `class RunInProgress(Exception)`.
- `hold_run_lock(config, *, attempts=3, delay=0.2)`: context manager; tries up to `attempts` times, then raises `RunInProgress`. The retry covers the tiny window where a `run_active` probe holds the lock.
- `run_active(config) -> bool`: probe (acquire with `attempts=1`, release immediately). Replaces every `db.run_in_progress(conn)` call in routes. `db.run_in_progress` itself stays, unused.

### Run modes and interrupted-run cleanup (`autojob/pipeline/run.py`)
- `RUN_MODES = ("full", "refresh")`. `run_scan(trigger="manual", *, mode="full", keywords=None, ...)`: `refresh` runs scan + filter (they are inseparable: jobs are only stored during the filter stage) and skips research and apply, logging `Refresh: research and apply skipped.` and emitting the same text as a `system` info event. `ValueError` on any other mode. Nothing is copied: the two phases sit behind `if mode == "full":`.
- `run_scan` takes the lock first: `with hold_run_lock(config):` wraps everything from `create_run` on; `RunInProgress` propagates to the caller (no run row is created).
- Right after `create_run`, still under the lock (so no other run can be mid-apply): `_reap_interrupted(conn, logger, run_id)` calls two new db helpers and logs one line each only when the count is non-zero: `Closed N interrupted run(s).` and `Requeued N job(s) left in 'applying' by an interrupted run.` Runs in both modes.
- `db.close_interrupted_runs(conn, *, closed_by_run_id) -> int`: `UPDATE runs SET finished_at = now, log = log || '\n[now] Interrupted: the process exited before this run finished. Closed by run <id>.' WHERE finished_at IS NULL AND id != closed_by_run_id`.
- `db.requeue_interrupted_applying_jobs(conn) -> int`: `UPDATE jobs SET status = 'queued' WHERE status = 'applying' AND id NOT IN (SELECT job_id FROM applications WHERE status IN ('ready_to_submit', 'sent', 'replied', 'interview'))`. A job in `applying` with a `ready_to_submit` application is legitimately waiting for Sean and is never touched. The 3 stuck jobs match the rule.
- `db.create_run(conn, *, trigger, mode="full")` stores the mode. Migration in `db.migrate_schema`: guarded `ALTER TABLE runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'full'` (not added to `SCHEMA`, same pattern as `jobs.reason`, so `test_migrate_schema_adds_columns_to_legacy_db` keeps working).
- `main(argv=None) -> int` replaces the bare `__main__` body: `run_scan(trigger="scheduled")`; on `RunInProgress` print `Another run is in progress; skipping this scheduled run.` and return 0 (not an error for the timer); otherwise print `Run <id> finished.` and return 0. `if __name__ == "__main__": raise SystemExit(main())`. The scheduled path reads `auto_submit` through `get_auto_submit(conn)` exactly like today and never writes the setting.

### Schedule (new `autojob/pipeline/schedule.py`)
`DAILY_RUN_TIME = time(8, 0)`, `TIMEZONE = ZoneInfo("Asia/Jerusalem")`, `next_scheduled_run(now=None) -> datetime` (today 08:00 if still ahead, else tomorrow 08:00, in `TIMEZONE`), `describe_next_run(now=None) -> str` returning `today at 08:00` or `tomorrow at 08:00`. The timer unit is the only other place the hour appears; a test parses `deploy/autojob-scan.timer` and asserts its single `OnCalendar` line is `*-*-* 08:00:00 Asia/Jerusalem` matching `DAILY_RUN_TIME`, so the two cannot drift silently. The label states the schedule, not whether the timer is installed; `deploy/install-timer.sh` is what makes it true.

### systemd (existing `deploy/`, no new folder)
- `deploy/autojob-scan.timer`: Description `auto-job daily run at 08:00 Asia/Jerusalem`; `[Timer]` `OnCalendar=*-*-* 08:00:00 Asia/Jerusalem`, `Persistent=true`, `Unit=autojob-scan.service`; `[Install] WantedBy=timers.target`. Drop the 09:00 and 16:00 lines.
- `deploy/autojob-scan.service`: keep `Type=oneshot`, `WorkingDirectory=%h/ai-projects/auto-job`, `ExecStart=%h/.local/bin/uv run python -m autojob.pipeline.run`; add `Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin` (the user manager's PATH has no `~/.local/bin`, where `claude` lives, so the LLM calls would fail silently without it) and `TimeoutStartSec=4h` (a hung browser must not block tomorrow's run). The main repo's `data/` is the DB the dashboard uses, so timer and web app share `data/run.lock`.
- New `deploy/install-timer.sh` (executable, `set -euo pipefail`, `cd "$(dirname "$0")"`): `mkdir -p ~/.config/systemd/user`, copy both units there, `systemctl --user daemon-reload`, `systemctl --user enable --now autojob-scan.timer`, `systemctl --user list-timers autojob-scan.timer --no-pager`, and print `loginctl show-user "$USER" -p Linger` with the note that it must be `yes`. Bob never runs it, never runs `systemctl`, and never touches the real `data/` (the worktree's own `data/` is what any manual run in the worktree would use).

### Web
- `POST /runs/refresh` (routes.py, next to `run_now`): same CSRF and lock pattern as `run_now`; guard is `runlock.run_active(config) or not lock.acquire(blocking=False)` (needs `config: Config = Depends(get_config)`); thread target `_run_scan_and_release(lock, mode)` (add the `mode` argument to the existing helper and catch `RunInProgress` inside it, logging nothing: the route already probed). Busy: redirect `/jobs?busy=1`. Started: redirect `/jobs?refresh=started`. `run_now` keeps redirecting to `/runs` and uses the same probe instead of `db.run_in_progress`.
- Jobs page (`jobs.html`): a form with button `Refresh jobs` placed right of the tabs row (wrap tabs and form in `<div class="page-actions">`, flex, `justify-content: space-between`, `align-items: center`, `margin-bottom: 16px`; move `margin-bottom` off `.tabs` inside it). `field-hint` text under it: `Scan and filter only. "Run now" in the sidebar also researches and applies.` Flash block directly below, for every tab: `refresh_started` shows `Refresh started. New jobs appear here as they are found. <a href="/runs/live">Watch it live</a>.`; `run_busy` shows `A run is already in progress. Try again once it finishes.` Move the existing busy message out of the queued-only block so it appears once. Empty state becomes `No jobs found yet. Use Refresh jobs to search now, or wait for the daily run.`
- `jobs_list` route: new query param `refresh: str | None = None`; `ctx["refresh_started"] = refresh == "started"`, `ctx["run_busy"] = busy == "1"` (keep `research_busy` for the old test).
- `jobs_research_pending` (line 294) and `application_release` (line 226) use `runlock.run_active(config)` too, so a timer-driven run is respected by every browser-using button. `job_apply_now` is left as is (see Risks).
- Sidebar: `base.html` untouched. `deps.next_scan_display(conn)` keeps its signature and returns `schedule.describe_next_run()`; `DEFAULT_TARGETS` loses `scan_times`. `applications.html` empty state becomes `No applications yet. The next scheduled run is {{ next_scan }}.`
- Targets page: remove the `scan_times` textarea and the `scan_times` Form param plus dict key in `targets_post`; in their place a label-less `<p class="field-hint">` reading `Daily run: every day at 08:00 Asia/Jerusalem (scan, filter, research, apply), next {{ next_scan }}. Installed with deploy/install-timer.sh.` A stored `targets` JSON that still has `scan_times` is simply ignored.
- Run log: `runs.html` Trigger cell and `run_detail.html` subtitle show `{{ run.trigger }}{% if run.mode == 'refresh' %} (refresh){% endif %}`. The live view needs no change: research and apply bars show `-` for a refresh.
- Fresh account: Jobs page shows the Refresh button above the empty state, the sidebar shows `Next scan tomorrow at 08:00` (or today), Run log shows the usual empty text. Nothing is imported or picked from outside the app.

### Decisions made for you
1. Lock is a `flock` file in `data/`, not a DB row: freed by the kernel on crash, visible to every process. Dead run rows are closed at the next run start, and the UI guard no longer reads them.
2. `mode="full" | "refresh"` on `run_scan` (stored in `runs.mode`), rather than a free-form `phases=` set: scan and filter cannot be separated, and the UI needs exactly these two labels.
3. The Refresh button lives on the Jobs page (where its results land), not in the sidebar. The sidebar keeps one button, Run now.
4. The schedule is a constant in code plus the unit file, with a test tying them together. The Targets "Scan times" field is removed because nothing honours it; the Targets page shows the fixed schedule instead.
5. Unit files stay in `deploy/` (they already exist there); the timer is edited to 08:00 only. The service gets a PATH that includes `~/.local/bin` and a 4h timeout.
6. Requeue of `applying` jobs happens at the start of every run (full or refresh), under the lock, and skips any job with a `ready_to_submit`/`sent`/`replied`/`interview` application.
7. A timer firing while a manual run is active exits 0 with a printed message; the missed day is not retried (Persistent only covers downtime).

## Touches
- `autojob/config.py`: `Config.run_lock_path` property.
- `autojob/db.py`: `create_run` (mode), `migrate_schema` (runs.mode), new `close_interrupted_runs`, `requeue_interrupted_applying_jobs`.
- `autojob/pipeline/run.py`: imports, `RUN_MODES`, `run_scan` signature and body (lock wrap, mode gate, `_reap_interrupted`), new `main()`, `__main__`.
- `autojob/pipeline/lock.py`, `autojob/pipeline/schedule.py`: new.
- `deploy/autojob-scan.timer`, `deploy/autojob-scan.service`: edited. `deploy/install-timer.sh`: new.
- `autojob/web/routes.py`: imports; `application_release` guard (one added condition); `jobs_list` (one param, two ctx lines); `jobs_research_pending` guard (one line); `targets_post` (remove `scan_times` param and key); `_run_scan_and_release` and `run_now`; new `run_refresh`. Nothing else, so live-browser-view merges cleanly.
- `autojob/web/deps.py`: `DEFAULT_TARGETS` (drop `scan_times`), `next_scan_display` body.
- `autojob/web/templates/jobs.html` (tabs row, flash block, empty state), `targets.html` (scan_times block), `runs.html`, `run_detail.html`, `applications.html` (empty state). `base.html` and `app.py` untouched.
- `autojob/web/static/style.css`: `.page-actions`, `.tabs` margin.
- `tests/test_pipeline.py`, `tests/test_db.py`, `tests/test_pages.py`, new `tests/test_schedule.py`, `tests/test_lock.py`.
- `AGENTS.md`: new `## Schedule and Refresh` section after "Live view".

## Tasks

### Task 1: lock, modes, cleanup, schedule, systemd (backend)
Files: `autojob/config.py`, `autojob/db.py`, `autojob/pipeline/lock.py`, `autojob/pipeline/schedule.py`, `autojob/pipeline/run.py`, `deploy/*`, `tests/test_pipeline.py`, `tests/test_db.py`, `tests/test_lock.py`, `tests/test_schedule.py`.
Build everything under "Run lock", "Run modes and interrupted-run cleanup", "Schedule" and "systemd" above. Anchors: `run_scan` and the `if __name__ == "__main__":` block in `run.py`; `create_run`, `run_in_progress`, `migrate_schema` in `db.py`; `_patch_no_op_apply`, `_seed_queued_job`, `test_apply_phase_defaults_auto_submit_to_false` in `tests/test_pipeline.py` as models.
Tests to add:
- `test_lock.py`: `hold_run_lock` held in the test (open the lock file, `fcntl.flock` it) makes `run_active` True and a second `hold_run_lock(attempts=1)` raise `RunInProgress`; released makes `run_active` False.
- `test_pipeline.py`: `test_refresh_mode_skips_research_and_apply` (patch `run_module.research_pending` and `apply_to_job` to record calls; seed a queued job; `run_scan(mode="refresh", keywords=[])`; both lists empty; run row `mode == "refresh"`; log contains `Refresh: research and apply skipped.`). `test_run_scan_raises_when_lock_held` (hold the lock; `run_scan` raises `RunInProgress`; `list_runs` empty). `test_run_scan_closes_interrupted_runs_and_requeues_applying_jobs` (seed one unfinished run row via `db.create_run`; job A `applying` with a `needs_help` application; job B `applying` with a `ready_to_submit` application; `run_scan(keywords=[])` with apply patched; old run has `finished_at` and the `Interrupted:` line; A is `queued`, B still `applying`). `test_scheduled_run_never_enables_auto_submit` (seed queued job; call `run_module.main()`; captured `auto_submit is False`; `db.get_setting(conn, "auto_submit")` still None; return value 0). `test_main_skips_when_lock_held` (hold lock; `main()` returns 0; no run row).
- `test_db.py`: extend `test_migrate_schema_adds_columns_to_legacy_db` with `runs.mode`; `test_requeue_interrupted_applying_jobs_keeps_ready_to_submit`.
- `test_schedule.py`: `describe_next_run` at 07:59 and 08:01 Jerusalem time; the timer-file parse test described under Schedule.
Verify: `./run-tests.sh` green; `~/.local/bin/uv run python -m autojob.pipeline.run` is NOT run against the real repo (if Bob wants a smoke run, `AUTOJOB_DATA_DIR=/tmp/autojob-smoke ~/.local/bin/uv run python -m autojob.pipeline.run` from the worktree, which has no targets and finishes quickly); `bash -n deploy/install-timer.sh`; `systemd-analyze --user verify deploy/autojob-scan.timer` may be used read-only if available, never `systemctl enable`.

### Task 2: Refresh button, Next scan label, Targets and Run log copy, docs (web), after Task 1
Files: `autojob/web/routes.py`, `autojob/web/deps.py`, `autojob/web/templates/{jobs,targets,runs,run_detail,applications}.html`, `autojob/web/static/style.css`, `tests/test_pages.py`, `AGENTS.md`.
Build everything under "Web" above. Anchors: `run_now`, `_run_scan_and_release`, `jobs_list`, `jobs_research_pending`, `application_release`, `targets_post` in `routes.py`; `next_scan_display`, `DEFAULT_TARGETS` in `deps.py`; `test_run_now_records_a_run` and the targets tests at lines 183 and 330 of `tests/test_pages.py` (drop `scan_times` from their posted data; the first asserts `08:00 Asia/Jerusalem` in the reloaded page instead).
Tests to add in `test_pages.py`: `test_refresh_records_a_refresh_run` (model on `test_run_now_records_a_run`, also patch `run_module.research_pending` and `run_module.apply_to_job` to fail if called; 303 to `/jobs?refresh=started`; latest run `mode == "refresh"`). `test_refresh_redirects_busy_when_lock_held` (hold the lock as in Task 1's test; 303 to `/jobs?busy=1`; no run row). `test_jobs_page_shows_refresh_button_and_flash` (GET `/jobs?refresh=started` contains `Refresh jobs` and `Watch it live`). `test_sidebar_shows_computed_next_scan` (monkeypatch `schedule.describe_next_run` or assert `Next scan t` followed by `at 08:00`).
AGENTS.md: a `## Schedule and Refresh` section (10 to 15 lines): the timer and service in `deploy/`, `install-timer.sh`, `data/run.lock` as the one guard shared by timer and dashboard, `run_scan(mode=)`, the interrupted-run cleanup rule, `next_scan` computed from `schedule.DAILY_RUN_TIME`, and that `auto_submit` is unchanged by scheduling.
Verify: `./run-tests.sh` green; `~/.local/bin/uv run uvicorn autojob.web.app:create_app --factory --host 127.0.0.1 --port 8041` from the worktree with `AUTOJOB_DATA_DIR=/tmp/autojob-ui` (never 8040, never the main repo's `data/`), set a password, log in, confirm the Jobs page button and the sidebar label; last step `node ~/.claude/skills/slop-check/scripts/scan.js` from the worktree root, clean exit.

## Verification in the browser (after the main session runs deploy/install-timer.sh with Sean's approval)
1. `systemctl --user list-timers autojob-scan.timer` shows the next trigger at 08:00 Asia/Jerusalem; the sidebar on autojob.sean.build says `Next scan tomorrow at 08:00` (or today) and the two agree.
2. Jobs page: click `Refresh jobs`; the flash appears, `/runs` shows a `manual (refresh)` row, the live view shows scan and filter bars moving and research/apply as `-`; no application is created.
3. Click `Run now` while the refresh is still running: lands on `/runs?busy=1`, no second row.
4. After the first run of any kind, jobs 41, 87 and 184 are `queued` again and the run log shows `Requeued 3 job(s)...`.
5. Next morning: a `scheduled` row appears in Run log, applications sit in `ready_to_submit` on the Applications page, the Targets auto-submit checkbox is still off.
6. Targets page has no scan times field, shows the daily run line.

## Risks
- `Apply now` and `Release` launch the single Chromium profile outside a run; if clicked while the 08:00 run is mid-apply, Chromium refuses the second profile launch and that one job records a failed attempt. Release now refuses when a run is active; Apply now is left alone (job detail has no busy copy today).
- The `run_active` probe briefly holds the lock; `hold_run_lock` retries three times so a scheduled run is not skipped by a coincident click.
- The user manager's PATH lacks `~/.local/bin`; the service sets PATH explicitly. If `claude` moves, the scheduled run fails soft (filter marks jobs `new`), visible in the run log.
- Jobs requeued from `applying` may have a stale `needs_help` application row; the next apply creates a fresh one, as Apply now does today.
