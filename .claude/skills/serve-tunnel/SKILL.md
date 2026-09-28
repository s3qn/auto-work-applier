---
name: serve-tunnel
description: >-
  Serve one build of the auto-job over an ngrok v3 tunnel and show a
  scannable QR. Use when you need to run a worktree (or the main checkout) on a
  real phone: starts a standalone ngrok v3 agent plus the app's dev server on a
  free port, captures the public URL, registers it in the shared tunnel state
  file (so the dev.sean.build dashboard renders a QR), and prints an ASCII QR
  fallback. Serve ONE tunnel at a time (ngrok free tier = one usable domain).
  Also handles stopping a tunnel and freeing its slot.
---

# serve-tunnel

Runs one build on a device-scannable tunnel. Called by the `feature-dev` loop
(once per feature) or directly to expose the main checkout.

## Stack config (read this first)

`serve.sh` is stack-agnostic. It sources **`$REPO/.claude/serve-config.sh`** for
how to start the dev server and how to tell that it is ready:

| Variable | Meaning |
|---|---|
| `DEV_START_CMD` | command to exec; must bind `$PORT` and stay in the foreground |
| `DEV_PROXY_ENV` | env var that receives the public https URL (Expo: `EXPO_PACKAGER_PROXY_URL`); empty if not needed |
| `DEV_READY_RE` | pattern in the dev log meaning "actually serving" |
| `DEV_FAIL_RE` | pattern meaning it failed to start |
| `DEV_SCAN_SCHEME` | `exp` for an Expo Go link, `https` for a browser link |

`serve-config.example.sh` ships presets for Expo, Vite, Next.js and FastAPI.
Copy it to `serve-config.sh` and uncomment the one that matches. Without that
file `serve.sh` exits 5 and tells you so.

## Contract (shared with the dev.sean.build dashboard)

- **State file:** `~/.claude/dev-feature-tunnels.json` (override `DEV_TUNNELS_STATE`).
  Array of `{ name, slug, branch, worktree, metroPort, expUrl, status, pid, logPath, startedAt }`.
  Deliberately **shared with the other projects on this machine**: the ngrok cap
  is per account, not per repo, so one state file enforces it globally and one
  dashboard shows every running feature. Slugs are global, so avoid reusing a
  feature name that another project is serving right now.
- **Checklist file:** `~/.claude/dev-feature-test-checklists.json` (override
  `DEV_TEST_CHECKLISTS`), a **separate** file, keyed by `slug`:
  `{ "<slug>": { items: [ { text, checked } ] } }`. The dashboard renders these
  as tap-to-toggle boxes under each QR and writes `checked` back on tap. Kept
  apart from the state file on purpose, so a status update never clobbers the
  checklist.
- **status:** `starting` → `ready` | `failed`.
- **Concurrency = 1** (ngrok free tier has one usable domain, and the agent's
  local inspector binds one port, 4040). Serve/test one at a time; Accept or
  Delete to free the slot before the next.
- **Ports:** first free port in 15000–16000 (`pick-port.js`).
- **Teardown kills by pid only**, never a global `pkill ngrok` (that also matches
  the shell running it). `serve.sh` launches the ngrok v3 agent and the dev
  server together under one `setsid` group led by the tracked pid, so
  `stop-tunnel.js` reaps both by killing the group.

`$SKILL_DIR` below is this skill's directory.

## Serve a build

```bash
bash "$SKILL_DIR/scripts/serve.sh" <worktree-path> "<feature name>"
```

- Refuses (exit 2) if a tunnel is already active. Accept or Delete it first.
- Refuses (exit 4) if no ngrok v3 agent is found (set `NGROK_BIN`, or install to
  `~/.local/bin` and `ngrok config add-authtoken <your personal token>`).
- Refuses (exit 5) if `.claude/serve-config.sh` is missing.
- On success prints the scan URL, the `https://dev.sean.build` link, and an
  inline ASCII QR. The dashboard shows the same QR on your phone.
- On failure (exit 3) prints both log tails (dev server + ngrok); the poll waits
  120s (`SERVE_TUNNEL_TIMEOUT` to change).
- Uses a standalone **ngrok v3** agent, NOT `expo start --tunnel`. The bundled
  `@expo/ngrok` only ships a dead v2 agent (`@expo/ngrok-bin@2.3.42`) that fails
  with `ERR_NGROK_108` on every connection. serve.sh runs `ngrok http <port>`
  and points the bundler at it via `DEV_PROXY_ENV`. ngrok v3 must be
  authenticated with **your** token (`~/.config/ngrok/ngrok.yml`), not Expo's
  shared token (Expo rewrites `~/.expo/ngrok.yml` on every `--tunnel`, so never
  source the token from there).

## Write a feature's test checklist

Persist the "what to test" list for a slug so the dashboard shows it under the
QR. Items are a JSON array on stdin (plain strings, or `{text[,checked]}`
objects); re-running for the same slug replaces its items (resetting `checked`):

```bash
echo '["Scan QR opens the feature","Happy-path flow works","Invalid input shows an error"]' \
  | node "$SKILL_DIR/scripts/write-checklist.js" --slug <slug>
```

The **content** must come from whoever knows what the feature does. In the
`feature-dev` loop that's the build subagent, which calls this right after
`serve.sh`.

## Stop a tunnel / free a slot

```bash
node "$SKILL_DIR/scripts/stop-tunnel.js" <slug>
```

## Inspect state

```bash
node "$SKILL_DIR/scripts/list.js"        # human-readable table
cat "${DEV_TUNNELS_STATE:-$HOME/.claude/dev-feature-tunnels.json}"
```

## Notes

- Do **not** pass `CI=1`: it suppresses the interactive dev server the tunnel needs.
- The free account's ngrok domain is STABLE across restarts (measured), so a
  dashboard QR survives an agent restart. The one-at-a-time cap is because the
  free tier has one usable DOMAIN, not one session: a second concurrent agent
  session establishes fine but its endpoint collides on that domain
  (`ERR_NGROK_334`).
- Failure modes and what they mean:
  - `ERR_NGROK_108` in the ngrok log = a v2 agent, or v3 authed with Expo's
    shared token. Re-auth v3 with your own token and confirm `ngrok version` is 3.x.
  - `Skipping dev server` / `Input is required` / `EADDRINUSE` in the dev log =
    the chosen port was taken; `pick-port.js` should avoid this, but a racing
    process can still grab it.
