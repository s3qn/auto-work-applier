---
name: feature-dev
description: >-
  Build and on-device test one or more features of the auto-job, isolated
  per feature. Use whenever the user asks to add/build/prototype app feature(s)
  and try them on a phone, including a single prompt that names SEVERAL
  features. The Director rates each feature and picks models, a chief, pro or
  noob planner plans it in its own git worktree, Bob builds it, features are
  merged into one wave worktree and reviewed there, the fixer fixes findings;
  the wave is served over a tunnel with a QR on dev.sean.build (server agent),
  then each feature gates on Accept (main session runs accept-push.sh) / Adjust (iterate) /
  Delete (discard). The Kaizen Monk reviews the run itself afterwards. No
  slash command. This triggers from natural feature requests.
---

# feature-dev

Isolated feature development with on-device QR testing. Each feature lives in
its own worktree and several build in parallel as a wave. The work is done by
the project's own agents in `.claude/agents/`; **you (the main session)
mediate every user gate**. Subagents cannot talk to the user directly.

| Step | Agent | Model |
|------|-------|-------|
| Rate complexity, pick models, review field, spot collisions | The Director (`director`) | sonnet |
| Plan a chief / pro / noob feature | `chief-planner` / `pro-planner` / `noob-planner` | fable / sonnet / haiku |
| Build plan tasks (every builder is Bob) | `bob` | sonnet (haiku for noob, via the `model` override) |
| Check the plan was built, find bugs (per feature or over a wave) | `reviewer` | opus (sonnet for noob and pro, via override) |
| Fix what the reviewer or Sean found (medium and above) | `fixer` | sonnet (opus for audio, timing, data) |
| Put a wave worktree on the phone | `server` | haiku |
| FEATURES.md, commit message file, voice notes | `scribe` | haiku |
| Review the run itself after the wave ships | The Kaizen Monk (`kaizen-monk`) | sonnet |
| Suggest micro-animations after a wave with a new screen or big UI change | The Animation Maniac (`animation-maniac`) | sonnet |

Token rules: one Bob per feature unless tasks are truly independent and large;
every agent reports briefly (its prompt caps the length); a planner that
replies `ESCALATE` goes to the next tier up, and the Director prefers the
lower tier when a feature is borderline for the same reason. Parallel
worktrees, merged by `wave-integrate.sh`, are the norm, not one feature at a
time; only real semantic overlaps get flagged as collisions.

Do not use the generic `Plan` or `general-purpose` agents for these steps.

Scripts live in `scripts/` here; the serve/QR half is the `serve-tunnel`
skill. `REPO` = the auto-job checkout root; `SKILL_DIR` = this skill's
directory.

## Flow

### 1. Start clean
Start each wave in a fresh session (previous waves' context is dead weight
here). Suggest `/model sonnet` for the main session doing this orchestration;
the actual work happens in subagents on the models below.

### 2. The Director
Always call it "The Director" when talking to Sean, never just "Director".
Split the request into features `[f1..fN]` (slugify: lowercase,
non-alphanumerics → `-`; N = 1 is the same flow). Spawn `director` once with
the whole batch. It replies with a tier (chief, pro or noob), a Bob model, a
reviewer model and a review field (`skip`/`wave`/`own`) per feature, plus
collisions and a worktree grouping. Create each worktree:

```bash
WT=$(bash "$SKILL_DIR/scripts/worktree-create.sh" "<feature name>")
```

Once the worktrees exist, and again right before step 6, run
`bash "$SKILL_DIR/scripts/overlap-check.sh" f1 f2 ...`: it lists each
feature's changed files, prints pairs that touch the same file, and warns on
files over 1500 lines. Pass overlaps on to the planners and fixers.

### 3. Planners, in parallel
Spawn the matching planner per feature in one message, with the request in
Sean's words, the worktree, the plan path
`<worktree>/.claude/plans/<slug>.md`, and the Director's notes. When a planner
asks a small question, answer it with the planner's own recommended default
rather than stopping to ask Sean, and record the default (you show these back
to Sean as "Decisions made for you" at the accept gate, step 9). Only surface
a question to Sean via `AskUserQuestion` when it is a real trade-off with no
sane default; batch all such questions into one call.

