# Daily apply queue (pull model for the PC Claude session)

## Context
The PC session pulls a batch of 20 jobs, submits them in Sean's own Chrome with no per-job approval, and posts results back. We build only the server side: 3 bearer-token routes, batch bookkeeping, cover letter trimming, applications page and a runs-page section.

## Decisions made for you
1. Separate token: `autojob/apply_token.py` (copy of `worker_token.py`: sha256 at rest, `hmac.compare_digest`, `data/apply_token.json` 0600) and `autojob/set_apply_token.py` (`--rotate`, same shape as `set_worker_token.py`). Add `Config.apply_token_path`. It must NOT touch `worker_configured`, so the server apply phase, pilot windows and partition behave as today.
2. Routes live in new `autojob/web/apply_queue_routes.py`, included in `app.py` next to `worker_router` (one line). Own dependency `require_apply_token`, sets setting `apply_queue_last_pull` on GET /api/apply-queue only. Check how `app.py` middleware exempts `/api/worker`; exempt `/api/apply-queue`, `/api/apply-result`, `/api/cv/` the same way.
3. Batch is stored: new tables `apply_batches` (id, day local Asia/Jerusalem `YYYY-MM-DD`, created_at, last_pulled_at) and `apply_batch_items` (batch_id, job_id, payload_json frozen at creation, state `open|submitted|blocked|failed|expired`, reason, result_at). Add to `SCHEMA` in `db.py` (CREATE IF NOT EXISTS, no rebuild needed). Frozen payload means cover letters (sonnet) are generated once and a second call the same day returns the identical batch.
4. A batch for a day is created on the first pull of that day. Items still `open` from an older day are set `expired` first, so those jobs become selectable again. Size = setting `apply_queue_size` (default 20, clamp 1..50, bad value falls back to 20). No size query param.
5. Selection: `jobs.status='queued'`, order `fit_score DESC, found_at DESC`, then exclude: live worker lease (`worker_lease_token` set and `worker_lease_until >= now`) or `worker_phase='submit'`; job in any `open` batch item; any application with status in sent/replied/interview/rejected for the job; `jobs.reason` starting `vip`; AllJobs URL containing `/User/Register/CreditCard.aspx` or `/Register/`. Failed/blocked/filtered_out are excluded by the status filter. Put it in `db.select_apply_batch_jobs(conn, limit)`.
6. Open-batch exclusion elsewhere: `db.open_batch_job_ids(conn)`; filter `queued_jobs` in `pipeline/run.py::_run_apply_phase` and the `queued` list in `apply/remote.py::claim_next`. No job status change on batch creation.
7. `apply_url`: first `plan_channels(job, conn)` target whose kind is not `email` (reuse as is; it always ends with `job_url`). `site`: host contains linkedin -> linkedin, jobmaster -> jobmaster, alljobs -> alljobs, else company.
8. `answers`: `answers.build_answer_set(get_profile_answers, get_answer_bank)` plus `website: "https://sean.build"`, and `years_experience` forced to `DEFAULT_ANSWER_BANK["years_experience"]` whatever the bank says.
9. Cover letter: deterministic trim, no LLM (an LLM trim can add claims). New `autojob/apply/trim.py`:
   - `trim_cover_letter(text) -> str`: split into sentences (`.?!` and newlines kept per paragraph); drop every sentence matching `junior|ג'וניור|ג׳וניור|jr\.?` (case-insensitive); of sentences that say experience is missing (regex: `no (professional|paid|commercial)|do not have|don't have|not yet|limited experience|lack|close to no`), keep only the first; drop the others; remove emptied paragraphs; collapse blank lines.
   - `short_variant(text, limit=1000)`: from the trimmed letter, drop whole sentences from the middle paragraphs, last first, keeping greeting, first paragraph, closing and signature, until `len <= limit`; if still over, cut at the last sentence end before the limit.
   - Both only remove text. Test asserts the multiset of words in output is a subset of the input.
   - Payload: `cover_letter` (trimmed full) and `cover_letter_short` (<=1000, for AllJobs).
   - Source letter: the job's existing application `cover_letter` if present, else `answers.generate_application_content(..., work_dir=config.llm_work_dir)`, generated in `run_in_threadpool`, up to 4 jobs in parallel via a ThreadPoolExecutor; `ClaudeCliError` already falls back to `_fallback_cover_letter`.
10. Generator prompt (`answers._build_prompt`): add "Never use the word junior or its Hebrew equivalent. Mention missing professional experience in at most one short honest sentence, and not in the opening line." Keep `HONESTY_INSTRUCTIONS` facts. Reword `_fallback_cover_letter` is already compliant (one sentence); leave it.
11. POST /api/apply-result body (pydantic): `id` (job id), `status` submitted|blocked|failed, `reason` optional, `cover_letter_used`, `cv_version`, `timestamp` optional ISO. 404 unknown job, 422 bad status.
   - Creates an application row if none for this job (channel `pc:<site>`, `apply_url` from the batch payload or job url, `applied_at` = timestamp if valid ISO else now). Reuse an existing non-final one otherwise.
   - submitted: application `sent`, `cover_letter`, `cv_version`, notes line; job `applied`; batch item `submitted`.
   - blocked: job `failed`, `jobs.reason` = reason; reason containing `vip` (case-insensitive) or apply_url containing `CreditCard.aspx` is stored as `vip: <reason>` so selection never offers it again; application `failed`; item `blocked`.
   - failed: add column `jobs.pc_attempts INTEGER NOT NULL DEFAULT 0` (ALTER in the migrate block next to the worker columns). Increment; reason appended to application notes; if `pc_attempts <= setting apply_queue_max_retries` (default 2) job back to `queued` else job `failed`; item `failed` either way (not re-offered the same day).
   - Idempotent: if the item/application already has `submitted`, return `{"ok": true, "duplicate": true}` and change nothing; a repeat of the same blocked/failed result does not double-increment `pc_attempts` (compare item state and result_at/reason). `submitted` may overwrite an earlier failed or blocked.
