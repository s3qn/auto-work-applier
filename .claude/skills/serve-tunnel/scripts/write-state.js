'use strict';
// Upsert one tunnel entry into the state file, keyed by slug.
// Usage:
//   node write-state.js --slug <s> --name <n> --branch <b> --worktree <path> \
//     --port <p> --status starting|ready|failed [--pid <pid>] \
//     [--exp <exp://url>] [--log <path>] [--started <iso>]
// Fields not passed are preserved from any existing entry with the same slug.

const { upsert, get } = require('./state-lib');

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i];
    const val = argv[i + 1];
    if (!key || !key.startsWith('--')) {
      throw new Error(`unexpected argument: ${key}`);
    }
    out[key.slice(2)] = val;
  }
  return out;
}

const a = parseArgs(process.argv.slice(2));
if (!a.slug) {
  process.stderr.write('write-state.js: --slug is required\n');
  process.exit(1);
}

const prev = get(a.slug) || {};
const entry = {
  name: a.name ?? prev.name ?? a.slug,
  slug: a.slug,
  branch: a.branch ?? prev.branch ?? `feat/${a.slug}`,
  worktree: a.worktree ?? prev.worktree ?? null,
  metroPort: a.port != null ? Number(a.port) : prev.metroPort ?? null,
  expUrl: a.exp ?? prev.expUrl ?? null,
  status: a.status ?? prev.status ?? 'starting',
  pid: a.pid != null ? Number(a.pid) : prev.pid ?? null,
  logPath: a.log ?? prev.logPath ?? null,
  startedAt: a.started ?? prev.startedAt ?? new Date().toISOString(),
};

upsert(entry);
process.stdout.write(`${entry.slug}: ${entry.status}${entry.expUrl ? ' ' + entry.expUrl : ''}\n`);