### 4. Build with Bob
When the Bobs are sent out, tell Sean the catchphrase exactly: **"All the Bobs
are building"**. For each plan, spawn `bob` (with the Director's model) for
its tasks: one Bob for all tasks unless the plan marks large independent
ones, or the feature is chief-tier and the plan splits cleanly into
backend/ and src/ work, in which case run two Bobs, each told which part it
owns. When a plan has 3 or more tasks that touch the same file, run
sequential Bobs instead, one per task against the same worktree, each
starting after the previous one reports. Bobs edit in place and never commit.

### 5. Review and fix per The Director's `review` field
- `skip` (noob): no separate review call; Bob's own verification stands.
- `own` (chief): spawn `reviewer` on that feature alone now, before merging,
  since a wrong design here is expensive to undo.
- `wave` (pro): no per-feature review; it is covered once in step 7.

For any findings from an `own` review, spawn `fixer` with them; it fixes
medium and above only, and reports low findings as left for later.

### 6. Merge the wave
Run `overlap-check.sh` again (step 2) so the reviewer and fixers know which
files are shared, then merge every feature going into this wave with one call:

```bash
bash "$SKILL_DIR/scripts/wave-integrate.sh" "<wave name>" f1 f2 f3 ...
```

It snapshots each feature worktree's uncommitted changes as a patch, resets
that worktree back to clean (the changes stay on disk, just unstaged, so
accept-push.sh can still commit them later), creates the wave worktree, and
applies the patches in order. If it exits 3, resolve the conflicts it prints
in the wave worktree, then rerun with `--resume`. Exit 0 prints the wave
worktree path.

### 7. One wave reviewer for the pro features
Spawn `reviewer` in wave mode with the wave worktree and the plan paths of
every `wave`-reviewed (pro) feature in it, to check each plan and hunt for
integration bugs between the merged features. Spawn `fixer` in the wave
worktree for medium-and-above findings.

### 8. Serve the wave
```bash
bash "$SKILL_DIR/scripts/serve-wave.sh" "<wave worktree>" "<wave name>" [checklist.json]
```
This frees port 8020 and any stale sessions, links `.venv`/installs
`node_modules` if missing, starts the wave's own backend and its
`serve.sh` tunnel in tmux session `<slug>` (windows `backend`, `metro`), waits
up to 180s for ready, and writes the checklist (start from each plan's
"Verification on the phone" plus anything risky the build turned up).

### 9. Test gate: per feature
For each feature in the wave, first show Sean the "Decisions made for you"
list recorded in step 3 so he can override any of them, then ask with
`AskUserQuestion`: **Accept / Adjust / Delete**.

- **Adjust**: small change or a bug: `fixer` in the wave worktree. A change
  that alters the design: back to a planner (the Director's tier, or lower if
  the change is small) to amend the plan, then `bob`, both against the wave
  worktree. Re-run the checklist if there is anything new to test, then
  return to this gate.
- **Delete**: the feature's own worktree (not the wave's) still holds its
  patch's source; run `worktree-delete.sh` on it and drop it from the wave's
  accept list. If its code already landed in the wave worktree, remove it
  there too before accepting.

### 10. Accept
Once every feature in the wave resolves to Accept, have `scribe` write one
commit message file summarizing the wave. Before running accept-push.sh,
check that no untracked file in main would be overwritten by the wave branch
(`git status --short`); if one is identical, move it aside. Then run
accept-push.sh directly with the message file and one `--done` per accepted
feature, in order (plus `--drop-next N` per feature that came from Next):

```bash
timeout 300 bash "$SKILL_DIR/scripts/accept-push.sh" "<wave name>" <message-file> \
  --done "<bullet for f1>" --done "<bullet for f2>" ...
```

This is the sole commit point: it commits the wave worktree, merges into
local main (rebasing if main advanced), **pushes main**, restores
node_modules if package.json changed, and tears down the wave worktree and
tunnel. The `timeout 300` means a hang fails after 5 minutes instead of eating
the 10 minute tool cap; if it fires, check `git log origin/main -1` before
retrying, since the push has usually already landed.

