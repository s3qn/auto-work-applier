'use strict';
// Shared read/modify/write helpers for the dev-feature tunnel state file.
// Contract (kept in sync with the dev.sean.build dashboard):
//   ~/.claude/dev-feature-tunnels.json  ->  JSON array of entries:
//   { name, slug, branch, worktree, metroPort, expUrl,
//     status: "starting"|"ready"|"failed", pid, logPath, startedAt }
// Override the path with DEV_TUNNELS_STATE (used by tests).

const fs = require('fs');
const os = require('os');
const path = require('path');

const ACTIVE = new Set(['starting', 'ready']);
const MAX_ACTIVE = 3;

function statePath() {
  if (process.env.DEV_TUNNELS_STATE) return process.env.DEV_TUNNELS_STATE;
  return path.join(os.homedir(), '.claude', 'dev-feature-tunnels.json');
}

function readState() {
  const p = statePath();
  try {
    const raw = fs.readFileSync(p, 'utf8').trim();
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch (err) {
    if (err.code === 'ENOENT') return [];
    // A corrupt file should not wedge the whole loop; start clean but warn.
    process.stderr.write(`state-lib: ignoring unreadable state file (${err.message})\n`);
    return [];
  }
}

// Atomic write: temp file in the same dir + rename, so a reader never sees a
// half-written array.
function writeState(entries) {
  const p = statePath();
  fs.mkdirSync(path.dirname(p), { recursive: true });
  const tmp = `${p}.tmp.${process.pid}`;
  fs.writeFileSync(tmp, JSON.stringify(entries, null, 2) + '\n');
  fs.renameSync(tmp, p);
}

// Insert or replace the entry with the same slug.
function upsert(entry) {
  const entries = readState().filter((e) => e.slug !== entry.slug);
  entries.push(entry);
  writeState(entries);
  return entry;
}

function remove(slug) {
  const entries = readState();
  const kept = entries.filter((e) => e.slug !== slug);
  writeState(kept);
  return entries.length - kept.length; // how many we dropped
}

function get(slug) {
  return readState().find((e) => e.slug === slug) || null;
}

function activeCount() {
  return readState().filter((e) => ACTIVE.has(e.status)).length;
}

function usedPorts() {
  return new Set(
    readState()
      .map((e) => Number(e.metroPort))
      .filter((n) => Number.isInteger(n))
  );
}

module.exports = {
  ACTIVE,
  MAX_ACTIVE,
  statePath,
  readState,
  writeState,
  upsert,
  remove,
  get,
  activeCount,
  usedPorts,
};