12. GET /api/cv/latest: `latest_cv_pdf(config)`, `FileResponse` plus header `X-CV-Version: <filename>@<int mtime>`; 404 if no CV. Include `cv_version` in each batch response too.
13. GET /api/apply-queue response: `{"batch_id", "day", "size", "jobs": [{id,title,company,location,fit,apply_url,site,answers,cover_letter,cover_letter_short}], "cv_version"}`; empty `jobs: []` with 200 when nothing is eligible.
14. Channel `pc:<site>`: extend the `applications.channel` CHECK in `SCHEMA` with `OR channel LIKE 'pc:%'` and make the existing rebuild migration (`db._rebuild_tables`, `_applications_is_current`) detect the old CHECK and rebuild. Add `cv_version TEXT` to applications (ALTER in the migrate block AND the `_copy_rows` column tuple in the rebuild, or the data is lost). Test with a DB created from the old schema.
15. User import/pick check: nothing is imported or picked by a person here; the batch is chosen by fit score. Empty account: with no token the section shows the CLI name; with a token and no queued jobs the API returns an empty list and the page says "No eligible jobs".
16. No question for Sean.

## Design: pages
- Applications table (`applications.html`): channel cell for `pc:` shows a chip "PC" plus the site. Add a "CV" column showing `cv_version`. Detail page (`application_detail.html`): show reason/notes, cv_version, and the cover letter used (check what it already renders; add only what is missing).
- Apply queue section on `/runs` (`runs.html`, data via new `_apply_queue_view(config, conn)` helper called from `runs_list`, small edit in `routes.py`): token status ("Token set" or "Run `uv run python -m autojob.set_apply_token`"), last pull time ("Last pull: never" when unset), today's batch table (title, company, site, state, reason) and counts per state. Use existing card/chip classes; no new CSS beyond a few lines if needed.

## Tasks (one Bob)
1. Build it all, in this order:
   a. `config.py` (`apply_token_path`), `apply_token.py`, `set_apply_token.py`.
   b. `db.py`: SCHEMA tables, channel CHECK, columns `jobs.pc_attempts`, `applications.cv_version`, rebuild migration fix, helpers `select_apply_batch_jobs`, `open_batch_job_ids`, batch create/get/record-result.
   c. `apply/trim.py` and the `answers._build_prompt` rule.
   d. `web/apply_queue_routes.py` (three routes), `app.py` include and auth exemption, `_run_apply_phase` and `claim_next` exclusions.
   e. Templates and `runs_list`.
   f. AGENTS.md: new section "Apply queue (PC pull)" (token CLI, routes, batch semantics, VIP rule, trim). Do not touch FEATURES.md.
   Tests (temp `AUTOJOB_DATA_DIR`, fake `run_claude`, patterns in `tests/test_worker_api.py`): new `tests/test_apply_queue.py` and `tests/test_apply_trim.py` covering: 401 without or with the worker token; same batch twice (identical ids and letters, sonnet called once per job); size setting; exclusions (applied, failed, filtered_out, live lease, VIP reason, CreditCard URL, open batch); order by fit; new day expires old open items; results submitted/blocked(vip)/failed with retries over the limit; idempotent repeat; missing application created with `pc:linkedin`; claim_next and `_run_apply_phase` skip open-batch jobs; /api/cv/latest header and 404; trim (no "junior", Hebrew, one experience sentence, only removes words, short <=1000); prompt contains the new rule; old-schema DB migrates and keeps rows; runs page and applications page render with a `pc:` row.
   Verify: `npx tsc --noEmit -p tsconfig.json` does not apply (no TS here); run `./run-tests.sh` and `node ~/.claude/skills/slop-check/scripts/scan.js`.

## Verification
- `./run-tests.sh` green; slop-check exit 0.
- On the dev port (8040, loopback only): `uv run python -m autojob.set_apply_token`, then `curl -H "Authorization: Bearer $T" 127.0.0.1:8040/api/apply-queue` twice: same `batch_id` and letters, no "junior".
- POST a submitted result with curl for one id, repeat it, check `/applications` shows a "PC" row with CV version and `/runs` shows the batch state.
- GET `/api/cv/latest -D -` shows `X-CV-Version`. Worker token on these routes returns 401.

## Risks
- Generating up to 20 sonnet letters on the first pull can take minutes: the PC client needs a long timeout (tell the PC session); trimmed existing letters make later pulls fast.
- The applications CHECK rebuild touches live data: back up `data/` DB before first run on the real server and keep the test with an old-schema DB.
