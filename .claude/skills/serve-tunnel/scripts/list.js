'use strict';
// Human-readable dump of the tunnel state file.
// One line per entry: slug, status, port, pid (up/dead), age since startedAt,
// exp URL. The age makes a stale entry visible at a glance.
const { readState, statePath } = require('./state-lib');

function age(startedAt) {
  if (!startedAt) return '-';
  const ms = Date.now() - Date.parse(startedAt);
  if (!Number.isFinite(ms) || ms < 0) return '-';
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h${m % 60}m`;
  return `${Math.floor(h / 24)}d`;
}

const entries = readState();
if (!entries.length) {
  process.stdout.write(`(no tunnels) ${statePath()}\n`);
  process.exit(0);
}
for (const e of entries) {
  const alive = e.pid ? (() => { try { process.kill(e.pid, 0); return 'up'; } catch { return 'dead'; } })() : '-';
  process.stdout.write(
    `${(e.slug || '?').padEnd(20)} ${String(e.status).padEnd(8)} ` +
      `port ${e.metroPort ?? '-'}  pid ${e.pid ?? '-'} (${alive})  up ${age(e.startedAt)}  ${e.expUrl ?? ''}\n`
  );
}