### 11. Restore main
```bash
bash "$SKILL_DIR/scripts/restore-main.sh" "<wave name>"
```
Kills the wave's tmux session and port 8020, starts main's own backend in
tmux session `shadow-backend`, and waits for `/health`.

### 12. Ledger, retro, Kaizen Monk
Log every agent's usage with `ledger.sh <wave> <feature> <role> <model>
<tokens> [tool_uses] [note]` as each agent's notification arrives; don't
batch it to the end. Once the wave is accepted, write
`.claude/runs/<wave>.md` (features, tiers, findings per feature, Sean's
verdicts, rework, session cost), then spawn `kaizen-monk` with the wave name
and show Sean its top suggestions.

If the wave added a new screen or a big UI change (not for fixes or small
tweaks), also spawn `animation-maniac` in the same message with each such
feature's name, plan path and the merged commit. Show Sean its suggestions
as ideas; the ones he picks go to The Director like any feature, and tell
the Maniac his verdicts so its ledger learns his taste.

## Rules

- Edits land **in-place, uncommitted**; `accept-push.sh` (via `accepter`) is
  the sole commit, run once per wave.
- Never `pkill` tunnels globally: teardown is per-slug (`stop-tunnel.js`), so
  anything else running on this machine survives. Port 8020 is freed with
  `fuser -k 8020/tcp`, never by killing a process by name pattern.
- Worktrees live at `../auto-job-worktrees/<slug>` (outside the repo),
  one per feature plus one per wave.
- Only one ngrok tunnel exists at a time (free tier = one usable domain), so
  waves are served one at a time even though features within a wave build in
  parallel.
- Prereq: a ngrok v3 agent authenticated with your own token, and the
  dev.sean.build dashboard running for QRs.
- **One editor per served worktree.** Before sending a fixer into a worktree,
  run `edit-lock.sh check <worktree>` and `ListAgents`: a held lock, a file
  changed in the last 3 minutes, or another interactive session on this
  project means someone else may be editing, so find out who before
  dispatching. Fixers acquire and release the lock themselves.
- **Queue fixers per big file.** Only one fixer edits a file over ~1500 lines
  (today `src/app/island/[id].tsx`) at a time; other fixers in that wave take
  work in other files or wait.
- **Lag, freeze or stutter on the phone: measure after one read.** If the
  first `pipe-declogger` pass does not reach a Verified cause, the next step
  is a fixer adding temporary on-device timing (buffered marks, one log line,
  JS and UI thread stall detectors) and reading the numbers from Metro, never
  a second static-only pass. Remove the instrumentation before accept.
- **Visual adjusts need a spec.** When Sean asks for a look ("make it glow",
  "less ugly"), write exact values into the fixer brief (colours, opacity,
  size, duration, easing) or offer two concrete options first. After a visual
  pass Sean rejects, look at a screenshot or recording frame before briefing
  the next pass.

## Verification checklist (per wave)
- [ ] Each feature's worktree created; `node_modules` and `.env` resolve.
- [ ] Each Bob's plan exists at `<worktree>/.claude/plans/<slug>.md`.
- [ ] tsc and backend tests pass; `own`/`wave` reviewer findings (medium+)
      fixed, low findings noted as later.
- [ ] `wave-integrate.sh` exited 0 (or conflicts were resolved and resumed).
- [ ] `serve-wave.sh` reported ready; QR visible on dev.sean.build; `/health`
      answers on :8020.
- [ ] "Decisions made for you" from every plan shown to Sean before the
      Accept / Adjust / Delete question.
- [ ] Every feature in the wave resolved to Accept or Delete before the
      single accept-push.sh call.
- [ ] Accept → wave commit is on main and pushed, wave worktree gone, slot
      freed, main's backend restored via `restore-main.sh`.
- [ ] Delete → that feature's worktree, branch, and state entry all gone.
- [ ] Ledger rows recorded for every agent call; `.claude/runs/<wave>.md`
      written; Kaizen Monk ran and its suggestions were shown to Sean.
